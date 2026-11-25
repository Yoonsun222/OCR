from dataclasses import replace
from threading import Event

from raglab.track_c.models import DocumentTarget, IndexRecord, SearchHit
from raglab.track_c.retrieval import retrieve
from raglab.track_c.text_index import MemoryTextIndex, SqliteTextIndex
from tests.track_c.helpers import evidence, pinned_query


class RecordingAdapter:
    def __init__(self, channel, hits=()):
        self.channel = channel
        self.hits = hits
        self.calls = []

    def search(self, tokens, allowed_document_ids, generation_id, limit, *, document_targets):
        self.calls.append((tokens, allowed_document_ids, generation_id, limit, document_targets))
        return self.hits


def hit(evidence_id, document_id="doc-1", version="v1", generation_id="gen-1", channel="text"):
    return SearchHit(
        IndexRecord(
            evidence(evidence_id, version, generation_id),
            document_id,
            channel,
            "pressure",
            "Pressure",
            "12.5",
            "MPa",
        ),
        1.0,
        1,
    )


def test_every_channel_receives_exact_pinned_targets_before_search():
    query = pinned_query()
    target = DocumentTarget("doc-2", "v7")
    query = replace(
        query,
        request=replace(query.request, targets=(*query.request.targets, target)),
        pins=(*query.pins, replace(query.pins[0], document_id="doc-2", document_version="v7")),
    )
    adapters = tuple(RecordingAdapter(channel) for channel in ("text", "structure", "visual"))

    assert retrieve(query, adapters) == ()
    for adapter in adapters:
        assert adapter.calls == [
            (("pressure",), frozenset({"doc-1", "doc-2"}), "gen-1", 30, query.request.targets)
        ]


def test_retrieval_discards_hits_outside_pinned_scope_or_adapter_channel():
    adapter = RecordingAdapter(
        "text",
        (
            hit("blocked-doc", document_id="doc-2"),
            hit("blocked-version", version="v2"),
            hit("blocked-generation", generation_id="gen-2"),
            hit("blocked-channel", channel="visual"),
            hit("valid"),
        ),
    )

    result = retrieve(pinned_query(), (adapter,))

    assert [candidate.evidence.evidence_id for candidate in result] == ["valid"]
    assert (result[0].structured_key, result[0].structured_value, result[0].unit) == (
        "Pressure", "12.5", "MPa",
    )


def test_text_indexes_search_the_pinned_version(monkeypatch):
    import raglab.track_c.text_index as text_index

    analyzed = []
    analyze = text_index.analyze

    def record_analyzed_text(value):
        analyzed.append(value)
        return analyze(value)

    monkeypatch.setattr(text_index, "analyze", record_analyzed_text)
    for index in (MemoryTextIndex(), SqliteTextIndex()):
        index.build((hit("stale", version="v2").record,), "gen-1")

        assert retrieve(pinned_query(), (index,)) == ()

    assert analyzed == []


def test_adapter_errors_do_not_expose_raw_diagnostics(caplog, capsys):
    class BackendError(Exception):
        pass

    class FailingAdapter(RecordingAdapter):
        def search(self, *args, **kwargs):
            raise BackendError("hidden document metadata")

    assert retrieve(pinned_query(), (FailingAdapter("visual"),)) == ()
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


def test_timeout_returns_available_channels_without_late_content_or_diagnostics(caplog, capsys):
    slow_started = Event()
    release_slow = Event()
    slow_finished = Event()

    class SlowAdapter(RecordingAdapter):
        def search(self, *args, **kwargs):
            slow_started.set()
            try:
                release_slow.wait(2)
                return (hit("late-content", channel="visual"),)
            finally:
                slow_finished.set()

    fast = RecordingAdapter("text", (hit("available"),))
    try:
        result = retrieve(pinned_query(), (SlowAdapter("visual"), fast), deadline_seconds=0.2)
        assert slow_started.is_set()
        assert not slow_finished.is_set()
        assert [candidate.evidence.evidence_id for candidate in result] == ["available"]
        assert not caplog.records
        assert capsys.readouterr() == ("", "")
    finally:
        release_slow.set()
        slow_finished.wait(2)


def test_repeated_timeouts_keep_worker_and_pending_work_bounded():
    release = Event()
    started = [Event() for _ in range(3)]
    finished = [Event() for _ in range(3)]

    class BlockingAdapter(RecordingAdapter):
        def search(self, *args, **kwargs):
            position = ("text", "structure", "visual").index(self.channel)
            started[position].set()
            try:
                release.wait(2)
                return ()
            finally:
                finished[position].set()

    blocking = tuple(BlockingAdapter(channel) for channel in ("text", "structure", "visual"))
    try:
        assert retrieve(pinned_query(), blocking, deadline_seconds=0.2) == ()
        assert all(event.is_set() for event in started)
        extra = RecordingAdapter("text", (hit("extra"),))
        for _ in range(10):
            assert retrieve(pinned_query(), (extra,), deadline_seconds=0.005) == ()
        assert extra.calls == []
    finally:
        release.set()
        for event in finished:
            event.wait(2)
