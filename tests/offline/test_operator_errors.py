import json
from dataclasses import FrozenInstanceError

import pytest

from raglab import cli
from raglab.offline.errors import OperatorError


def sample_error():
    return OperatorError(
        code="RAGLAB-MEM-002", stage="demo", summary="profile memory cap exceeded",
        observed="13.1 GiB", expected="<=12 GiB", protected_state="no index changed",
        retry_safe=True, next_command=r".\raglab.ps1 demo --profile local16",
        docs="docs/operator/ERROR-CATALOG.md#raglab-mem-002", exit_code=6,
    )


def test_error_keeps_operator_action_and_retry_safety():
    error = sample_error()
    with pytest.raises(OperatorError) as raised:
        raise error
    assert raised.value.retry_safe is True
    assert raised.value.next_command.startswith(r".\raglab.ps1")
    with pytest.raises(FrozenInstanceError):
        error.summary = "changed"


def test_cli_reports_known_operator_error(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    def fail(args):
        raise sample_error()

    monkeypatch.setattr(cli, "_config_command", fail)
    assert cli.main(["config", "validate", "--profile", "local16"]) == 6
    err = capsys.readouterr().err
    for expected in ("RAGLAB-MEM-002", "no index changed", r".\raglab.ps1 demo", "logs"):
        assert expected in err
    [report] = list((tmp_path / "logs").rglob("*.json"))
    assert json.loads(report.read_text(encoding="utf-8"))["retry_safe"] is True


@pytest.mark.parametrize("exception_type", [RuntimeError, ValueError, OSError])
def test_internal_error_traceback_excludes_message_locals_and_source(
    exception_type, monkeypatch, tmp_path, capsys,
):
    monkeypatch.chdir(tmp_path)

    def fail(*args, **kwargs):
        secret_local = "sentinel-local"
        raise exception_type("sentinel-source", secret_local, r"C:\Users\Alice\secret.pdf")

    monkeypatch.setattr(cli.offline_config, "load_profile_config", fail)
    assert cli.main(["config", "validate", "--profile", "local16"]) == 10
    err = capsys.readouterr().err
    assert "RAGLAB-INTERNAL-001" in err
    [report] = list((tmp_path / "logs").rglob("*.json"))
    content = report.read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["traceback"]
    for forbidden in ("sentinel", "Alice", "secret.pdf", str(tmp_path)):
        assert forbidden not in content + err


def test_cli_preserves_exit_code_when_diagnostics_cannot_be_written(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").write_text("already in use", encoding="utf-8")

    def fail(args):
        raise sample_error()

    monkeypatch.setattr(cli, "_config_command", fail)
    assert cli.main(["config", "validate", "--profile", "local16"]) == 6
    assert "Diagnostics unavailable" in capsys.readouterr().err
    assert (tmp_path / "logs").read_text(encoding="utf-8") == "already in use"


def test_internal_diagnostics_never_write_inside_source_bundle(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "logs"
    bundle.mkdir()

    def fail(*args):
        raise RuntimeError("private detail")

    monkeypatch.setattr(cli, "_verify_command", fail)
    assert cli.main(["verify-bundle", "--bundle", str(bundle)]) == 10
    err = capsys.readouterr().err
    assert "Diagnostics unavailable" in err
    assert str(bundle) not in err
    assert list(bundle.iterdir()) == []
