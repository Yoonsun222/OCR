"""Validated, payload-free JSONL observability events."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType


class EventName(StrEnum):
    STAGE_FINISHED = "stage_finished"


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
_FORBIDDEN_PARTS = (
    "text",
    "question",
    "answer",
    "file",
    "path",
    "coordinate",
    "crop",
    "embedding",
)
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_SAFE_STAGE = re.compile(r"[a-z][a-z0-9_]*\Z")
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class SafeEvent:
    event_name: EventName
    run_id: str
    query_id: str
    document_id: str
    generation_id: str
    stage: str
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.event_name, EventName):
            raise ValueError("event_name must be an EventName")
        for name in ("run_id", "query_id", "document_id", "generation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
                raise ValueError(f"{name} must be a safe opaque identifier")
        if not isinstance(self.stage, str) or _SAFE_STAGE.fullmatch(self.stage) is None:
            raise ValueError("stage must be a safe identifier")
        if not isinstance(self.metrics, Mapping):
            raise ValueError("metrics must be a mapping")

        safe_metrics: dict[str, int | float | bool] = {}
        for key, value in self.metrics.items():
            if not isinstance(key, str):
                raise ValueError("forbidden event field: metric key must be a string")
            normalized_key = key.lower()
            # The explicit metric allowlist intentionally includes
            # coordinate_validation_failures; every other coordinate field is denied.
            if normalized_key not in _ALLOWED_METRICS or (
                normalized_key != "coordinate_validation_failures"
                and any(part in normalized_key for part in _FORBIDDEN_PARTS)
            ):
                raise ValueError(f"forbidden event field: {key}")
            if not (
                isinstance(value, bool)
                or type(value) is int
                or isinstance(value, float) and math.isfinite(value)
            ):
                raise ValueError("metric values must be finite numeric or boolean values")
            safe_metrics[key] = value
        object.__setattr__(self, "metrics", MappingProxyType(dict(sorted(safe_metrics.items()))))


class JsonlEventWriter:
    """Append each event as one flushed, durable JSONL record."""

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
            "run_id": event.run_id,
            "query_id": event.query_id,
            "document_id": event.document_id,
            "generation_id": event.generation_id,
            "stage": event.stage,
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
