from pathlib import Path
import os
import shutil
import subprocess

import pytest


ROOT = Path("scripts/offline")


@pytest.mark.parametrize("name", ["install", "uninstall", "test-clean-vm"])
def test_powershell_51_parser_and_policy(name):
    path = ROOT / f"{name}.ps1"
    text = path.read_text(encoding="utf-8-sig").lower()
    for token in ("invoke-webrequest", "invoke-restmethod", "start-bitstransfer", "pip install",
                  "remove-item", "restart-computer", "set-itemproperty"):
        assert token not in text
    engine = shutil.which("powershell.exe")
    if not engine:
        pytest.skip("Windows PowerShell 5.1 is unavailable")
    literal = str(path.absolute()).replace("'", "''")
    command = (
        "$tokens=$null; $errors=$null; "
        f"[void][Management.Automation.Language.Parser]::ParseFile('{literal}', "
        "[ref]$tokens, [ref]$errors); "
        "if ($errors.Count) { $errors | Out-String | Write-Output; exit 1 }"
    )
    result = subprocess.run([engine, "-NoProfile", "-Command", command], capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_guards_and_copy_contract():
    source = (ROOT / "lifecycle.py").read_text(encoding="utf-8")
    for token in ("observe_verified_payload=", "member.stream", "include-system-site-packages = false",
                  "assert_disjoint", "pin_entry", "PENDING_REBOOT"):
        assert token in source


@pytest.fixture
def lifecycle():
    import runpy
    # The same entry point the PowerShell scripts launch, without dispatching CLI.
    return runpy.run_path(str(ROOT / "lifecycle.py"))["install"].__globals__


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file identity")
@pytest.mark.parametrize("partial_removal", [False, True])
def test_mocked_lifecycle_uses_pinned_streams_repairs_and_keeps_foreign_files(
    lifecycle, tmp_path, monkeypatch, partial_removal,
):
    import sys
    from types import SimpleNamespace

    if sys.version_info[:2] != (3, 11):
        pytest.skip("bootstrap requires Python 3.11")
    bundle, install, data = (tmp_path / name for name in ("bundle", "install", "working data"))
    bundle.mkdir()
    expected = {name: name.encode() for name in lifecycle["REQUIRED"]}
    expected.update({p.as_posix(): p.read_bytes() for p in Path("src/raglab").rglob("*.py")})
    expected["configs/security-defaults.json"] = Path("configs/security-defaults.json").read_bytes()
    for profile in ("local16", "local32"):
        name = f"configs/profiles/{profile}.json"
        expected[name] = Path(name).read_bytes()
    for name, content in expected.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def verify(root, _verifier, *, observe_verified_payload):
        from contextlib import ExitStack
        with ExitStack() as pins:
            payload = {name: pins.enter_context(lifecycle["PATHS"].pin(root / name))
                       for name in expected}
            observe_verified_payload(payload, "a" * 64, None, SimpleNamespace(check=lambda: None))
            return lifecycle["BundleReport"]("PASS", "APR-TEST", (), "a" * 64)

    called = []
    def doctor(*_args):
        called.append("doctor")
        return SimpleNamespace(generation_fingerprint="a" * 64)

    monkeypatch.setitem(lifecycle, "verify_bundle", verify)
    def blocked_doctor(argv, *, cwd, **_):
        assert not data.exists()
        assert cwd.name.startswith(".raglab-doctor-")
        assert cwd.parent == data.parent
        assert all((cwd / name).is_dir() for name in ("tmp", "cache", "runs"))
        assert Path(argv[-1]).parent == cwd
        return SimpleNamespace(returncode=5, stdout="BLOCK readiness")

    with monkeypatch.context() as faults:
        faults.setattr(lifecycle["subprocess"], "run", blocked_doctor)
        with pytest.raises(lifecycle["Block"]) as failure:
            lifecycle["install"](bundle, install, data, "Install", "local16")
    assert failure.value.number == 5
    assert set(tmp_path.iterdir()) == {bundle}

    monkeypatch.setitem(lifecycle, "staged_doctor", doctor)
    lifecycle["install"](bundle, install, data, "Install", "local16")
    assert (install / "src/raglab/cli.py").read_bytes() == expected["src/raglab/cli.py"]
    assert not (install / ".venv/Lib/site-packages/pip").exists()
    lifecycle["install"](bundle, install, data, "Install", "local16")
    assert called == ["doctor"]
    assert lifecycle["installed_command"](
        install, data, "config", "validate", "--profile", "local16",
    ) > 0

    (install / "src/raglab/cli.py").write_bytes(b"drift")
    substituted_root = data if partial_removal else install
    displaced = tmp_path / "displaced root"

    def substitute_during_doctor(*_):
        substituted_root.rename(displaced)
        substituted_root.mkdir()
        (substituted_root / "personal.txt").write_bytes(b"keep")
        return SimpleNamespace(generation_fingerprint="a" * 64)

    with monkeypatch.context() as faults:
        faults.setitem(lifecycle, "staged_doctor", substitute_during_doctor)
        with pytest.raises(lifecycle["Block"]):
            lifecycle["install"](bundle, install, data, "Repair", "local16")
    foreign = substituted_root / "personal.txt"
    assert foreign.read_bytes() == b"keep"
    foreign.unlink()
    substituted_root.rmdir()
    displaced.rename(substituted_root)
    lifecycle["install"](bundle, install, data, "Install", "local16")
    assert (install / "src/raglab/cli.py").read_bytes() == expected["src/raglab/cli.py"]
    assert called == ["doctor", "doctor"]

    # The lock must describe every verified member to qualify for a no-op.
    import json
    marker = install / lifecycle["MARKER"]
    lock = json.loads(marker.read_text(encoding="utf-8"))
    optional = next(iter(set(lock["payload"]) - lifecycle["REQUIRED"]))
    lock["payload"].remove(optional)
    marker.write_text(json.dumps(lock), encoding="utf-8")
    lifecycle["install"](bundle, install, data, "Install", "local16")
    assert called == ["doctor", "doctor", "doctor"]
    assert optional in lifecycle["read_json"](marker)["payload"]

    foreign = install / "foreign.txt"
    foreign.write_bytes(b"leave me alone")
    with pytest.raises(lifecycle["Block"]):
        lifecycle["uninstall"](install, data, "KeepAuditReceipts")
    assert foreign.read_bytes() == b"leave me alone"
    foreign.unlink()
    (data / "tmp").mkdir()
    lifecycle["long_path_probe"](data)
    prepare = lifecycle["prepare_removal"]

    def substitute_after_plan(*args):
        result = prepare(*args)
        substituted_root.rename(displaced)
        substituted_root.mkdir()
        (substituted_root / "personal.txt").write_bytes(b"keep")
        return result

    with monkeypatch.context() as faults:
        faults.setitem(lifecycle, "prepare_removal", substitute_after_plan)
        with pytest.raises(lifecycle["Block"]):
            lifecycle["uninstall"](install, data, "KeepAuditReceipts")
    foreign = substituted_root / "personal.txt"
    assert foreign.read_bytes() == b"keep"
    assert data.is_dir()
    foreign.unlink()
    substituted_root.rmdir()
    displaced.rename(substituted_root)

    discard = lifecycle["discard"]

    def interrupted(*args, **kwargs):
        if partial_removal:
            discard(*args, **kwargs)
        raise RuntimeError("interrupted")

    with monkeypatch.context() as faults:
        faults.setitem(lifecycle, "discard", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            lifecycle["uninstall"](install, data, "KeepAuditReceipts")
    assert not install.exists() and not data.exists()
    if partial_removal:
        remaining, = tmp_path.glob(".raglab-remove-*")
        inventory = lifecycle["inventory"](remaining)
        with pytest.raises(lifecycle["RemovalRecoveryRequired"]):
            lifecycle["uninstall"](install, data, "KeepAuditReceipts")
        assert lifecycle["inventory"](remaining) == inventory
        assert not (tmp_path / "working data-uninstall/uninstall-report.json").exists()
        return
    lifecycle["uninstall"](install, data, "KeepAuditReceipts")
    assert not list(tmp_path.glob(".raglab-remove-*"))
    assert not (tmp_path / "working data-uninstall/.removal-plan.json").exists()
    assert not install.exists() and not data.exists()
    receipt = tmp_path / "working data-uninstall/uninstall-report.json"
    assert lifecycle["read_json"](receipt)["negative_lookup"] is True
    lifecycle["uninstall"](install, data, "KeepAuditReceipts")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path validation")
def test_protects_workspace_home_and_drive_root(lifecycle):
    for path in [Path.cwd(), Path.home(), Path(Path.cwd().anchor)]:
        with pytest.raises(lifecycle["Block"]):
            lifecycle["protected_root"](path)


def test_canary_adapter_is_bound_to_operator_and_signed_values(lifecycle, monkeypatch, tmp_path):
    attempts = []

    class SecurityAdapter:
        def verify_denied(self, endpoint, approval):
            attempts.append((endpoint, approval))
            return True

    approved = "https://company.invalid/denial-sink"

    def doctor(args, *, active_canary):
        assert args[args.index("--approval-id") + 1] == "APR-TEST"
        assert not active_canary.verify_denied("https://other.invalid", "APR-TEST")
        assert not active_canary.verify_denied(approved, "APR-OTHER")
        assert active_canary.verify_denied(approved, "APR-TEST")
        return 0

    monkeypatch.setitem(lifecycle, "raglab", doctor)
    lifecycle["active_check"](tmp_path, tmp_path, "local16", "APR-TEST", approved, SecurityAdapter())
    assert attempts == [(approved, "APR-TEST")]

@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell")
def test_powershell_invokes_isolated_bootstrap_with_space_paths(tmp_path):
    engine = shutil.which("powershell.exe")
    if not engine:
        pytest.skip("Windows PowerShell 5.1 is unavailable")
    result = subprocess.run(
        [engine, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "test-clean-vm.ps1"), "-BundlePath", str(tmp_path / "absent bundle"),
         "-OutputRoot", str(tmp_path / "output root")],
        capture_output=True, encoding="utf-8", errors="replace", timeout=15,
    )
    assert result.returncode in {2, 3}, result.stdout + result.stderr
    assert "RAGLAB-ACCEPTANCE-" in result.stderr
    assert not (tmp_path / "output root").exists()

@pytest.mark.skipif(os.name != "nt", reason="requires Windows file identity")
def test_acceptance_requires_new_boot_before_removal(lifecycle, tmp_path, monkeypatch):
    bundle, output = tmp_path / "bundle", tmp_path / "acceptance"
    bundle.mkdir()
    output.mkdir()
    boot = "2026-11-25T00:00:00.0000000Z"
    result = output / "acceptance-report.json"
    armed = {
        "schema_version": 1, "profile": "local16", "status": "PENDING_REBOOT",
        "boot_before": boot, "steps": {}, "generation_fingerprint": "a" * 64,
        "peak_cli_working_set_bytes": 0, "active_elapsed_ms": 0,
    }
    lifecycle["report"](result, armed)
    monkeypatch.setitem(lifecycle, "admin_token", lambda: False)
    monkeypatch.setitem(lifecycle, "vm_snapshot", lambda: {"boot": boot})
    with pytest.raises(lifecycle["Block"]) as failure:
        lifecycle["acceptance"](bundle, output, "local16", True, None, None)
    assert failure.value.number == 5
    assert lifecycle["read_json"](result) == armed


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file identity")
def test_failed_bundle_finalization_discards_stage_without_installing(lifecycle, tmp_path, monkeypatch):
    from contextlib import ExitStack
    from types import SimpleNamespace

    bundle, install, data = (tmp_path / name for name in ("bundle", "install", "data"))
    bundle.mkdir()
    for name in lifecycle["REQUIRED"]:
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    def rejected(source, _verifier, *, observe_verified_payload):
        with ExitStack() as pins:
            payload = {name: pins.enter_context(lifecycle["PATHS"].pin(source / name))
                       for name in lifecycle["REQUIRED"]}
            observe_verified_payload(payload, "a" * 64, None, SimpleNamespace(check=lambda: None))
        return lifecycle["BundleReport"]("BLOCK", None, ())

    monkeypatch.setitem(lifecycle, "verify_bundle", rejected)
    with pytest.raises(lifecycle["Block"]) as failure:
        lifecycle["install"](bundle, install, data, "Install", "local16")
    assert failure.value.number == 4
    assert set(tmp_path.iterdir()) == {bundle}

@pytest.mark.skipif(os.name != "nt", reason="requires the Windows native acceptance probe")
def test_windows_acceptance_probe_runs_through_native_powershell(lifecycle):
    snapshot = lifecycle["vm_snapshot"]()
    assert snapshot["windows_build"].startswith("10.0.")
    assert snapshot["boot"].endswith("Z")
    assert snapshot["defender"] in {"UNKNOWN", "REALTIME_ENABLED", "INACTIVE_OR_PASSIVE"}
    assert snapshot["edr"] in {"UNKNOWN", "SENSE_RUNNING", "SENSE_STOPPED"}


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file identity")
def test_staging_rejects_and_does_not_adopt_foreign_member(lifecycle, tmp_path, monkeypatch):
    from contextlib import ExitStack
    from types import SimpleNamespace

    bundle, install, data = (tmp_path / name for name in ("bundle", "install", "data"))
    bundle.mkdir()
    for name in lifecycle["REQUIRED"]:
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    def verify(source, _verifier, *, observe_verified_payload):
        with ExitStack() as pins:
            payload = {name: pins.enter_context(lifecycle["PATHS"].pin(source / name))
                       for name in lifecycle["REQUIRED"]}
            observe_verified_payload(payload, "a" * 64, None, SimpleNamespace(check=lambda: None))
        return lifecycle["BundleReport"]("PASS", "APR-TEST", (), "a" * 64)

    original = lifecycle["build_venv"]

    def contaminated(stage):
        original(stage)
        (stage.root / "foreign.txt").write_bytes(b"foreign")

    def unexpected_doctor(*_args):
        pytest.fail("doctor ran before stage validation")

    monkeypatch.setitem(lifecycle, "verify_bundle", verify)
    monkeypatch.setitem(lifecycle, "build_venv", contaminated)
    monkeypatch.setitem(lifecycle, "staged_doctor", unexpected_doctor)
    with pytest.raises((lifecycle["Block"], OSError)):
        lifecycle["install"](bundle, install, data, "Install", "local16")
    stage, = tmp_path.glob(".raglab-build-*")
    foreign, = stage.iterdir()
    assert foreign.name == "foreign.txt"
    assert foreign.read_bytes() == b"foreign"
    assert not install.exists() and not data.exists()

@pytest.mark.skipif(os.name != "nt", reason="requires Windows file identity")
def test_forged_removal_plan_cannot_authorize_foreign_tree_deletion(lifecycle, tmp_path):
    install, data, receipt = (tmp_path / name for name in ("foreign install", "foreign data", "foreign data-uninstall"))
    for root in (install, data, receipt):
        root.mkdir()
    for root in (install, data):
        (root / "personal.txt").write_bytes(b"keep")
    identity = lifecycle["removal_identity"]
    check = lifecycle["check_path"]
    lifecycle["report"](receipt / lifecycle["REMOVAL_PLAN"], {
        "description": {"schema_version": 1, "generation_fingerprint": "a" * 64,
                        "installed_objects": 1, "working_objects": 1},
        "policy": "Purge",
        "roots": [str(install), str(data)],
        "quarantine": [".raglab-remove-" + char * 32 for char in "ab"],
        "identities": [identity(check(root)) for root in (install, data)],
        "members": [{"personal.txt": identity(check(root / "personal.txt"))}
                    for root in (install, data)],
    })
    with pytest.raises(lifecycle["Block"]):
        lifecycle["uninstall"](install, data, "Purge")
    assert [(root / "personal.txt").read_bytes() for root in (install, data)] == [b"keep", b"keep"]
