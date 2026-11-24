import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from raglab.observability.events import (
    EventName,
    EventStage,
    JsonlEventWriter,
    OpaqueId,
    OpaqueIdKind,
    SafeEvent,
)


def opaque(kind: OpaqueIdKind, suffix: str = "01") -> OpaqueId:
    return OpaqueId.parse(f"{kind.value}_{suffix:0>32}")


def sample_event(metrics: dict[str, object] | None = None) -> SafeEvent:
    return SafeEvent(
        EventName.STAGE_FINISHED,
        opaque(OpaqueIdKind.RUN),
        opaque(OpaqueIdKind.QUERY),
        opaque(OpaqueIdKind.DOCUMENT),
        opaque(OpaqueIdKind.GENERATION),
        EventStage.RETRIEVE,
        metrics or {},
    )


@pytest.mark.parametrize(
    "key",
    [
        "text", "question", "answer", "file", "file_name", "path", "coordinate",
        "crop", "embedding", "question_count", "unregistered_count",
    ],
)
def test_event_rejects_payload_and_unregistered_fields(key: str) -> None:
    with pytest.raises(ValueError, match="forbidden event field"):
        sample_event({key: 1})


@pytest.mark.parametrize("value", ["42", [42], None, float("nan"), float("inf")])
def test_event_requires_finite_numeric_or_boolean_metrics(value: object) -> None:
    with pytest.raises(ValueError, match="numeric or boolean"):
        sample_event({"duration_ms": value})


@pytest.mark.parametrize(
    "field, value",
    [
        ("event_name", "private_content"),
        ("run_id", "run_01"),
        ("query_id", "query_01"),
        ("document_id", "doc_opaque"),
        ("generation_id", "gen_01"),
        ("stage", "retrieve"),
    ],
)
def test_event_rejects_invalid_identifiers_and_stage(field: str, value: str) -> None:
    fields = {
        "event_name": EventName.STAGE_FINISHED,
        "run_id": opaque(OpaqueIdKind.RUN),
        "query_id": opaque(OpaqueIdKind.QUERY),
        "document_id": opaque(OpaqueIdKind.DOCUMENT),
        "generation_id": opaque(OpaqueIdKind.GENERATION),
        "stage": EventStage.RETRIEVE,
        "metrics": {},
    }
    fields[field] = value
    with pytest.raises(ValueError, match=field):
        SafeEvent(**fields)


def test_writer_emits_one_compact_json_line_with_utc_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "events.jsonl"
    writer = JsonlEventWriter(path)
    metrics = {"duration_ms": 42.5, "candidate_count": 3, "coordinate_validation_failures": False}
    event = sample_event(metrics)
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
        "run_id": opaque(OpaqueIdKind.RUN).value,
        "query_id": opaque(OpaqueIdKind.QUERY).value,
        "document_id": opaque(OpaqueIdKind.DOCUMENT).value,
        "generation_id": opaque(OpaqueIdKind.GENERATION).value,
        "stage": EventStage.RETRIEVE.value,
        "metrics": {"candidate_count": 3, "coordinate_validation_failures": False, "duration_ms": 42.5},
    }
    assert list(record["metrics"]) == sorted(record["metrics"])


def test_writers_append_independent_lines_at_same_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = Path("events.jsonl")

    def write(retry_count: int) -> None:
        JsonlEventWriter(path).write(
            sample_event({"retry_count": retry_count})
        )

    with ThreadPoolExecutor(max_workers=4) as writers:
        list(writers.map(write, range(12)))

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert sorted(record["metrics"]["retry_count"] for record in records) == list(range(12))


@pytest.mark.parametrize("kind", list(OpaqueIdKind))
def test_opaque_id_parse_requires_kind_and_32_lowercase_hex(kind: OpaqueIdKind) -> None:
    with pytest.raises(ValueError, match="invalid opaque identifier"):
        OpaqueId.parse(f"{kind.value}_friendly_slug")


def test_event_requires_typed_ids_of_their_registered_kinds() -> None:
    with pytest.raises(ValueError, match="run_id must be a run OpaqueId"):
        SafeEvent(
            EventName.STAGE_FINISHED,
            opaque(OpaqueIdKind.QUERY),
            opaque(OpaqueIdKind.QUERY),
            opaque(OpaqueIdKind.DOCUMENT),
            opaque(OpaqueIdKind.GENERATION),
            EventStage.RETRIEVE,
        )


def test_event_rejects_string_ids_and_stage() -> None:
    with pytest.raises(ValueError, match="run_id must be a run OpaqueId"):
        SafeEvent(
            EventName.STAGE_FINISHED,
            "run_0123456789abcdef0123456789abcdef",
            opaque(OpaqueIdKind.QUERY),
            opaque(OpaqueIdKind.DOCUMENT),
            opaque(OpaqueIdKind.GENERATION),
            EventStage.RETRIEVE,
        )


@pytest.mark.parametrize("key", ["Duration_ms", "DURATION_MS", "duration_ms "])
def test_metric_keys_require_exact_lowercase_allowlist(key: str) -> None:
    with pytest.raises(ValueError, match="forbidden event field") as error:
        sample_event({key: 1})
    assert str(error.value) == "forbidden event field"


def test_metric_key_collisions_after_casefold_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate metric key"):
        sample_event({"duration_ms": 1, "DURATION_MS": 2})


def test_writer_documents_single_process_scope() -> None:
    assert "single-process" in (JsonlEventWriter.__doc__ or "")
    assert "separate paths" in (JsonlEventWriter.__doc__ or "")
