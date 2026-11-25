import json
from dataclasses import replace

from raglab.cli import main
from raglab.offline.repro import build_repro_bundle
from test_repro import repro_inputs, pytestmark  # noqa: F401


def test_verify_only_uses_validity_exit_codes(tmp_path, capsys):
    bundle = tmp_path / "private-operator"
    assert build_repro_bundle(repro_inputs(), bundle).status == "PASS"
    args = ["reproduce", "--bundle", str(bundle), "--verify-only"]
    assert main(args) == 0
    assert main(["reproduce", "--bundle", str(bundle / ".." / bundle.name), "--verify-only"]) == 7
    (bundle / "metrics.json").write_bytes(b"{}")
    assert main(args) == 7
    output = capsys.readouterr()
    assert "PASS" in output.out and "BLOCK" in output.out
    assert "private-operator" not in output.out + output.err


def test_environment_diff_projects_only_verified_change_fields(tmp_path, capsys):
    left, right = tmp_path / "left", tmp_path / "right"
    base = repro_inputs()
    newer = replace(base, environment={**base.environment, "os_build": "10.0.26200", "locale": "en-US"})
    assert build_repro_bundle(base, left).status == "PASS"
    assert build_repro_bundle(newer, right).status == "PASS"
    args = ["reproduce", "--left", str(left), "--right", str(right), "--diff-env"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "locale": {"left": "ko-KR", "right": "en-US"},
        "os_build": {"left": "10.0.26100", "right": "10.0.26200"},
    }
    (right / "environment.json").write_bytes(b'{"cpu":"private-user"}')
    assert main(args) == 7
    assert "private-user" not in capsys.readouterr().out


def test_invalid_cli_mode_does_not_print_operands(capsys):
    assert main(["reproduce", "--bundle", "secret.pdf", "--diff-env"]) == 2
    assert "secret.pdf" not in capsys.readouterr().err
