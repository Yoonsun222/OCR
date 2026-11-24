import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from raglab.observability.events import EventName, JsonlEventWriter, SafeEvent


@pytest.mark.parametrize(
    "key",
    [
        "text", "question", "answer", "file", "file_name", "path", "coordinate",
        "crop", "embedding", "question_count", "unregistered_count",
    ],
)
def test_event_rejects_payload_and_unregistered_fields(key: str) -> None:
    with pytest.raises(ValueError, match="forbidden event field"):
        SafeEvent(EventName.STAGE_FINISHED, "run_01", "query_01", "doc_opaque",
                  "gen_01", "stage", {key: 1})


@pytest.mark.parametrize("value", ["42", [42], None, float("nan"), float("inf")])
def test_event_requires_finite_numeric_or_boolean_metrics(value: object) -> None:
    with pytest.raises(ValueError, match="numeric or boolean"):
        SafeEvent(EventName.STAGE_FINISHED, "run_01", "query_01", "doc_opaque",
                  "gen_01", "retrieve", {"duration_ms": value})


@pytest.mark.parametrize(
    "field, value",
    [
        ("event_name", "private_content"),
        ("run_id", "run_01\nrun_02"),
        ("query_id", "synthetic question?"),
        ("document_id", "synthetic.pdf"),
        ("generation_id", "synthetic/path"),
        ("stage", "retrieve\nserialize"),
    ],
)
def test_event_rejects_invalid_identifiers_and_stage(field: str, value: str) -> None:
    fields = {
        "event_name": EventName.STAGE_FINISHED,
        "run_id": "run_01",
        "query_id": "query_01",
        "document_id": "doc_opaque",
        "generation_id": "gen_01",
        "stage": "retrieve",
        "metrics": {},
    }
    fields[field] = value
    with pytest.raises(ValueError, match=field):
        SafeEvent(**fields)


def test_writer_emits_one_compact_json_line_with_utc_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "events.jsonl"
    writer = JsonlEventWriter(path)
    metrics = {"duration_ms": 42.5, "candidate_count": 3, "coordinate_validation_failures": False}
    event = SafeEvent(EventName.STAGE_FINISHED, "run_01", "query_01", "doc_opaque",
                      "gen_01", "retrieve", metrics)
    metrics["file_name"] = "synthetic.pdf"

    before = datetime.now(UTC)
    writer.write(event)
    after = datetime.now(UTC)

    raw = path.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1 and b"\r" not in raw
    record = json.loads(raw)
    assert raw == json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    timestamp = datetime.fromisoformat(record.pop("timestamp"))
    assert timestamp.tzinfo == UTC and before <= timestamp <= after
    assert record == {
        "event_name": "stage_finished",
        "run_id": "run_01",
        "query_id": "query_01",
        "document_id": "doc_opaque",
        "generation_id": "gen_01",
        "stage": "retrieve",
        "metrics": {"candidate_count": 3, "coordinate_validation_failures": False, "duration_ms": 42.5},
    }
    assert list(record["metrics"]) == sorted(record["metrics"])


def test_writers_append_independent_lines_at_same_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = Path("events.jsonl")

    def write(retry_count: int) -> None:
        JsonlEventWriter(path).write(
            SafeEvent(EventName.STAGE_FINISHED, "run_01", "query_01", "doc_opaque",
                      "gen_01", "retrieve", {"retry_count": retry_count})
        )

    with ThreadPoolExecutor(max_workers=4) as writers:
        list(writers.map(write, range(12)))

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert sorted(record["metrics"]["retry_count"] for record in records) == list(range(12))
