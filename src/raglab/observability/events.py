"""Validated, payload-free JSONL observability events."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType


class EventName(StrEnum):
    STAGE_FINISHED = "stage_finished"


class OpaqueIdKind(StrEnum):
    RUN = "run"
    QUERY = "query"
    DOCUMENT = "document"
    GENERATION = "generation"


_OPAQUE_VALUE = re.compile(r"(?:run|query|document|generation)_[0-9a-f]{32}\Z")
_ID_FACTORY = object()


@dataclass(frozen=True, slots=True)
class OpaqueId:
    """A registered-kind ID represented only by a kind and 128-bit hex token."""

    kind: OpaqueIdKind
    value: str
    _factory: InitVar[object] = field(default=None, repr=False, kw_only=True)

    def __post_init__(self, _factory: object) -> None:
        if _factory is not _ID_FACTORY:
            raise TypeError("use OpaqueId.issue or OpaqueId.parse")
        if not isinstance(self.kind, OpaqueIdKind):
            raise ValueError("invalid opaque identifier")
        if not isinstance(self.value, str) or _OPAQUE_VALUE.fullmatch(self.value) is None:
            raise ValueError("invalid opaque identifier")
        if not self.value.startswith(f"{self.kind.value}_"):
            raise ValueError("invalid opaque identifier")

    @classmethod
    def issue(cls, kind: OpaqueIdKind) -> OpaqueId:
        if not isinstance(kind, OpaqueIdKind):
            raise ValueError("invalid opaque identifier kind")
        return cls(kind, f"{kind.value}_{secrets.token_hex(16)}", _factory=_ID_FACTORY)

    @classmethod
    def parse(cls, value: str) -> OpaqueId:
        if not isinstance(value, str) or _OPAQUE_VALUE.fullmatch(value) is None:
            raise ValueError("invalid opaque identifier")
        kind_label = value.partition("_")[0]
        try:
            kind = OpaqueIdKind(kind_label)
        except ValueError as exc:
            raise ValueError("invalid opaque identifier") from exc
        return cls(kind, value, _factory=_ID_FACTORY)


class EventStage(StrEnum):
    AUTHORIZE = "authorize"
    DELETE = "delete"
    GENERATE = "generate"
    INDEX = "index"
    RETRIEVE = "retrieve"


_ALLOWED_METRICS = frozenset(
    {
        "duration_ms",
        "candidate_count",
        "denied_count",
        "retry_count",
        "working_set_bytes",
        "queue_ms",
        "deleted_count",
        "coordinate_validation_failures",
    }
)
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class SafeEvent:
    event_name: EventName
    run_id: OpaqueId
    query_id: OpaqueId
    document_id: OpaqueId
    generation_id: OpaqueId
    stage: EventStage
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.event_name, EventName):
            raise ValueError("event_name must be an EventName")
        expected_kinds = {
            "run_id": OpaqueIdKind.RUN,
            "query_id": OpaqueIdKind.QUERY,
            "document_id": OpaqueIdKind.DOCUMENT,
            "generation_id": OpaqueIdKind.GENERATION,
        }
        for name, expected_kind in expected_kinds.items():
            value = getattr(self, name)
            if not isinstance(value, OpaqueId) or value.kind is not expected_kind:
                raise ValueError(f"{name} must be a {expected_kind.value} OpaqueId")
        if not isinstance(self.stage, EventStage):
            raise ValueError("stage must be a registered EventStage")
        if not isinstance(self.metrics, Mapping):
            raise ValueError("metrics must be a mapping")

        keys = list(self.metrics)
        normalized_keys: set[str] = set()
        for key in keys:
            if not isinstance(key, str):
                raise ValueError("forbidden event field")
            normalized_key = key.casefold()
            if normalized_key in normalized_keys:
                raise ValueError("duplicate metric key")
            normalized_keys.add(normalized_key)

        safe_metrics: dict[str, int | float | bool] = {}
        for key, value in self.metrics.items():
            if key not in _ALLOWED_METRICS:
                raise ValueError("forbidden event field")
            if not (
                isinstance(value, bool)
                or type(value) is int
                or isinstance(value, float) and math.isfinite(value)
            ):
                raise ValueError("metric values must be finite numeric or boolean values")
            safe_metrics[key] = value
        object.__setattr__(self, "metrics", MappingProxyType(dict(sorted(safe_metrics.items()))))


class JsonlEventWriter:
    """Append JSONL records with single-process thread safety.

    Separate processes must use separate paths; cross-process locking is not
    provided.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def write(self, event: SafeEvent) -> None:
        if not isinstance(event, SafeEvent):
            raise TypeError("event must be a SafeEvent")
        parent = self.path.parent
        if parent != Path("."):
            parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event_name": event.event_name.value,
            "run_id": event.run_id.value,
            "query_id": event.query_id.value,
            "document_id": event.document_id.value,
            "generation_id": event.generation_id.value,
            "stage": event.stage.value,
            "metrics": dict(event.metrics),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        # One lock-protected write keeps records from separate writers from interleaving.
        with _WRITE_LOCK:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
