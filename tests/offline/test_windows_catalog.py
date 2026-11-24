import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack, nullcontext

import pytest

from raglab.offline import windows
from raglab.offline.ports import VerificationDeadline
from raglab.offline.windows_paths import WindowsLocalPaths


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows catalog API")
THUMBPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"


def test_company_publisher_policy_reads_hklm_64_bit_view(monkeypatch):
    import winreg

    def open_key(hive, path, reserved, flags):
        assert hive == winreg.HKEY_LOCAL_MACHINE
        assert path == r"SOFTWARE\Policies\RagLab\OfflineToolkit"
        assert reserved == 0
        assert flags == winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        return nullcontext("machine policy")

    def query(key, value):
        assert key == "machine policy"
        assert value == "ApprovedPublisherThumbprints"
        return [THUMBPRINT.lower()], winreg.REG_MULTI_SZ

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query)
    assert windows.MachinePublisherAuthority().approved_thumbprints() == {THUMBPRINT}


@pytest.mark.parametrize("policy", [[], ["bad"], ["A" * 41], ["0123"]])
def test_invalid_machine_publisher_policy_blocks(monkeypatch, policy):
    import winreg

    monkeypatch.setattr(winreg, "OpenKey", lambda *args: nullcontext(None))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *args: (policy, winreg.REG_MULTI_SZ))
    with pytest.raises(ValueError):
        windows.MachinePublisherAuthority().approved_thumbprints()


def test_catalog_native_wintrust_uses_cache_only_and_closes_expired_state(tmp_path):
    import raglab.offline.windows_paths as paths

    observed = []
    now = 0
    deadline = VerificationDeadline(1, lambda: now)

    def fake_wintrust(window, policy, pointer):
        nonlocal now
        data = ctypes.cast(pointer, ctypes.POINTER(windows._WinTrustData)).contents
        observed.append(data.state_action)
        assert data.provider_flags & 0x1000
        assert data.file.contents.file == 5
        if data.state_action == 1:
            data.state_data = 99
            now = 2
        return 0

    class Probe(windows._NativeCatalog):
        def __init__(self):
            self.verify_trust = fake_wintrust

    fixture = paths.PinnedPath(tmp_path / "bundle.cat", None, 5)
    with pytest.raises(TimeoutError):
        Probe().signer(fixture, deadline)
    assert observed == [1, 2]

    observed.clear()
    with pytest.raises(TimeoutError):
        Probe().signer(fixture, deadline)
    assert observed == []


def test_catalog_open_deadline_closes_native_handle(tmp_path):
    import raglab.offline.windows_paths as paths

    now = 0
    calls = []
    deadline = VerificationDeadline(1, lambda: now)

    class Probe(windows._NativeCatalog):
        def __init__(self):
            pass

        def cat_open(self, *_args):
            nonlocal now
            calls.append("open")
            now = 2
            return 12

        def cat_close(self, handle):
            assert handle == 12
            calls.append("close")
            return True

    with pytest.raises(TimeoutError):
        Probe().members(paths.PinnedPath(tmp_path / "bundle.cat", None), 3, deadline)
    assert calls == ["open", "close"]


@pytest.mark.skipif(not shutil.which("powershell.exe"), reason="PowerShell 5.1 catalog fixture")
def test_native_catalog_uses_literal_path_and_matches_windows_sip_hashes(tmp_path):
    root = tmp_path / "bundle [space]; $(Write-Output INJECTED) O'Brien"
    root.mkdir()
    artifact = root / "payload.exe"
    shutil.copyfile(sys.executable, artifact)
    (root / "manifest.json").write_bytes(b"{}")
    (root / "approval.json").write_bytes(b"{}")
    output = tmp_path / "source.cat"
    setup = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { exit 2 }
Set-Location -LiteralPath $env:RAGLAB_TEST_ROOT
New-FileCatalog -Path . -CatalogFilePath $env:RAGLAB_TEST_CAT -CatalogVersion 2 | Out-Null
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", setup],
        env={**os.environ, "RAGLAB_TEST_ROOT": str(root), "RAGLAB_TEST_CAT": str(output)},
        capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    output.rename(root / "bundle.cat")
    paths = WindowsLocalPaths()
    native = windows._NativeCatalog()
    deadline = VerificationDeadline(15)

    with ExitStack() as handles:
        held = handles.enter_context(paths.pin(paths.validate(root)))
        catalog = handles.enter_context(paths.pin_entry(held.path / "bundle.cat"))
        files = {
            name: handles.enter_context(paths.pin_entry(held.path / name))
            for name in ("manifest.json", "approval.json", "payload.exe")
        }
        assert native.signer(catalog, deadline) is None
        members = native.members(catalog, len(files), deadline)
        assert members.keys() == files.keys()
        assert members["manifest.json"] == hashlib.sha256(b"{}").hexdigest()
        assert members["approval.json"] == members["manifest.json"]
        assert native.member_hashes_match(files, members, deadline)
        assert not windows.WindowsCatalogVerifier().verify(catalog, files, deadline).valid
        with pytest.raises(ValueError):
            native.members(catalog, 1, deadline)

    artifact.write_bytes(b"tampered")
    with ExitStack() as handles:
        held = handles.enter_context(paths.pin(paths.validate(root)))
        files = {
            name: handles.enter_context(paths.pin_entry(held.path / name))
            for name in ("manifest.json", "approval.json", "payload.exe")
        }
        assert not native.member_hashes_match(files, members, deadline)


def test_system_dll_loader_ignores_environment_and_limits_search(monkeypatch):
    from raglab.offline.windows_paths import system_dll

    monkeypatch.setenv("SystemRoot", "C:/untrusted")
    handle = object()
    seen = []

    def load(library, **kwargs):
        seen.append((library, kwargs))
        return handle

    monkeypatch.setattr(ctypes, "WinDLL", load)
    assert system_dll("wintrust.dll") is handle
    assert seen == [("wintrust.dll", {"winmode": 0x800, "use_last_error": True})]
