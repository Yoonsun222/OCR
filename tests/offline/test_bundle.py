import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from raglab.offline.bundle import verify_bundle as verify
from raglab.offline.ports import CatalogDecision


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows offline bundle contract")

THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "bundle-valid"


class SyntheticAuthority:
    def approved_thumbprints(self):
        return frozenset({THUMBPRINT})


def verify_bundle(root, verifier):
    return verify(root, verifier, publisher_authority=SyntheticAuthority())


class AcceptCatalog:
    def verify(self, catalog, files, deadline) -> CatalogDecision:
        assert catalog.path.name == "bundle.cat"
        assert catalog.info is not None
        members = {
            name: hashlib.sha256(member.read_bytes(member.info.size)).hexdigest()
            for name, member in files.items()
        }
        return CatalogDecision(True, THUMBPRINT.lower(), None, members)


@pytest.fixture
def bundle(tmp_path):
    root = shutil.copytree(FIXTURE, tmp_path / "bundle")
    (root / "nested").mkdir()
    (root / "nested" / "empty.txt").touch()
    (root / "bundle.cat").write_bytes(b"synthetic catalog")
    return root


def modify_json(path, **changes):
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")


def modify_artifact(root, **changes):
    path = root / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["artifacts"][0].update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")


def assert_failure(report, suffix, artifact):
    assert report.status == "BLOCK"
    assert (report.failures[0].code, report.failures[0].artifact) == (
        f"RAGLAB-BUNDLE-{suffix}", artifact,
    )


def test_valid_bundle_passes(bundle):
    report = verify_bundle(bundle, AcceptCatalog())
    assert report.status == "PASS"
    assert report.approval_id == "APR-TEST"
    assert report.failures == ()


def test_invalid_bundle_directory_blocks(tmp_path):
    report = verify_bundle(tmp_path / "missing", AcceptCatalog())
    assert_failure(report, "001", ".")


def test_hash_mismatch_blocks_bundle(bundle):
    (bundle / "nested" / "empty.txt").write_bytes(b"changed")
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "002", "nested/empty.txt")


def test_missing_artifact_blocks_bundle(bundle):
    (bundle / "nested" / "empty.txt").unlink()
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "001", "nested/empty.txt")


def test_windows_relative_path_is_normalized(bundle):
    modify_artifact(bundle, path=r"nested\empty.txt")
    assert verify_bundle(bundle, AcceptCatalog()).status == "PASS"


@pytest.mark.parametrize("unsafe", [
    "../outside.txt", "nested/../nested/empty.txt", "/private/operator.txt",
    r"C:\private\operator.txt", r"C:operator.txt", r"\operator.txt",
    r"\\server\share\operator.txt", r"\\?\C:\private\operator.txt",
    "nested/empty.txt:secret", "nested/empty.txt.", "nested /empty.txt",
    "nested/CON.txt", "nested/LPT1.txt", "nested//empty.txt", "nested/./empty.txt",
    "nested/\u0000", "nested/\ud800",
])
def test_unsafe_path_is_rejected_without_echo(bundle, unsafe):
    modify_artifact(bundle, path=unsafe)
    report = verify_bundle(bundle, AcceptCatalog())
    assert_failure(report, "001", "manifest.json")
    assert unsafe not in repr(report)


def test_absolute_operator_artifact_path_is_not_echoed(bundle, tmp_path):
    target = tmp_path / "private-operator"
    target.touch()
    modify_artifact(bundle, path=str(target))
    report = verify_bundle(bundle, AcceptCatalog())
    assert_failure(report, "001", "manifest.json")
    assert str(target) not in repr(report)
    assert target.name not in repr(report)


@pytest.mark.parametrize("document,artifact,code", [
    ("manifest.json", "manifest.json", "001"),
    ("approval.json", "approval.json", "004"),
    ("bundle.cat", "bundle.cat", "003"),
    ("nested/empty.txt", "nested/empty.txt", "001"),
    ("nested", "nested", "001"),
])
def test_symlink_outside_bundle_is_rejected(bundle, tmp_path, document, artifact, code):
    member = bundle / document
    outside = tmp_path / "outside"
    member.rename(outside)
    try:
        member.symlink_to(outside, target_is_directory=outside.is_dir())
    except OSError as exc:
        pytest.skip(f"symlink not available ({type(exc).__name__})")
    assert_failure(verify_bundle(bundle, AcceptCatalog()), code, artifact)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_windows_junction_outside_bundle_is_rejected(bundle, tmp_path):
    outside = tmp_path / "outside"
    (bundle / "nested").rename(outside)
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(bundle / "nested"), str(outside)],
        check=True, capture_output=True,
    )
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "001", "nested")


