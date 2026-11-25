from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from raglab.cli import main


REPORT = Path(__file__).parents[1] / "fixtures" / "doctor-pass.json"


def _current_report():
    value = json.loads(REPORT.read_bytes())
    now = datetime.now(timezone.utc)
    value["generated_at"] = (now - timedelta(minutes=1)).isoformat()
    value["bundle_approval_expires_at"] = (now + timedelta(hours=1)).isoformat()
    return value


def _arguments(report, output):
    return ["demo", "--profile", "local16", "--doctor-report", str(report), "--output", str(output)]


def test_demo_cli_prints_answer_and_relative_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "doctor.json"
    report.write_text(json.dumps(_current_report()), encoding="utf-8")
    output = tmp_path / "output"
    assert main(_arguments(report, output)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "PART A는 합성 도면 오른쪽에 있습니다.",
        "result.json", "overlay.svg", "summary.html", "events.jsonl", "deletion-smoke-report.json",
    ]
    before = {item.name: item.read_bytes() for item in output.iterdir()}
    assert main(_arguments(report, output)) == 2
    assert {item.name: item.read_bytes() for item in output.iterdir()} == before


@pytest.mark.parametrize("case", [
    "missing", "oversized", "bad-json", "duplicate", "nested", "stale", "future", "expired",
    "profile", "block", "check-block", "fingerprint", "incomplete", "unexpected-field", "boolean-schema",
])
def test_demo_cli_blocks_unusable_doctor_report(tmp_path, monkeypatch, capsys, case):
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "private-doctor.json"
    output = tmp_path / "output"
    value = _current_report()
    now = datetime.now(timezone.utc)
    if case == "stale":
        value["generated_at"] = (now - timedelta(hours=25)).isoformat()
    elif case == "future":
        value["generated_at"] = (now + timedelta(minutes=1)).isoformat()
    elif case == "expired":
        value["bundle_approval_expires_at"] = now.isoformat()
    elif case == "profile":
        value["profile"] = "local32"
    elif case == "block":
        value["overall_status"] = "BLOCK"
    elif case == "check-block":
        value["checks"][0]["status"] = "BLOCK"
    elif case == "fingerprint":
        value["generation_fingerprint"] = "private-data"
    elif case == "incomplete":
        value["checks"].pop()
    elif case == "unexpected-field":
        value["private-field"] = "private-data"
    elif case == "boolean-schema":
        value["schema_version"] = True

    if case != "missing":
        data = {
            "oversized": b" " * 100_000,
            "bad-json": b"private-data",
            "duplicate": b'{"schema_version":1,"schema_version":1}',
            "nested": b"[" * 2_000 + b"]" * 2_000,
        }.get(case, json.dumps(value).encode("utf-8"))
        report.write_bytes(data)
    assert main(_arguments(report, output)) == 5
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-" not in captured.err
    assert not output.exists()
