import hashlib
import json
import shutil
from pathlib import Path

import pytest

from raglab.cli import main
from raglab.offline.ports import CatalogDecision


THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "bundle-valid"


class SyntheticVerifier:
    def verify(self, catalog_path: Path) -> CatalogDecision:
        return CatalogDecision(catalog_path.is_file(), THUMBPRINT, None)


@pytest.fixture
def bundle(tmp_path):
    root = shutil.copytree(FIXTURE, tmp_path / "bundle [private location]")
    (root / "nested").mkdir()
    (root / "nested" / "empty.txt").touch()
    (root / "bundle.cat").touch()
    return root


def invoke(bundle, output):
    return main(
        ["verify-bundle", "--bundle", str(bundle), "--report", str(output)],
        catalog_verifier=SyntheticVerifier(),
    )


def test_verify_bundle_writes_passing_report(bundle, tmp_path):
    output = tmp_path / "reports" / "result.json"
    assert invoke(bundle, output) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "PASS", "approval_id": "APR-TEST", "failures": [],
    }


def test_verify_bundle_uses_default_report_path(bundle, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["verify-bundle", "--bundle", str(bundle)], catalog_verifier=SyntheticVerifier()) == 0
    assert json.loads(Path("runs/verify-bundle.json").read_text(encoding="utf-8"))["status"] == "PASS"


@pytest.mark.parametrize("failure,exit_code", [
    ("bundle path", 2), ("expiration", 3), ("license", 3), ("approval JSON", 3),
    ("manifest JSON", 4), ("artifact", 4), ("hash", 4), ("catalog", 4),
])
def test_stable_exit_code_and_redacted_report(bundle, tmp_path, capsys, failure, exit_code):
    if failure == "bundle path":
        bundle = bundle / "missing"
    elif failure == "expiration":
        approval = bundle / "approval.json"
        approval.write_text(
            approval.read_text(encoding="utf-8").replace("2099-01-01", "2000-01-01"),
            encoding="utf-8",
        )
    elif failure == "license":
        manifest = bundle / "manifest.json"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('"approved"', '"denied"'),
            encoding="utf-8",
        )
    elif failure == "approval JSON":
        (bundle / "approval.json").write_text("[invalid", encoding="utf-8")
    elif failure == "manifest JSON":
        (bundle / "manifest.json").write_text("[invalid", encoding="utf-8")
    elif failure == "artifact":
        (bundle / "nested" / "empty.txt").unlink()
    elif failure == "hash":
        (bundle / "nested" / "empty.txt").write_bytes(b"tampered")
    else:
        (bundle / "bundle.cat").unlink()

    output = tmp_path / "reports" / "verify.json"
    assert invoke(bundle, output) == exit_code
    data = output.read_text(encoding="utf-8")
    assert json.loads(data)["status"] == "BLOCK"
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out + captured.err + data
    assert "private location" not in captured.out + captured.err + data
    assert list(output.parent.iterdir()) == [output]


def test_invalid_report_path_returns_operator_error(bundle, tmp_path, capsys):
    output = tmp_path / "private-output"
    output.mkdir()
    assert invoke(bundle, output) == 2
    assert str(output) not in capsys.readouterr().err


def test_report_cannot_overwrite_bundle_metadata(bundle):
    output = bundle / "approval.json"
    original = output.read_bytes()
    assert invoke(bundle, output) == 2
    assert output.read_bytes() == original


def test_command_uses_production_verifier_by_default(bundle, tmp_path, monkeypatch):
    from raglab.offline import windows

    def successful_process(_command, **_options):
        import subprocess

        return subprocess.CompletedProcess(
            [], 0, json.dumps({
                "Status": "Valid",
                "Thumbprint": THUMBPRINT,
                "MembershipStatus": "Valid",
                "MembershipThumbprint": THUMBPRINT,
                "MembershipSignatureStatus": "Valid",
                "HashAlgorithm": "SHA256",
                "ManifestSha256": hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest(),
                "ApprovalSha256": hashlib.sha256((bundle / "approval.json").read_bytes()).hexdigest(),
            }), "",
        )

    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(windows.subprocess, "run", successful_process)
    assert main([
        "verify-bundle", "--bundle", str(bundle), "--report", str(tmp_path / "report.json"),
    ]) == 0


def test_cli_parse_error_does_not_echo_unrecognized_operator_path(tmp_path, capsys):
    private_path = str(tmp_path / "private")
    assert main(["verify-bundle", "--bundle", str(tmp_path), private_path]) == 2
    assert private_path not in capsys.readouterr().err