def test_casefolded_duplicate_paths_block_bundle(bundle):
    path = bundle / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["artifacts"].append({**document["artifacts"][0], "path": "NESTED/EMPTY.TXT"})
    path.write_text(json.dumps(document), encoding="utf-8")
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "001", "manifest.json")


@pytest.mark.parametrize("data", [
    b"{invalid", b"[]", b"\xff", b'{"schema_version":1,"schema_version":1}',
    b'{"schema_version":true,"artifacts":[]}', b'{"schema_version":2,"artifacts":[]}',
    b'{"schema_version":1,"artifacts":[]}', b'{"schema_version":1,"artifacts":[null]}',
    b'{"schema_version":1,"artifacts":NaN}',
])
def test_malformed_manifest_blocks_bundle(bundle, data):
    (bundle / "manifest.json").write_bytes(data)
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "001", "manifest.json")


@pytest.mark.parametrize("changes", [{"path": 42}, {"sha256": "xyz"}, {"sha256": None}])
def test_malformed_manifest_artifact_blocks_bundle(bundle, changes):
    modify_artifact(bundle, **changes)
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "001", "manifest.json")


@pytest.mark.parametrize("changes", [
    {"approval_id": None}, {"approval_id": r"C:\private\approval"},
    {"expires_at": "2099-01-01T00:00:00"}, {"expires_at": "invalid"},
    {"allowed_signer_thumbprints": []}, {"allowed_signer_thumbprints": None},
    {"allowed_signer_thumbprints": THUMBPRINT}, {"allowed_signer_thumbprints": ["broken"]},
])
def test_malformed_approval_blocks_bundle(bundle, changes):
    modify_json(bundle / "approval.json", **changes)
    report = verify_bundle(bundle, AcceptCatalog())
    assert_failure(report, "004", "approval.json")
    assert report.approval_id is None


def test_missing_signer_allowlist_blocks_bundle(bundle):
    (bundle / "approval.json").write_text(
        '{"approval_id":"APR-TEST","expires_at":"2099-01-01T00:00:00Z"}', encoding="utf-8",
    )
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "004", "approval.json")


def test_expired_approval_blocks_bundle(bundle):
    modify_json(bundle / "approval.json", expires_at="2000-01-01T00:00:00Z")
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "004", "approval.json")


def test_unapproved_license_blocks_bundle(bundle):
    modify_artifact(bundle, license_decision="denied")
    assert_failure(verify_bundle(bundle, AcceptCatalog()), "005", "nested/empty.txt")


def test_missing_catalog_blocks_before_calling_verifier(bundle):
    class UnusedVerifier:
        def verify(self, *_):
            pytest.fail("a missing catalog reached the verifier")

    (bundle / "bundle.cat").unlink()
    assert_failure(verify_bundle(bundle, UnusedVerifier()), "003", "bundle.cat")


def test_verifier_reason_and_signer_are_never_echoed(bundle):
    private = str(bundle / "private-input")

    class RejectCatalog:
        def verify(self, *_):
            return CatalogDecision(False, private, private)

    report = verify_bundle(bundle, RejectCatalog())
    assert_failure(report, "003", "bundle.cat")
    assert private not in repr(report)


def test_unsigned_approval_id_does_not_appear_in_catalog_failure(bundle):
    class RejectCatalog:
        def verify(self, *_):
            return CatalogDecision(False, None, None)

    report = verify_bundle(bundle, RejectCatalog())
    assert report.approval_id is None


def test_catalog_signer_must_be_in_approval_allowlist(bundle):
    modify_json(bundle / "approval.json", allowed_signer_thumbprints=["A" * 40])
    report = verify_bundle(bundle, AcceptCatalog())
    assert_failure(report, "003", "bundle.cat")
    assert report.approval_id is None


def test_verifier_exception_blocks_without_exposing_operator_paths(bundle):
    private = str(bundle / "private-input")

    class CrashingVerifier:
        def verify(self, *_):
            raise RuntimeError(private)

    report = verify_bundle(bundle, CrashingVerifier())
    assert_failure(report, "003", "bundle.cat")
    assert private not in repr(report)


def test_verified_metadata_digest_must_match_the_parsed_bytes(bundle):
    class MismatchedApprovalCatalog(AcceptCatalog):
        def verify(self, catalog, files, deadline):
            decision = super().verify(catalog, files, deadline)
            return CatalogDecision(
                True, THUMBPRINT, None, {**decision.members, "approval.json": "0" * 64},
            )

    report = verify_bundle(bundle, MismatchedApprovalCatalog())
    assert_failure(report, "003", "bundle.cat")
    assert report.approval_id is None
