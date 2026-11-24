"""Validated, payload-free JSONL observability events."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import unicodedata
import weakref
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
_ISSUED_IDS: weakref.WeakSet[OpaqueId] = weakref.WeakSet()
_ISSUED_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True, weakref_slot=True)
class OpaqueId:
    """A registered-kind ID represented only by a kind and 128-bit hex token."""

    kind: OpaqueIdKind
    value: str
    _factory: InitVar[object] = field(default=None, repr=False, kw_only=True)

    def __post_init__(self, _factory: object) -> None:
        if _factory is not _ID_FACTORY:
            raise TypeError("opaque IDs are issued only by OpaqueIdRegistry")
        if not isinstance(self.kind, OpaqueIdKind):
            raise ValueError("invalid opaque identifier")
        if not isinstance(self.value, str) or _OPAQUE_VALUE.fullmatch(self.value) is None:
            raise ValueError("invalid opaque identifier")
        if not self.value.startswith(f"{self.kind.value}_"):
            raise ValueError("invalid opaque identifier")

class OpaqueIdRegistry:
    """Map validated upstream IDs to random observability-only IDs."""

    def __init__(self) -> None:
        self._ids: dict[tuple[OpaqueIdKind, str], OpaqueId] = {}
        self._lock = threading.Lock()

    def map(self, kind: OpaqueIdKind, source_id: str) -> OpaqueId:
        if not isinstance(kind, OpaqueIdKind) or not isinstance(source_id, str):
            raise ValueError("invalid upstream identifier")
        if (
            not source_id.strip()
            or len(source_id) > 256
            or any(unicodedata.category(character) == "Cc" for character in source_id)
        ):
            raise ValueError("invalid upstream identifier")
        key = (kind, source_id)
        with self._lock:
            existing = self._ids.get(key)
            if existing is not None:
                return existing
            identifier = OpaqueId(kind, f"{kind.value}_{secrets.token_hex(16)}", _factory=_ID_FACTORY)
            with _ISSUED_LOCK:
                _ISSUED_IDS.add(identifier)
            self._ids[key] = identifier
            return identifier


def _is_registry_issued(identifier: OpaqueId) -> bool:
    with _ISSUED_LOCK:
        return identifier in _ISSUED_IDS


class EventStage(StrEnum):
    AUTHORIZE = "authorize"
    DELETE = "delete"
    GENERATE = "generate"
    INDEX = "index"
    RETRIEVE = "retrieve"
    SOURCE_LOAD = "source_load"
    MODEL = "model"
    SERIALIZE = "serialize"
    HIGHLIGHT_OPEN = "highlight_open"


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
            if (
                not isinstance(value, OpaqueId)
                or value.kind is not expected_kind
                or not _is_registry_issued(value)
            ):
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
