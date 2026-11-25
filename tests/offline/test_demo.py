from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from raglab.offline import demo


REPORT = Path(__file__).parents[1] / "fixtures" / "doctor-pass.json"


@pytest.fixture
def passing_doctor():
    report = demo.read_doctor_report(REPORT)
    now = datetime.fromisoformat(report.generated_at) + timedelta(minutes=5)
    return report, lambda: now


def test_demo_returns_reproducible_part_a_region(passing_doctor, tmp_path):
    report, clock = passing_doctor
    result = demo.run_synthetic_demo(demo.DemoContext(report, tmp_path, clock=clock))
    assert result.answer == "PART A는 합성 도면 오른쪽에 있습니다."
    assert result.regions[0].bbox == (0.6, 0.3, 0.24, 0.3)
    assert result.regions[0].artifact_hash == result.artifact_hash
    assert result.artifact_hash == sha256((demo.FIXTURE_DIRECTORY / "source.svg").read_bytes()).hexdigest()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "deletion-smoke-report.json", "events.jsonl", "overlay.svg", "result.json", "summary.html",
    ]
    result_json = json.loads((tmp_path / "result.json").read_bytes())
    assert result_json["regions"][0]["bbox"] == list(result.regions[0].bbox)
    assert result_json["overlay_path"] == "overlay.svg"
    overlay = ET.parse(tmp_path / "overlay.svg").getroot()
    highlight = overlay.find("{http://www.w3.org/2000/svg}rect[@id='evidence-part-a']")
    assert [highlight.attrib[key] for key in ("x", "y", "width", "height")] == ["600", "180", "240", "180"]
    html = (tmp_path / "summary.html").read_text(encoding="utf-8")
    for external_load in ("<script", "<img", "src=", "href=", "://", "@font-face"):
        assert external_load not in html
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_bytes().splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert {event["run_id"] for event in events} == {result.run_id}
    deletion = json.loads((tmp_path / "deletion-smoke-report.json").read_bytes())
    assert deletion["run_id"] == result.run_id
    assert deletion["status"] == "PASS"


@pytest.mark.parametrize("name", ["source.svg", "expected.json"])
def test_modified_fixture_is_blocked_before_output(passing_doctor, tmp_path, monkeypatch, name):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    for path in demo.FIXTURE_DIRECTORY.iterdir():
        (fixture / path.name).write_bytes(path.read_bytes())
    (fixture / name).write_bytes(b"<!DOCTYPE svg [<!ENTITY injected SYSTEM 'file:///sensitive'>]>")
    monkeypatch.setattr(demo, "FIXTURE_DIRECTORY", fixture)
    report, clock = passing_doctor
    output = tmp_path / "output"
    with pytest.raises(demo.OperatorError) as failure:
        demo.run_synthetic_demo(demo.DemoContext(report, output, clock=clock))
    assert failure.value.exit_code == 6
    assert not output.exists()


def test_direct_gate_rejects_incomplete_report(passing_doctor, tmp_path):
    report, clock = passing_doctor
    report = replace(report, checks=report.checks[:-1])
    with pytest.raises(demo.OperatorError) as failure:
        demo.run_synthetic_demo(demo.DemoContext(report, tmp_path, clock=clock))
    assert failure.value.exit_code == 5
    assert not list(tmp_path.iterdir())


def test_evidence_mismatch_blocks_even_with_matching_fixture_hashes(passing_doctor, tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    source = (demo.FIXTURE_DIRECTORY / "source.svg").read_bytes()
    expected = json.loads((demo.FIXTURE_DIRECTORY / "expected.json").read_bytes())
    expected["bbox"][0] = 0.5
    (fixture / "source.svg").write_bytes(source)
    (fixture / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.setattr(demo, "FIXTURE_DIRECTORY", fixture)
    monkeypatch.setattr(demo, "FIXTURE_HASHES", tuple(
        (name, sha256((fixture / name).read_bytes()).hexdigest()) for name, _ in demo.FIXTURE_HASHES
    ))
    report, clock = passing_doctor
    output = tmp_path / "output"
    with pytest.raises(demo.OperatorError) as failure:
        demo.run_synthetic_demo(demo.DemoContext(report, output, clock=clock))
    assert failure.value.exit_code == 6
    assert not output.exists()


def test_occupied_final_marker_rolls_back_owned_outputs(passing_doctor, tmp_path):
    report, clock = passing_doctor
    marker = tmp_path / "result.json"
    marker.write_bytes(b"an unrelated file")
    with pytest.raises(demo.OperatorError) as failure:
        demo.run_synthetic_demo(demo.DemoContext(report, tmp_path, clock=clock))
    assert failure.value.exit_code == 2
    assert list(tmp_path.iterdir()) == [marker]
    assert marker.read_bytes() == b"an unrelated file"
