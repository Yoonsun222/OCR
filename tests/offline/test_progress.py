import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from raglab.offline.progress import JsonlProgressSink, ProgressEvent


def event(sequence):
    return ProgressEvent(1, "run-opaque", sequence, "demo", 1, 2, 25, 4096, 0, "running")


def test_progress_rejects_sequence_regression(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlProgressSink(path)
    sink.emit(event(2))
    with pytest.raises(ValueError):
        sink.emit(event(1))
    with pytest.raises(ValueError):
        JsonlProgressSink(path).emit(event(2))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_progress_rejects_extra_fields(tmp_path):
    @dataclass(frozen=True)
    class ExtraEvent(ProgressEvent):
        document_name: str = "private.pdf"

    path = tmp_path / "events.jsonl"
    with pytest.raises(ValueError):
        JsonlProgressSink(path).emit(ExtraEvent(1, "run-opaque", 1, "demo", 1, 2, 25, 4096, 0, "running"))
    assert not path.exists()


def test_concurrent_duplicate_writes_commit_one_canonical_line(tmp_path):
    path = tmp_path / "events.jsonl"
    sinks = [JsonlProgressSink(path), JsonlProgressSink(path)] * 8

    def emit(sink):
        try:
            sink.emit(event(1))
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(emit, sinks)) == 1
    [line] = path.read_text(encoding="utf-8").splitlines()
    data = json.loads(line)
    assert data["run_id"] == "run-opaque"
    assert line == json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
