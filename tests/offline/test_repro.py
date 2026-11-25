import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from raglab.offline.bundle import BundleReport
from raglab.offline.config import load_profile_config
from raglab.offline.demo import DemoResult, read_doctor_report
from raglab.offline.jsonio import serialize_json
from raglab.offline.progress import ProgressEvent
from raglab.offline.repro import (
    ApprovedDatasetManifest, ReproInputs, build_repro_bundle, verify_repro_bundle,
)


FILES = {
    "approvals.snapshot.json", "artifact-lock.json", "dataset-manifest.json",
    "deletion-report.json", "environment.json", "errors.jsonl",
    "effective-config.redacted.json", "manifest.sha256", "metrics.json",
    "network-check.json", "pipeline-manifest.json", "run-manifest.json",
}
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows local path contract")


def repro_inputs():
    doctor = read_doctor_report(Path("tests/fixtures/doctor-pass.json"))
    run_id = "1" * 32
    return ReproInputs(
        bundle_report=BundleReport("PASS", "APR-TEST", (), doctor.generation_fingerprint),
        config=load_profile_config("local16"),
        doctor_report=doctor,
        demo_result=DemoResult(1, run_id, "private synthetic text", "9" * 64, (), "secret.pdf", 15),
        environment={
            "os_build": "10.0.26100", "cpu": "x64", "ram_tier": "local16",
            "python": "3.11.9", "locale": "ko-KR", "timezone": "+09:00",
            "power_plan": "balanced",
        },
        progress=(ProgressEvent(1, run_id, 1, "synthetic_demo", 1, 1, 15, 1024, 0, "PASS"),),
        deletion_report={"schema_version": 1, "run_id": run_id, "status": "PASS"},
        dataset=ApprovedDatasetManifest("synthetic-mini", "1.0", 1, "7" * 64),
    )


def _rehash(bundle):
    manifest = "".join(
        f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(FILES - {"manifest.sha256"})
    )
    (bundle / "manifest.sha256").write_bytes(manifest.encode("ascii"))


def test_bundle_contains_only_export_approved_data(tmp_path):
    bundle = tmp_path / "bundle"
    assert build_repro_bundle(repro_inputs(), bundle).status == "PASS"
    assert verify_repro_bundle(bundle).status == "PASS"
    assert {p.name for p in bundle.iterdir()} == FILES
    exported = b"".join(p.read_bytes() for p in bundle.iterdir())
    assert b"secret.pdf" not in exported
    assert b"private synthetic text" not in exported
    assert b"9" * 64 not in exported
    assert json.loads((bundle / "dataset-manifest.json").read_bytes()) == {
        "dataset_id": "synthetic-mini", "version": "1.0", "question_count": 1, "manifest_hash": "7" * 64,
    }


def test_build_is_deterministic_and_never_replaces_existing_output(tmp_path):
    inputs = repro_inputs()
    first, second = tmp_path / "first", tmp_path / "second"
    assert build_repro_bundle(inputs, first).status == "PASS"
    assert build_repro_bundle(inputs, second).status == "PASS"
    original = {p.name: p.read_bytes() for p in first.iterdir()}
    assert {p.name: p.read_bytes() for p in second.iterdir()} == original
    assert build_repro_bundle(inputs, first).status == "BLOCK"
    assert {p.name: p.read_bytes() for p in first.iterdir()} == original
    assert {p.name for p in tmp_path.iterdir()} == {"first", "second"}


@pytest.mark.parametrize("change", ["hash", "extra", "missing", "traversal", "hardlink", "oversized"])
def test_rejects_changed_inventory_or_files(tmp_path, change):
    bundle = tmp_path / "bundle"
    assert build_repro_bundle(repro_inputs(), bundle).status == "PASS"
    metrics = bundle / "metrics.json"
    if change == "hash":
        metrics.write_bytes(b"{}")
    elif change == "extra":
        (bundle / "secret.pdf").write_bytes(b"private")
    elif change == "missing":
        metrics.unlink()
    elif change == "traversal":
        manifest = bundle / "manifest.sha256"
        manifest.write_bytes(manifest.read_bytes().replace(b"metrics.json", b"../metrics.json"))
    elif change == "hardlink":
        os.link(metrics, tmp_path / "outside.json")
    else:
        metrics.write_bytes(b" " * (1024 * 1024 + 1))
    assert verify_repro_bundle(bundle).status == "BLOCK"


