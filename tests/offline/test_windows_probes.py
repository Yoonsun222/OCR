import json
import os
import subprocess
from datetime import datetime, timezone
from uuid import UUID

import pytest

from raglab.offline import windows
from raglab.offline.config import load_profile_config
from raglab.offline.windows_paths import WindowsLocalPaths


def test_powershell_uses_fixed_command_and_timeout(monkeypatch, tmp_path):
    system = tmp_path / "System32"
    monkeypatch.setattr(windows, "_system_directory", lambda: system)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'{"supported":true}', b"")

    monkeypatch.setattr(windows.subprocess, "run", run)
    assert windows._powershell_json(windows._SYSTEM_SCRIPT) == {"supported": True}
    args, options = calls[0]
    assert args == [str(system / "WindowsPowerShell/v1.0/powershell.exe"),
                    "-NoProfile", "-NonInteractive", "-Command", windows._SYSTEM_SCRIPT]
    assert options["timeout"] == 10


@pytest.mark.parametrize("stdout", [b"{bad", b"[]", b'{"a":1,"a":2}', b'{"a":NaN}'])
def test_malformed_powershell_json_blocks(monkeypatch, tmp_path, stdout):
    monkeypatch.setattr(windows, "_system_directory", lambda: tmp_path / "System32")
    monkeypatch.setattr(windows.subprocess, "run",
                        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout, b""))
    with pytest.raises(ValueError):
        windows._powershell_json(windows._SYSTEM_SCRIPT)


def test_native_query_timeout_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(windows, "_system_directory", lambda: tmp_path / "System32")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("powershell", 10)

    monkeypatch.setattr(windows.subprocess, "run", timeout)
    assert windows.WindowsEgressPolicyProbe().inspect().status == "UNKNOWN"


@pytest.mark.parametrize("change", [
    {}, {"enabled_outbound_allows": 1}, {"default_deny_profiles": 2},
    {"firewall_services_running": False}, {"active_profiles_known": False},
    {"policy_exemptions_absent": False},
])
def test_egress_proof_requires_all_profiles_zero_allows_and_trusted_proxy(monkeypatch, change):
    policy = dict(firewall_services_running=True, default_deny_profiles=3,
                  enabled_outbound_allows=0, active_profiles_known=True,
                  policy_exemptions_absent=True)
    policy.update(change)
    monkeypatch.setattr(windows, "_powershell_json", lambda _: json.loads(json.dumps(policy)))
    monkeypatch.setattr(windows, "_windows_proxy_is_direct", lambda: True)
    monkeypatch.setattr(windows, "_machine_isolation_is_attested", lambda: True, raising=False)
    for key in windows._PROXY_VARIABLES:
        monkeypatch.delenv(key, raising=False)
    probe = windows.WindowsEgressPolicyProbe()
    assert probe.inspect().status == ("UNKNOWN" if change else "DENIED")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.test:443")
    assert probe.inspect().status == "UNKNOWN"


def test_passive_firewall_facts_are_insufficient_without_machine_attestation(monkeypatch):
    monkeypatch.setattr(windows, "_powershell_json", lambda _: dict(
        firewall_services_running=True, default_deny_profiles=3, enabled_outbound_allows=0,
        active_profiles_known=True, policy_exemptions_absent=True,
    ))
    monkeypatch.setattr(windows, "_windows_proxy_is_direct", lambda: True)
    monkeypatch.setattr(windows, "_machine_isolation_is_attested", lambda: False, raising=False)
    for key in windows._PROXY_VARIABLES:
        monkeypatch.delenv(key, raising=False)
    assert windows.WindowsEgressPolicyProbe().inspect().status == "UNKNOWN"


@pytest.mark.parametrize("overrides,expected", [
    ({}, True),
    ({"schema_version": True}, False),
    ({"scope": "firewall-only"}, False),
    ({"expires_at": "2030-01-01T00:00:00Z"}, False),
    ({"expires_at": "2031-01-01T00:00:00"}, False),
    ({"expires_at": "2031-01-01Z"}, False),
    ({"unexpected": True}, False),
])
def test_machine_attestation_requires_current_full_isolation_with_strict_schema(overrides, expected):
    policy = dict(schema_version=1, scope="full-workstation-egress-deny", expires_at="2031-01-01T00:00:00Z")
    policy.update(overrides)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert windows._isolation_attestation_valid(policy, now) is expected


@pytest.mark.parametrize("flags,expected", [
    ({}, True),
    ({"AutoDetect": True}, False),
    ({"ProxyIsEnabled": True}, False),
    ({"AutoConfigIsEnabled": True}, False),
    ({"Proxy": "proxy.example.test"}, False),
    ({"AutoDetect": 0}, False),
])
def test_advanced_winhttp_refuses_proxy_routes_and_unknown_settings(monkeypatch, tmp_path, flags, expected):
    system = tmp_path / "System32"
    monkeypatch.setattr(windows, "_system_directory", lambda: system)
    settings = dict(ProxyIsEnabled=False, AutoConfigIsEnabled=False, AutoDetect=False, PerUserProxySettings=True)
    settings.update(flags)

    def run(args, **kwargs):
        assert args == [str(system / "netsh.exe"), "winhttp", "show", "advproxy"]
        assert kwargs["timeout"] == 10
        output = b"\x91localized header\r\n" + json.dumps(settings).encode("utf-8")
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(windows.subprocess, "run", run)
    assert windows._advanced_winhttp_proxy_is_direct() is expected


@pytest.mark.skipif(os.name != "nt", reason="Native Windows filesystem smoke checks")
def test_unicode_long_path_round_trip_leaves_no_derivatives(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(windows, "_powershell_json", lambda _: {
        "supported": True, "physical_bytes": 16 * 1024**3, "available_bytes": 12 * 1024**3,
    })
    probe = windows.WindowsSystemProbe(load_profile_config("local16"), WindowsLocalPaths())
    snapshot = probe.snapshot()
    assert snapshot.paths_writable and snapshot.long_unicode_path
    assert snapshot.atomic_write_cleanup and snapshot.derivative_deleted
    for root in tmp_path.iterdir():
        assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Native Windows filesystem smoke checks")
@pytest.mark.parametrize("atomic", [True, False])
def test_failed_exclusive_probe_preserves_file_added_by_another_writer(monkeypatch, tmp_path, atomic):
    token = UUID(int=7)
    monkeypatch.setattr(windows, "uuid4", lambda: token)
    borrowed = tmp_path / f".raglab-doctor-{token.hex}.json"
    borrowed.write_bytes(b"other writer owns this")
    probe = windows.WindowsSystemProbe(load_profile_config("local16"), WindowsLocalPaths())
    with pytest.raises(OSError):
        probe._round_trip(tmp_path, atomic=atomic)
    assert borrowed.read_bytes() == b"other writer owns this"
    assert list(tmp_path.iterdir()) == [borrowed]
