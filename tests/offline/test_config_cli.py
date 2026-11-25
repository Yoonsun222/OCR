import hashlib
import json
import os

import pytest

from raglab.cli import main


def save_run(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def explain(path, *, profile="local16", run=None):
    args = ["config", "explain", "--profile", profile, "--output", str(path)]
    if run is not None:
        args += ["--run", str(run)]
    return main(args)


def diff(left, right):
    return main(["config", "diff", "--left", str(left), "--right", str(right)])


@pytest.mark.parametrize("profile", ["local16", "local32"])
def test_validate_reports_valid_shipped_profile(profile, capsys):
    assert main(["config", "validate", "--profile", profile]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "PASS"
    assert report["issues"] == []


@pytest.mark.parametrize("value,exit_code", [
    ({"private-operator-key": "private-secret"}, 2),
    ({"temp_root": "../private-secret"}, 2),
    ({"visual_candidates": 99}, 2),
    ({"network_mode": "allow"}, 3),
    ({"diagnostic_content": True}, 3),
])
def test_validate_exit_codes_and_redaction(tmp_path, capsys, value, exit_code):
    run = save_run(tmp_path / "private-operator.json", value)
    assert main(["config", "validate", "--profile", "local16", "--run", str(run)]) == exit_code
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "BLOCK"
    assert str(run) not in captured.out + captured.err
    assert "private-operator" not in captured.out + captured.err
    assert "private-secret" not in captured.out + captured.err


@pytest.mark.parametrize("text", [
    '{"network_mode":"deny","network_mode":"allow"}',
    '{"rerank_candidates":NaN}',
    '["private-secret"]',
    '{"private-secret"',
])
def test_config_json_rejects_malformed_and_ambiguous_input(tmp_path, capsys, text):
    run = tmp_path / "synthetic.json"
    run.write_text(text, encoding="utf-8")
    assert main(["config", "validate", "--profile", "local16", "--run", str(run)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "BLOCK"
    assert "private-secret" not in output.out + output.err


def test_explain_is_atomic_deterministic_and_redacts_every_path(tmp_path, capsys):
    private_path = r"C:\Users\Synthetic_Operator\private-target"
    experiment = save_run(tmp_path / "path-input.json", {
        "temp_root": private_path, "output_root": "relative-private-target", "rerank_candidates": 8,
    })
    output = tmp_path / "reports" / "effective-config.json"
    assert explain(output, run=experiment) == 0
    original = output.read_bytes()
    assert explain(output, run=experiment) == 0
    assert output.read_bytes() == original
    document = json.loads(original)
    assert document["schema_version"] == 1
    assert document["status"] == "PASS"
    assert document["values"]["network_mode"] == {
        "value": "deny", "source_layer": "security", "locked": True,
    }
    assert document["values"]["rerank_candidates"] == {
        "value": 8, "source_layer": "run", "locked": False,
    }
    fingerprint = hashlib.sha256(private_path.encode("utf-8")).hexdigest()
    assert document["values"]["temp_root"] == {
        "value": f"<path:{fingerprint}>", "source_layer": "run", "locked": False,
    }
    captured = capsys.readouterr()
    assert b"private-target" not in original
    assert b"Synthetic_Operator" not in original
    assert str(output) not in captured.out + captured.err
    assert list(output.parent.iterdir()) == [output]


def test_explain_does_not_overwrite_input_even_for_invalid_config(tmp_path, capsys):
    run = tmp_path / "run.json"
    run.write_bytes(b"invalid json")
    output = tmp_path / "aliased.json"
    os.link(run, output)
    assert explain(output, run=run) == 2
    assert run.read_bytes() == output.read_bytes() == b"invalid json"
    assert str(run) not in capsys.readouterr().err


def test_diff_ignores_provenance_tracks_value_changes_and_prints_ordered_rows(tmp_path, capsys):
    left = tmp_path / "left.json"
    middle = tmp_path / "middle.json"
    right = tmp_path / "right.json"
    defaults = save_run(tmp_path / "restated.json", {"rerank_candidates": 20})
    assert explain(left) == 0
    assert explain(middle, run=defaults) == 0
    assert explain(right, profile="local32") == 0
    capsys.readouterr()
    assert diff(left, middle) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert diff(left, right) == 2
    patch = json.loads(capsys.readouterr().out)
    assert patch == [
        {"op": "replace", "path": "/interactive_concurrency", "value": 2},
        {"op": "replace", "path": "/max_working_set_gib", "value": 26},
        {"op": "replace", "path": "/profile", "value": "local32"},
        {"op": "replace", "path": "/rerank_candidates", "value": 30},
        {"op": "replace", "path": "/visual_candidates", "value": 5},
    ]


def test_diff_detects_distinct_redacted_paths_without_echo(tmp_path, capsys):
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    for output, hidden in [(left, "private-one"), (right, "private-two")]:
        run = save_run(tmp_path / "experiment.json", {"cache_root": f"C:/Users/{hidden}/cache"})
        assert explain(output, run=run) == 0
    capsys.readouterr()
    assert diff(left, right) == 2
    captured = capsys.readouterr()
    (row,) = json.loads(captured.out)
    assert row["path"] == "/cache_root"
    assert row["value"].startswith("<path:")
    assert "private-" not in captured.out + captured.err


@pytest.mark.parametrize("corruption", ["raw path", "unsafe provenance", "unknown field", "blocked status"])
def test_diff_rejects_untrusted_report_schema_without_echo(tmp_path, capsys, corruption):
    baseline = tmp_path / "effective.json"
    assert explain(baseline) == 0
    forged = tmp_path / "invalid.json"
    content = json.loads(baseline.read_text(encoding="utf-8"))
    if corruption == "raw path":
        content["values"]["cache_root"]["value"] = r"C:\Users\private-report"
    elif corruption == "unsafe provenance":
        content["values"]["cache_root"]["source_layer"] = r"C:\Users\private-report"
    elif corruption == "unknown field":
        content["values"]["private-report"] = content["values"]["cache_root"]
    else:
        content["status"] = "BLOCK"
    forged.write_text(json.dumps(content), encoding="utf-8")
    capsys.readouterr()
    assert diff(baseline, forged) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-report" not in output.err


@pytest.mark.parametrize("field,excess", [
    ("max_working_set_gib", 13), ("interactive_concurrency", 2),
    ("rerank_candidates", 21), ("visual_candidates", 4),
])
def test_diff_rejects_report_exceeding_selected_profile_ceiling(tmp_path, capsys, field, excess):
    baseline = tmp_path / "baseline.json"
    over_budget = tmp_path / "over-budget.json"
    assert explain(baseline) == 0
    document = json.loads(baseline.read_text(encoding="utf-8"))
    document["values"][field]["value"] = excess
    over_budget.write_text(json.dumps(document), encoding="utf-8")
    capsys.readouterr()
    assert diff(baseline, over_budget) == 2
    assert capsys.readouterr().out == ""
