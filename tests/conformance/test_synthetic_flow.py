import json
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from raglab.conformance.synthetic import run_synthetic_conformance


def test_synthetic_flow_closes_every_common_gate(tmp_path: Path) -> None:
    report = run_synthetic_conformance(tmp_path)
    assert report.to_dict() == {
        "authorized_query": True,
        "highlight_hash_verified": True,
        "generation_active": True,
        "revocation_blocked": True,
        "deletion_verified": True,
        "payload_free_log": True,
    }
    with pytest.raises(FrozenInstanceError):
        setattr(report, "authorized_query", False)
    assert "secret" not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_synthetic_flow_logs_only_opaque_stage_metrics(tmp_path: Path) -> None:
    run_synthetic_conformance(tmp_path)
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]

    assert [(event["stage"], event["metrics"]) for event in events] == [
        ("retrieve", {"candidate_count": 1}),
        ("source_load", {"denied_count": 0}),
        ("model", {"denied_count": 0}),
        ("serialize", {"denied_count": 0}),
        ("highlight_open", {"denied_count": 1}),
        ("delete", {"deleted_count": 2}),
    ]
    for event in events:
        assert event.keys() == {
            "event_name", "run_id", "query_id", "document_id", "generation_id",
            "stage", "metrics", "timestamp",
        }
        assert event["event_name"] == "stage_finished"
        for kind in ("run", "query", "document", "generation"):
            identifier = event[f"{kind}_id"]
            assert re.fullmatch(rf"{kind}_[0-9a-f]{{32}}", identifier)
            assert identifier == events[0][f"{kind}_id"]


def test_synthetic_flow_scans_existing_jsonl_fields(tmp_path: Path) -> None:
    run_synthetic_conformance(tmp_path)
    path = tmp_path / "events.jsonl"
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    invalid_event = json.loads(first_line)
    invalid_event["metrics"]["synthetic_content"] = "secret_01"
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(invalid_event) + "\n")

    report = run_synthetic_conformance(tmp_path)

    assert report.generation_active
    assert not report.payload_free_log
