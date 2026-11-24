import hashlib
import json
import os
from dataclasses import replace

import pytest

from raglab.cli import main
from raglab.offline.bundle import verify_bundle
from raglab.offline.ports import CatalogDecision


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows offline bundle contract")
SIGNER = "A" * 40


class Authority:
    def __init__(self, *signers):
        self.signers = frozenset(signers)

    def approved_thumbprints(self):
        return self.signers


class Catalog:
    def __init__(self, change=None):
        self.change = change
        self.calls = 0

    def verify(self, catalog, files, deadline):
        self.calls += 1
        members = {
            name: hashlib.sha256(member.read_bytes(member.info.size)).hexdigest()
            for name, member in files.items()
        }
        if self.change is not None:
            self.change(catalog, files, members)
        return CatalogDecision(True, SIGNER, None, members)


@pytest.fixture
def prepared(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "payload.txt").write_bytes(b"synthetic")
    (bundle / "bundle.cat").write_bytes(b"fake catalog")
    manifest = {
        "schema_version": 1,
        "artifacts": [{
            "path": "payload.txt", "sha256": hashlib.sha256(b"synthetic").hexdigest(),
            "license_decision": "approved",
        }],
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    approval = {
        "approval_id": "APR-BUNDLE",
        "expires_at": "2099-01-01T00:00:00Z",
        "allowed_signer_thumbprints": [SIGNER],
    }
    (bundle / "approval.json").write_text(json.dumps(approval), encoding="utf-8")
    return bundle


def run(bundle, catalog=None, **options):
    options.setdefault("publisher_authority", Authority(SIGNER))
    return verify_bundle(bundle, catalog or Catalog(), **options)


def test_signed_unlisted_payload_cannot_bypass_license_inventory(prepared):
    (prepared / "extra.dll").write_bytes(b"unsigned fixture")
    assert run(prepared).status == "BLOCK"


@pytest.mark.parametrize("control", ["manifest.json", "approval.json", "bundle.cat"])
def test_control_files_are_reserved_from_payload_inventory(prepared, control):
    payload = prepared / "manifest.json"
    manifest = json.loads(payload.read_text())
    manifest["artifacts"].append({
        "path": control.upper(), "sha256": "0" * 64, "license_decision": "approved",
    })
    payload.write_text(json.dumps(manifest), encoding="utf-8")
    report = run(prepared)
    assert report.status == "BLOCK"
    assert report.failures[0].artifact == "manifest.json"


@pytest.mark.parametrize("catalog_change", [
    lambda entries: entries.pop("payload.txt"),
    lambda entries: entries.update({"extra.dll": "0" * 64}),
    lambda entries: entries.pop("approval.json"),
])
def test_catalog_inventory_must_equal_signed_metadata_and_payloads(prepared, catalog_change):
    report = run(prepared, Catalog(lambda _cat, _files, members: catalog_change(members)))
    assert report.status == "BLOCK"
    assert report.failures[0].artifact == "bundle.cat"


def test_policy_independently_limits_publishers(prepared):
    assert run(prepared, publisher_authority=Authority("B" * 40)).status == "BLOCK"


def test_missing_machine_policy_blocks_cli_before_catalog_verification(prepared, tmp_path, monkeypatch):
    import winreg

    def absent(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(winreg, "OpenKey", absent)
    fake = Catalog()
    output = tmp_path / "result.json"
    assert main([
        "verify-bundle", "--bundle", str(prepared), "--report", str(output),
    ], catalog_verifier=fake) == 3
    assert fake.calls == 0
    assert json.loads(output.read_text())["status"] == "BLOCK"


@pytest.mark.parametrize("limit,value", [
    ("metadata_bytes", 20), ("catalog_bytes", 3), ("artifact_count", 0),
    ("inventory_entries", 2), ("inventory_depth", 0), ("payload_bytes", 5),
])
def test_intake_limits_block_before_verifier(prepared, limit, value):
    from raglab.offline.ports import VerificationLimits

    nested = prepared / "nested"
    nested.mkdir()
    (prepared / "payload.txt").rename(nested / "payload.txt")
    manifest_path = prepared / "manifest.json"
    manifest_path.write_text(manifest_path.read_text().replace("payload.txt", "nested/payload.txt"))
    fake = Catalog()
    limits = replace(VerificationLimits(), **{limit: value})
    assert run(prepared, fake, limits=limits).status == "BLOCK"
    assert fake.calls == 0


@pytest.mark.parametrize("alias", ["literal", "hardlink", "case"])
def test_report_collision_never_replaces_existing_non_directory_input(tmp_path, alias):
    source = tmp_path / "invalid-bundle"
    source.write_bytes(b"preserve")
    output = source
    if alias == "hardlink":
        output = tmp_path / "alias"
        os.link(source, output)
    if alias == "case":
        output = source.with_name(source.name.upper())
    assert main([
        "verify-bundle", "--bundle", str(source), "--report", str(output),
    ], catalog_verifier=Catalog(), publisher_authority=Authority(SIGNER)) == 2
    assert source.read_bytes() == b"preserve"
    assert output.read_bytes() == b"preserve"


def test_report_inside_bundle_refused_before_catalog_verification(prepared):
    fake = Catalog()
    output = prepared / "other" / "report.json"
    assert main([
        "verify-bundle", "--bundle", str(prepared), "--report", str(output),
    ], catalog_verifier=fake, publisher_authority=Authority(SIGNER)) == 2
    assert fake.calls == 0
    assert not output.parent.exists()


@pytest.mark.parametrize("remote", [r"\\bad.invalid\share\bundle", r"\\?\UNC\bad.invalid\share\bundle", r"\\.\C:\bundle"])
def test_unc_and_device_paths_fail_before_native_drive_probe(monkeypatch, remote):
    from raglab.offline.windows_paths import WindowsLocalPaths

    def forbid(*_args):
        pytest.fail("lexically forbidden path reached the native filesystem")

    monkeypatch.setattr(WindowsLocalPaths, "drive_type", forbid)
    with pytest.raises(ValueError):
        WindowsLocalPaths().validate(remote)


def test_mapped_network_drive_rejected_before_any_handle_open(monkeypatch):
    from raglab.offline.windows_paths import WindowsLocalPaths

    monkeypatch.setattr(WindowsLocalPaths, "volume_target", lambda *_: r"\Device\HarddiskVolume42")
    monkeypatch.setattr(WindowsLocalPaths, "drive_type", lambda *_: 4)
    with pytest.raises(ValueError):
        WindowsLocalPaths().validate("Z:/synthetic")


def test_held_files_deny_mutation_during_catalog_verification(prepared):
    def try_mutations(catalog, files, _members):
        with pytest.raises(PermissionError):
            files["payload.txt"].path.write_bytes(b"mutated")
        with pytest.raises(PermissionError):
            catalog.path.rename(catalog.path.with_name("moved.cat"))

    report = run(prepared, Catalog(try_mutations))
    assert report.status == "PASS"
    assert len(report.generation_fingerprint) == 64
    assert run(prepared).generation_fingerprint == report.generation_fingerprint


def test_new_member_during_native_check_invalidates_verification(prepared):
    def introduce_member(catalog, _files, _members):
        (catalog.path.parent / "new.exe").write_bytes(b"unapproved")

    assert run(prepared, Catalog(introduce_member)).status == "BLOCK"


def test_generation_fingerprint_covers_catalog_bytes(prepared):
    first = run(prepared).generation_fingerprint
    (prepared / "bundle.cat").write_bytes(b"updated fake catalog")
    second = run(prepared).generation_fingerprint
    assert first and second and first != second


def test_invalid_offset_minute_is_not_normalized(prepared):
    approval = prepared / "approval.json"
    approval.write_text(approval.read_text().replace("00:00:00Z", "00:00:00+00:99"))
    assert run(prepared).status == "BLOCK"


def test_substituted_volume_alias_rejected_before_drive_type_query(monkeypatch):
    from raglab.offline.windows_paths import WindowsLocalPaths

    monkeypatch.setattr(WindowsLocalPaths, "volume_target", lambda *_: r"\??\C:\redirected")
    monkeypatch.setattr(
        WindowsLocalPaths, "drive_type", lambda *_: pytest.fail("drive alias reached filesystem query"),
    )
    with pytest.raises(ValueError):
        WindowsLocalPaths().validate("Z:/synthetic")


def test_existing_report_parent_cannot_follow_reparse_point(prepared, tmp_path):
    import subprocess

    other = tmp_path / "other"
    other.mkdir()
    junction = tmp_path / "junction"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(other)],
        check=True, capture_output=True,
    )
    fake = Catalog()
    assert main([
        "verify-bundle", "--bundle", str(prepared), "--report", str(junction / "result.json"),
    ], catalog_verifier=fake, publisher_authority=Authority(SIGNER)) == 2
    assert fake.calls == 0
    assert not (other / "result.json").exists()


def test_report_parent_is_pinned_while_bundle_is_verified(prepared, tmp_path):
    reports = tmp_path / "reports" / "parent"
    reports.mkdir(parents=True)
    output = reports / "published.json"

    def try_move(_catalog, _files, _members):
        with pytest.raises(PermissionError):
            reports.rename(reports.with_name("replacement-parent"))

    assert main([
        "verify-bundle", "--bundle", str(prepared), "--report", str(output),
    ], catalog_verifier=Catalog(try_move), publisher_authority=Authority(SIGNER)) == 0
    assert output.is_file()