def test_rejects_link_to_valid_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    assert build_repro_bundle(repro_inputs(), bundle).status == "PASS"
    link = tmp_path / "link"
    try:
        link.symlink_to(bundle, target_is_directory=True)
    except OSError as exc:
        if exc.winerror != 1314:
            raise
        pytest.skip("creating symlinks requires Windows Developer Mode or privilege")
    assert verify_repro_bundle(link).status == "BLOCK"


@pytest.mark.parametrize(("file", "field", "value"), [
    ("environment.json", "cpu", r"C:\Users\owner\secret.pdf"),
    ("environment.json", "power_plan", "Employee Private Plan"),
    ("run-manifest.json", "schema_version", True),
    ("run-manifest.json", "schema_version", 2),
    ("run-manifest.json", "document-NAME", "secret.pdf"),
    ("dataset-manifest.json", "document_hashes", ["9" * 64]),
    ("metrics.json", "elapsed_ms", False),
    ("network-check.json", "status", "BLOCK"),
])
def test_rehash_cannot_make_invalid_metadata_acceptable(tmp_path, file, field, value):
    bundle = tmp_path / "bundle"
    assert build_repro_bundle(repro_inputs(), bundle).status == "PASS"
    member = bundle / file
    payload = json.loads(member.read_bytes())
    payload[field] = value
    member.write_bytes(serialize_json(payload).encode("utf-8"))
    _rehash(bundle)
    assert verify_repro_bundle(bundle).status == "BLOCK"


@pytest.mark.parametrize("field", ["unknown", "document_name"])
def test_unknown_source_fields_block_before_publication(tmp_path, field):
    inputs = repro_inputs()
    inputs = replace(inputs, environment={**inputs.environment, field: "secret.pdf"})
    output = tmp_path / "bundle"
    assert build_repro_bundle(inputs, output).status == "BLOCK"
    assert list(tmp_path.iterdir()) == []



def test_concurrent_destination_is_preserved_and_staging_is_removed(tmp_path, monkeypatch):
    from raglab.offline.windows_paths import WindowsLocalPaths

    rename = WindowsLocalPaths._rename_file

    def occupied(self, handle, destination, *, replace=True):
        destination.mkdir()
        (destination / "owner.txt").write_text("existing", encoding="utf-8")
        return rename(self, handle, destination, replace=replace)

    monkeypatch.setattr(WindowsLocalPaths, "_rename_file", occupied)
    destination = tmp_path / "bundle"
    assert build_repro_bundle(repro_inputs(), destination).status == "BLOCK"
    assert (destination / "owner.txt").read_text(encoding="utf-8") == "existing"
    assert {p.name for p in tmp_path.iterdir()} == {"bundle"}


def test_staged_write_during_rename_blocks_publication(tmp_path, monkeypatch):
    from raglab.offline.windows_paths import WindowsLocalPaths

    rename = WindowsLocalPaths._rename_file

    def changed(self, handle, destination, *, replace=True):
        staged = self._canonical(handle)
        (staged / "metrics.json").write_bytes(b"{}")
        return rename(self, handle, destination, replace=replace)

    monkeypatch.setattr(WindowsLocalPaths, "_rename_file", changed)
    assert build_repro_bundle(repro_inputs(), tmp_path / "bundle").status == "BLOCK"
    assert list(tmp_path.iterdir()) == []


def test_unsupported_typed_error_payload_is_refused_even_after_rehash(tmp_path):
    bundle = tmp_path / "bundle"
    assert build_repro_bundle(repro_inputs(), bundle).status == "PASS"
    (bundle / "errors.jsonl").write_bytes(
        b'{"code":"POLICY_DENIED","schema_version":1,"observed":"secret.pdf"}\n'
    )
    _rehash(bundle)
    assert verify_repro_bundle(bundle).status == "BLOCK"


