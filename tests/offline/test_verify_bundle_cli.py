import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from raglab.cli import main
from raglab.offline.ports import CatalogDecision


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows offline bundle contract")

THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "bundle-valid"


class SyntheticAuthority:
    def approved_thumbprints(self):
        return frozenset({THUMBPRINT})


class SyntheticVerifier:
    def verify(self, catalog, files, deadline) -> CatalogDecision:
        members = {
            name: hashlib.sha256(member.read_bytes(member.info.size)).hexdigest()
            for name, member in files.items()
        }
        return CatalogDecision(catalog.info is not None, THUMBPRINT, None, members)


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
        publisher_authority=SyntheticAuthority(),
    )


def test_verify_bundle_writes_passing_report(bundle, tmp_path):
    output = tmp_path / "reports" / "result.json"
    assert invoke(bundle, output) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report == {
        "status": "PASS", "approval_id": "APR-TEST", "failures": [],
        "generation_fingerprint": report["generation_fingerprint"],
    }
    assert len(report["generation_fingerprint"]) == 64


def test_verify_bundle_uses_default_report_path(bundle, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(
        ["verify-bundle", "--bundle", str(bundle)],
        catalog_verifier=SyntheticVerifier(), publisher_authority=SyntheticAuthority(),
    ) == 0
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
    from raglab.offline.windows import WindowsCatalogVerifier

    calls = []

    def successful_native(_verifier, catalog, files, deadline):
        calls.append(catalog.path.name)
        return SyntheticVerifier().verify(catalog, files, deadline)

    monkeypatch.setattr(WindowsCatalogVerifier, "verify", successful_native)
    assert main(
        ["verify-bundle", "--bundle", str(bundle), "--report", str(tmp_path / "report.json")],
        publisher_authority=SyntheticAuthority(),
    ) == 0
    assert calls == ["bundle.cat"]


def test_cli_parse_error_does_not_echo_unrecognized_operator_path(tmp_path, capsys):
    private_path = str(tmp_path / "private")
    assert main(["verify-bundle", "--bundle", str(tmp_path), private_path]) == 2
    assert private_path not in capsys.readouterr().err
