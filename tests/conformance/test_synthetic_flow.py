import json
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from raglab.conformance.synthetic import run_synthetic_conformance
from raglab.observability.events import EventStage, JsonlEventWriter, SafeEvent
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore


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


@pytest.mark.parametrize("skip_retry_writes", [False, True])
def test_synthetic_retry_verifies_only_its_own_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_retry_writes: bool,
) -> None:
    run_synthetic_conformance(tmp_path)
    prior_path = tmp_path / "events.jsonl"
    prior_events = [json.loads(line) for line in prior_path.read_text(encoding="utf-8").splitlines()]
    prior_events[0]["metrics"]["candidate_count"] = "secret_01"
    prior_log = "".join(json.dumps(event) + "\n" for event in prior_events)
    prior_path.write_text(prior_log, encoding="utf-8")

    if skip_retry_writes:
        monkeypatch.setattr(JsonlEventWriter, "write", lambda self, event: None)

    report = run_synthetic_conformance(tmp_path)

    assert report.generation_active
    assert report.payload_free_log is (not skip_retry_writes)
    assert prior_path.read_text(encoding="utf-8") == prior_log
    retry_path, = set(tmp_path.glob("*.jsonl")) - {prior_path}
    retry_lines = retry_path.read_text(encoding="utf-8").splitlines()
    if skip_retry_writes:
        assert retry_lines == []
    else:
        assert len(retry_lines) == len(prior_events)
        run_id = json.loads(retry_lines[0])["run_id"]
        assert run_id != prior_events[0]["run_id"]
        assert run_id in retry_path.name
        assert "secret_01" not in retry_lines[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metrics", {"candidate_count": "secret_01"}),
        ("metrics", {"candidate_count": True}),
        ("query_id", "query_" + "0" * 32),
        ("timestamp", "2026-11-24T00:00:00"),
    ],
)
def test_synthetic_flow_checks_persisted_event_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object,
) -> None:
    write = JsonlEventWriter.write

    def write_changed_event(writer: JsonlEventWriter, event: SafeEvent) -> None:
        write(writer, event)
        if event.stage is EventStage.RETRIEVE:
            record = json.loads(writer.path.read_text(encoding="utf-8"))
            record[field] = value
            line = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
            writer.path.write_text(line + "\n", encoding="utf-8", newline="\n")

    monkeypatch.setattr(JsonlEventWriter, "write", write_changed_event)

    assert not run_synthetic_conformance(tmp_path).payload_free_log


def test_synthetic_flow_requires_the_guard_to_honor_the_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def policy_only(store: MemoryPolicyStore, _serving_blocked: object) -> AuthorizationGuard:
        return AuthorizationGuard.policy_only(store)

    monkeypatch.setattr(AuthorizationGuard, "deletion_managed", staticmethod(policy_only))

    report = run_synthetic_conformance(tmp_path)

    assert report.authorized_query
    assert report.revocation_blocked
    assert not report.deletion_verified
