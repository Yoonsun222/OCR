import json
import os
import shutil
import subprocess

import pytest

from raglab.offline import windows


THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
VALID_RESULT = {
    "Status": "Valid",
    "Thumbprint": THUMBPRINT.lower(),
    "MembershipStatus": "Valid",
    "MembershipThumbprint": THUMBPRINT,
    "MembershipSignatureStatus": "Valid",
    "HashAlgorithm": "SHA256",
    "ManifestSha256": "A" * 64,
    "ApprovalSha256": "B" * 64,
}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    name = "bundle [space]; $(Write-Output INJECTED) O'Brien"
    root = tmp_path / name
    root.mkdir()
    catalog = root / "bundle.cat"
    catalog.touch()
    return catalog


def intercept_output(monkeypatch, payload, returncode=0):
    monkeypatch.setattr(
        windows.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, payload, ""),
    )


def test_powershell_command_keeps_catalog_path_out_of_script(catalog, monkeypatch):
    def inspect_process(command, **options):
        assert command[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
        assert command[0].endswith("powershell.exe")
        assert str(catalog) not in " ".join(command)
        assert str(catalog) in options["env"].values()
        assert "Get-AuthenticodeSignature -LiteralPath" in command[-1]
        assert "Test-FileCatalog" in command[-1]
        assert options["timeout"] > 0
        return subprocess.CompletedProcess(command, 0, json.dumps(VALID_RESULT), "")

    monkeypatch.setattr(windows.subprocess, "run", inspect_process)
    result = windows.PowerShellCatalogVerifier().verify(catalog)
    assert result.valid is True
    assert result.signer == THUMBPRINT
    assert result.manifest_sha256 == "a" * 64
    assert result.approval_sha256 == "b" * 64


@pytest.mark.parametrize("changes", [
    {"Status": "NotSigned"}, {"Status": 0}, {"Thumbprint": None}, {"Thumbprint": "bad"},
    {"MembershipStatus": "ValidationFailed"}, {"MembershipStatus": None},
    {"MembershipThumbprint": "C" * 40}, {"HashAlgorithm": "SHA1"},
    {"MembershipSignatureStatus": "HashMismatch"},
    {"ManifestSha256": None}, {"ApprovalSha256": None},
])
def test_invalid_signature_or_membership_blocks(catalog, monkeypatch, changes):
    intercept_output(monkeypatch, json.dumps({**VALID_RESULT, **changes}))
    assert windows.PowerShellCatalogVerifier().verify(catalog).valid is False


@pytest.mark.parametrize("payload,returncode", [
    ("not json", 0), ("null", 0), ("[]", 0), ("{}", 0),
    (json.dumps(VALID_RESULT), 1),
])
def test_invalid_subprocess_result_blocks(catalog, monkeypatch, payload, returncode):
    intercept_output(monkeypatch, payload, returncode)
    assert windows.PowerShellCatalogVerifier().verify(catalog).valid is False


@pytest.mark.parametrize("exception", [
    OSError("private-path"), subprocess.TimeoutExpired("private-path", 10),
])
def test_process_failure_has_generic_reason(catalog, monkeypatch, exception):
    def fail(*_, **__):
        raise exception

    monkeypatch.setattr(windows.subprocess, "run", fail)
    result = windows.PowerShellCatalogVerifier().verify(catalog)
    assert result.valid is False
    assert "private-path" not in (result.reason or "")


@pytest.mark.skipif(
    os.name != "nt" or not shutil.which("powershell.exe"), reason="Windows PowerShell 5.1 required",
)
def test_real_powershell_uses_literal_metacharacter_path(catalog, monkeypatch):
    monkeypatch.undo()
    # Create a real unsigned catalog on this local host and call the same production script,
    # which must see it as unsigned instead of dropping or interpreting path characters.
    staged = catalog.with_name("manifest.json")
    staged.write_text("{}", encoding="utf-8")
    catalog.unlink()
    sibling = catalog.parent.with_suffix(".cat")
    setup = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
Set-Location -LiteralPath $env:RAGLAB_TEST_ROOT
New-FileCatalog -Path . -CatalogFilePath $env:RAGLAB_TEST_CAT -CatalogVersion 2 | Out-Null
"""
    setup_result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", setup],
        env={**os.environ, "RAGLAB_TEST_ROOT": str(catalog.parent), "RAGLAB_TEST_CAT": str(sibling)},
        capture_output=True, timeout=10,
    )
    assert setup_result.returncode == 0, setup_result.stderr
    sibling.rename(catalog)

    native_run = subprocess.run
    observed = []

    def capture_result(*args, **kwargs):
        result = native_run(*args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(windows.subprocess, "run", capture_result)
    assert windows.PowerShellCatalogVerifier().verify(catalog).valid is False
    assert observed[0].returncode == 0, observed[0].stderr
    result = json.loads(observed[0].stdout)
    assert result["Status"] == "NotSigned"
    assert result["MembershipStatus"] == "Valid"
    assert result["ManifestSha256"] is not None
    assert result["ApprovalSha256"] is None

    assert result["MembershipSignatureStatus"] == "NotSigned"

    staged.write_text('{"tampered":true}', encoding="utf-8")
    assert windows.PowerShellCatalogVerifier().verify(catalog).valid is False
    assert observed[1].returncode == 0, observed[1].stderr
    assert json.loads(observed[1].stdout)["MembershipStatus"] == "ValidationFailed"
