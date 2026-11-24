import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from raglab.observability.events import (
    EventName,
    EventStage,
    JsonlEventWriter,
    OpaqueId,
    OpaqueIdKind,
    OpaqueIdRegistry,
    SafeEvent,
)
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore


ID_REGISTRY = OpaqueIdRegistry()


def opaque(kind: OpaqueIdKind, suffix: str = "01") -> OpaqueId:
    source_prefix = {
        OpaqueIdKind.RUN: "run",
        OpaqueIdKind.QUERY: "query",
        OpaqueIdKind.DOCUMENT: "doc",
        OpaqueIdKind.GENERATION: "gen",
    }[kind]
    return ID_REGISTRY.map(kind, f"{source_prefix}_{suffix}")


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


def test_opaque_id_has_no_public_parse_or_issue_constructor() -> None:
    assert not hasattr(OpaqueId, "parse")
    assert not hasattr(OpaqueId, "issue")
    with pytest.raises(TypeError):
        OpaqueId(OpaqueIdKind.DOCUMENT, "document_0123456789abcdef0123456789abcdef")


def test_registry_maps_legacy_ids_stably_without_exposing_source_values(tmp_path: Path) -> None:
    registry = OpaqueIdRegistry()
    mapped = registry.map(OpaqueIdKind.DOCUMENT, "doc_01")
    assert registry.map(OpaqueIdKind.DOCUMENT, "doc_01") is mapped
    assert mapped.value != "doc_01"

    event = SafeEvent(
        EventName.STAGE_FINISHED,
        registry.map(OpaqueIdKind.RUN, "run_01"),
        registry.map(OpaqueIdKind.QUERY, "query_01"),
        mapped,
        registry.map(OpaqueIdKind.GENERATION, "gen_01"),
        EventStage.RETRIEVE,
    )
    source_ids = ("run_01", "query_01", "doc_01", "gen_01")
    event_ids = (event.run_id.value, event.query_id.value, event.document_id.value, event.generation_id.value)
    assert all(event_id != source_id for event_id, source_id in zip(event_ids, source_ids, strict=True))
    event_path = tmp_path / "events.jsonl"
    JsonlEventWriter(event_path).write(event)
    serialized = json.loads(event_path.read_text(encoding="utf-8"))
    serialized_ids = tuple(serialized[field] for field in ("run_id", "query_id", "document_id", "generation_id"))
    assert all(value != source for value, source in zip(serialized_ids, source_ids, strict=True))


def test_registry_maps_real_query_pin_identifiers() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    pin = AuthorizationGuard.policy_only(store).pin(
        "user_01", "g" * 64, "doc_01", "doc-v1", "gen_01"
    )
    assert UUID(pin.query_snapshot_id).version == 4

    registry = OpaqueIdRegistry()
    query_id = registry.map(OpaqueIdKind.QUERY, pin.query_snapshot_id)
    document_id = registry.map(OpaqueIdKind.DOCUMENT, pin.document_id)
    generation_id = registry.map(OpaqueIdKind.GENERATION, pin.generation_id)
    assert query_id.value != pin.query_snapshot_id
    assert document_id.value != pin.document_id
    assert generation_id.value != pin.generation_id


def test_registry_maps_encoded_tokens_without_reflecting_them(tmp_path: Path) -> None:
    encoded_payload = "doc_U2VjcmV0LnBkZg=="
    registry = OpaqueIdRegistry()
    mapped = registry.map(OpaqueIdKind.DOCUMENT, encoded_payload)
    event = SafeEvent(
        EventName.STAGE_FINISHED,
        registry.map(OpaqueIdKind.RUN, "run_01"),
        registry.map(OpaqueIdKind.QUERY, "query_01"),
        mapped,
        registry.map(OpaqueIdKind.GENERATION, "gen_01"),
        EventStage.RETRIEVE,
    )
    event_path = tmp_path / "events.jsonl"
    JsonlEventWriter(event_path).write(event)
    serialized = json.loads(event_path.read_text(encoding="utf-8"))
    assert mapped.value != encoded_payload
    assert serialized["document_id"] == mapped.value
    assert serialized["document_id"] != encoded_payload


@pytest.mark.parametrize("source_id", ["", " \t", "doc_01\n", "doc_01\u0085", "x" * 257])
def test_registry_rejects_unbounded_or_control_character_ids(source_id: str) -> None:
    with pytest.raises(ValueError) as error:
        OpaqueIdRegistry().map(OpaqueIdKind.DOCUMENT, source_id)
    assert str(error.value) == "invalid upstream identifier"


@pytest.mark.parametrize(
    "stage",
    [EventStage.SOURCE_LOAD, EventStage.MODEL, EventStage.SERIALIZE, EventStage.HIGHLIGHT_OPEN],
)
def test_additional_registered_stages_are_accepted(stage: EventStage) -> None:
    event = SafeEvent(
        EventName.STAGE_FINISHED,
        opaque(OpaqueIdKind.RUN),
        opaque(OpaqueIdKind.QUERY),
        opaque(OpaqueIdKind.DOCUMENT),
        opaque(OpaqueIdKind.GENERATION),
        stage,
    )
    assert event.stage is stage


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