def test_rejects_local_directory_junctions_for_build_and_verify(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    bundle = outside / "bundle"
    assert build_repro_bundle(repro_inputs(), bundle).status == "PASS"
    junction = tmp_path / "junction"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True, capture_output=True,
    )
    try:
        assert verify_repro_bundle(junction / "bundle").status == "BLOCK"
        assert build_repro_bundle(repro_inputs(), junction / "redirected").status == "BLOCK"
        assert not (outside / "redirected").exists()
    finally:
        junction.rmdir()


def test_late_foreign_member_keeps_failed_bundle_out_of_destination(tmp_path, monkeypatch):
    from raglab.offline.windows_paths import WindowsLocalPaths

    rename = WindowsLocalPaths._rename_file
    output = tmp_path / "bundle"

    def foreign_member(self, handle, destination, *, replace=True):
        if destination == output:
            (self._canonical(handle) / "unowned.txt").write_bytes(b"foreign")
        return rename(self, handle, destination, replace=replace)

    monkeypatch.setattr(WindowsLocalPaths, "_rename_file", foreign_member)
    assert build_repro_bundle(repro_inputs(), output).status == "BLOCK"
    assert not output.exists()
    [foreign] = tmp_path.glob(".raglab-*.tmp/unowned.txt")
    assert foreign.read_bytes() == b"foreign"
    assert list(foreign.parent.iterdir()) == [foreign]


def test_path_creation_gap_cannot_take_custody_of_foreign_directory(tmp_path, monkeypatch):
    mkdir = Path.mkdir
    substituted = []

    def replace_unheld_stage(self, *args, **kwargs):
        mkdir(self, *args, **kwargs)
        if self.parent == tmp_path and self.name.startswith(".raglab-"):
            self.rmdir()
            mkdir(self)
            substituted.append(self)

    monkeypatch.setattr(Path, "mkdir", replace_unheld_stage)
    destination = tmp_path / "bundle"
    report = build_repro_bundle(repro_inputs(), destination)
    if substituted:
        [foreign] = substituted
        assert report.status == "BLOCK"
        assert foreign.is_dir()
        assert list(foreign.iterdir()) == []
        assert not destination.exists()
    else:
        assert report.status == "PASS"
        assert verify_repro_bundle(destination).status == "PASS"


@pytest.mark.parametrize("change", ["missing", "blocked", "duplicate"])
def test_complete_passing_doctor_checks_required_for_repro(tmp_path, change):
    inputs = repro_inputs()
    first, *other = inputs.doctor_report.checks
    if change == "missing":
        checks = tuple(other)
    elif change == "blocked":
        checks = (replace(first, status="BLOCK"), *other)
    else:
        checks = (first, first, *other)
    report = replace(inputs.doctor_report, checks=checks)
    assert build_repro_bundle(replace(inputs, doctor_report=report), tmp_path / "bundle").status == "BLOCK"


def test_historical_complete_doctor_report_can_be_exported(tmp_path):
    inputs = repro_inputs()
    report = replace(
        inputs.doctor_report, generated_at="2025-01-01T00:00:00Z",
        bundle_approval_expires_at="2025-01-02T00:00:00Z",
    )
    assert build_repro_bundle(replace(inputs, doctor_report=report), tmp_path / "bundle").status == "PASS"


def test_native_staging_creation_refuses_existing_name(tmp_path, monkeypatch):
    from uuid import UUID

    from raglab.offline import windows_paths

    identifier = UUID(int=1)
    staging = tmp_path / f".raglab-{identifier.hex}.tmp"
    staging.mkdir()
    preserved = staging / "foreign.txt"
    preserved.write_bytes(b"untouched")
    monkeypatch.setattr(windows_paths.uuid, "uuid4", lambda: identifier)

    destination = tmp_path / "bundle"
    assert build_repro_bundle(repro_inputs(), destination).status == "BLOCK"
    assert preserved.read_bytes() == b"untouched"
    assert not destination.exists()
