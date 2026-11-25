import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from raglab.offline.windows_paths import WindowsLocalPaths


FORBIDDEN_FIELDS = frozenset({
    "document_name", "document_path", "document_text", "ocr_text",
    "question", "crop", "embedding", "secret", "token",
})
_REDACTED = "[REDACTED]"
_FIELD_SEPARATORS = re.compile(r"[^a-z0-9]")
_FORBIDDEN_KEYS = frozenset(_FIELD_SEPARATORS.sub("", key) for key in FORBIDDEN_FIELDS)
_ABSOLUTE_PATH = re.compile(r"""[A-Za-z]:[\\/]|\\\\|//|(?<![\w.])/(?![\s/'"]|$)|(?<![\w.\\])\\(?=[^\s\\'"])""")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_STREAM_LOCK = Lock()
_MAX_PROGRESS_LINE_BYTES = 4096


class Redactor:
    def redact(self, value: object) -> object:
        if isinstance(value, str):
            return _REDACTED if _ABSOLUTE_PATH.search(value) else value
        if isinstance(value, Mapping):
            output = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                safe_key = self.redact(key)
                if safe_key == _REDACTED or _FIELD_SEPARATORS.sub("", key.casefold()) in _FORBIDDEN_KEYS:
                    output[safe_key] = _REDACTED
                else:
                    output[safe_key] = self.redact(item)
            return output
        if isinstance(value, (tuple, list)):
            return [self.redact(item) for item in value]
        if value is None or type(value) in {int, float, bool}:
            return value
        return _REDACTED


@dataclass(frozen=True)
class ProgressEvent:
    schema_version: int
    run_id: str
    sequence: int
    stage: str
    completed: int
    total: int
    elapsed_ms: int
    peak_working_set_bytes: int
    retry_count: int
    status: str


def _event_line(event: ProgressEvent) -> bytes:
    if type(event) is not ProgressEvent or vars(event).keys() != ProgressEvent.__dataclass_fields__.keys():
        raise ValueError("unexpected progress event fields")
    counts = (
        event.sequence, event.completed, event.total, event.elapsed_ms,
        event.peak_working_set_bytes, event.retry_count,
    )
    if type(event.schema_version) is not int or event.schema_version != 1 or any(
        type(count) is not int or not 0 <= count <= 2**64 - 1 for count in counts
    ) or event.completed > event.total:
        raise ValueError("invalid numeric progress field")
    if any(type(value) is not str or not _IDENTIFIER.fullmatch(value) for value in (
        event.run_id, event.stage, event.status,
    )):
        raise ValueError("progress identifiers must be opaque")
    payload = Redactor().redact(asdict(event))
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (line + "\n").encode("utf-8")


def _last_sequence(stream: BinaryIO) -> int | None:
    previous = None
    while line := stream.readline(_MAX_PROGRESS_LINE_BYTES):
        try:
            row = ProgressEvent(**json.loads(line))
            if _event_line(row) != line or (previous is not None and row.sequence <= previous):
                raise ValueError("noncanonical or out-of-order progress line")
        except (TypeError, ValueError, RecursionError):
            raise ValueError("invalid existing progress stream") from None
        previous = row.sequence
    return previous


class JsonlProgressSink:
    def __init__(self, path: Path):
        self._path = Path(path)

    def emit(self, event: ProgressEvent) -> None:
        line = _event_line(event)
        with _STREAM_LOCK, WindowsLocalPaths().open_progress_stream(self._path) as stream:
            previous = _last_sequence(stream)
            if previous is not None and event.sequence <= previous:
                raise ValueError("progress sequence must strictly increase")
            position = stream.tell()
            try:
                if stream.write(line) != len(line):
                    raise OSError("incomplete progress write")
            except OSError:
                stream.truncate(position)
                raise
