import hashlib
import json
import os
import struct

import pytest

from raglab.cli import main
from raglab.offline.ports import CatalogDecision
from test_doctor import StaticEgress, StaticSystem, ready_snapshot
from test_verify_bundle_cli import SyntheticAuthority, SyntheticVerifier, bundle


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows offline doctor contract")


def invoke(bundle, report, *, verifier=None, egress="DENIED", extra=(), canary=None):
    return main(
        ["doctor", "--bundle", str(bundle), "--profile", "local16", "--offline",
         "--report", str(report), *extra],
        catalog_verifier=verifier or SyntheticVerifier(),
        publisher_authority=SyntheticAuthority(),
        system_probe=StaticSystem(ready_snapshot("local16")),
        egress_probe=StaticEgress(egress),
        active_canary=canary,
    )


@pytest.mark.parametrize("egress,exit_code", [("DENIED", 0), ("UNKNOWN", 5)])
def test_doctor_writes_atomic_payload_free_report(bundle, tmp_path, capsys, egress, exit_code):
    output = tmp_path / "private-output" / "doctor.json"
    assert invoke(bundle, output, egress=egress) == exit_code
    contents = output.read_text(encoding="utf-8")
    report = json.loads(contents)
    assert report["schema_version"] == 1
    assert report["overall_status"] == ("PASS" if exit_code == 0 else "BLOCK")
    assert report["profile"] == "local16"
    capture = capsys.readouterr()
    assert "OFFLINE_EGRESS_POLICY" in capture.out
    assert "private-" not in capture.out + capture.err + contents
    assert "nested/empty.txt" not in contents
    assert list(output.parent.iterdir()) == [output]


def test_signer_mismatch_blocks_doctor_and_catalog_check(bundle, tmp_path):
    class UnapprovedVerifier(SyntheticVerifier):
        def verify(self, *args):
            decision = super().verify(*args)
            return CatalogDecision(True, "f" * 40, None, decision.members)

    output = tmp_path / "reports" / "doctor.json"
    assert invoke(bundle, output, verifier=UnapprovedVerifier()) == 5
    checks = json.loads(output.read_text(encoding="utf-8"))["checks"]
    assert next(row for row in checks if row["check_id"] == "CATALOG_SIGNER")["status"] == "BLOCK"


def test_doctor_refuses_report_inside_bundle(bundle):
    target = bundle / "approval.json"
    contents = target.read_bytes()
    assert invoke(bundle, target) == 2
    assert target.read_bytes() == contents


def test_active_switch_fails_without_approved_adapter_and_profile(bundle, tmp_path):
    output = tmp_path / "reports" / "doctor.json"
    assert invoke(bundle, output, extra=("--active-egress-canary", "--approval-id", "APR-CANARY")) == 5
    assert json.loads(output.read_text(encoding="utf-8"))["overall_status"] == "BLOCK"


def add_artifact(bundle, relative, data):
    artifact = bundle / relative
    artifact.write_bytes(data)
    manifest = bundle / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["artifacts"].append({
        **document["artifacts"][0],
        "path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
    })
    manifest.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("valid", [True, False])
def test_canary_receives_only_pinned_signed_metadata(bundle, tmp_path, valid):
    endpoint = "https://private-sink.example.test/policy"
    authorization = json.dumps({
        "schema_version": 1,
        "canary": {"endpoint": endpoint, "approval_id": "APR-CANARY"},
    }).encode("utf-8")
    add_artifact(bundle, "site-profile.json", authorization)
    if not valid:
        (bundle / "site-profile.json").write_bytes(authorization + b" ")
    calls = []

    class Canary:
        def verify_denied(self, endpoint, approval_id):
            calls.append((endpoint, approval_id))
            return True

    output = tmp_path / "reports" / "doctor.json"
    extra = ("--active-egress-canary", "--approval-id", "APR-CANARY")
    assert invoke(bundle, output, extra=extra, canary=Canary()) == (0 if valid else 5)
    assert calls == ([(endpoint, "APR-CANARY")] if valid else [])
    assert endpoint not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("correct_machine,policy,expected", [
    (True, "valid", 0), (True, "missing", 5), (True, "wrong-generation", 5),
    (True, "wrong-architecture", 5), (True, "expired", 5), (True, "offset-expiry", 5),
    (False, "valid", 5),
])
def test_signed_dll_header_and_machine_loader_policy(bundle, tmp_path, monkeypatch, correct_machine, policy, expected):
    monkeypatch.setattr("raglab.offline.doctor.sysconfig.get_platform", lambda: "win-amd64")
    # This mock contains just enough static structure for the header check.
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    machine = 0x8664 if correct_machine else 0xAA64
    struct.pack_into("<HHIIIHHH", data, 68, machine, 1, 0, 0, 0, 240, 0x2002, 0x20B)
    add_artifact(bundle, "synthetic.dll", data)

    from raglab.offline import windows

    output = tmp_path / "reports" / "doctor.json"
    assert invoke(bundle, output) == 5
    fingerprint = json.loads(output.read_text(encoding="utf-8"))["generation_fingerprint"]
    attestation = {
        "schema_version": 1,
        "generation_fingerprint": "f" * 64 if policy == "wrong-generation" else fingerprint,
        "python_architecture": "win-arm64" if policy == "wrong-architecture" else "win-amd64",
        "expires_at": "2000-01-01T00:00:00Z" if policy == "expired" else "2099-01-01T00:00:00Z",
    }
    if policy == "offset-expiry":
        attestation["expires_at"] = "2099-01-01T00:00:00+00:00"

    def read_attestation(value_name):
        assert value_name == "DllLoaderReadinessAttestation"
        return None if policy == "missing" else attestation

    monkeypatch.setattr(windows, "_machine_policy_json", read_attestation)
    assert invoke(bundle, output) == expected
    report = json.loads(output.read_text(encoding="utf-8"))
    dll = next(check for check in report["checks"] if check["check_id"] == "DLL_INVENTORY")
    assert dll["status"] == ("PASS" if expected == 0 else "BLOCK")
    assert "synthetic.dll" not in repr(report)
