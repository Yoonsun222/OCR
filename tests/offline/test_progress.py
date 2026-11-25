import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from raglab.offline.progress import JsonlProgressSink, ProgressEvent


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='native Windows progress writer')


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


def test_progress_rejects_hardlinked_destination(tmp_path):
    source = tmp_path / "source.jsonl"
    JsonlProgressSink(source).emit(event(1))
    destination = tmp_path / "events.jsonl"
    destination.hardlink_to(source)
    original = source.read_bytes()
    with pytest.raises((OSError, ValueError)):
        JsonlProgressSink(destination).emit(event(2))
    assert source.read_bytes() == original


def test_progress_checks_entire_existing_stream_before_appending(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlProgressSink(path)
    sink.emit(event(1))
    original = b'{"sequence":0,"ocr_text":"private"}\n' + path.read_bytes()
    path.write_bytes(original)
    with pytest.raises(ValueError):
        sink.emit(event(2))
    assert path.read_bytes() == original


def test_progress_rejects_junction_ancestor(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    junction = tmp_path / "redirected"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(source)],
                   check=True, capture_output=True)
    try:
        with pytest.raises((OSError, ValueError)):
            JsonlProgressSink(junction / "new" / "events.jsonl").emit(event(1))
        assert list(source.iterdir()) == []
    finally:
        junction.rmdir()


def test_progress_blocks_writer_in_another_process(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlProgressSink(path)
    sink.emit(event(1))
    original = path.read_bytes()
    script = (
        "import sys; from pathlib import Path; "
        "from raglab.offline.windows_paths import WindowsLocalPaths; "
        "with WindowsLocalPaths().open_progress_stream(Path(sys.argv[1])):\n"
        " print('locked', flush=True); sys.stdin.readline()"
    ).replace("; with", "\nwith")
    src = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": src}
    with subprocess.Popen(
        [sys.executable, "-c", script, str(path)], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as holder:
        try:
            assert holder.stdout.readline().strip() == "locked"
            with pytest.raises(OSError):
                sink.emit(event(2))
        finally:
            holder.communicate(input="release\n", timeout=5)
        assert holder.returncode == 0
    assert path.read_bytes() == original
    sink.emit(event(2))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
