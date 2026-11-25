import json
import sqlite3
from pathlib import Path

import pytest

from raglab.track_c.models import DocumentTarget, IndexRecord
from tests.track_c.helpers import evidence


@pytest.fixture(params=("MemoryTextIndex", "SqliteTextIndex"))
def index(request):
    from raglab.track_c import text_index

    return getattr(text_index, request.param)()


def record(document_id, evidence_id, *, generation_id="gen-2", document_version="v1", text="Pressure"):
    return IndexRecord(evidence(evidence_id, document_version, generation_id, text), document_id, "text", text)


def test_scoped_retrieval_excludes_unauthorized_text_before_analysis(index, monkeypatch):
    from raglab.track_c import text_index

    corpus = json.loads((Path(__file__).parent / "fixtures" / "corpus.json").read_text())
    allowed = record(**corpus["records"][0])
    index.build((allowed, record("doc-hidden", "ev-hidden", text="Pressure hidden")), "gen-2")
    index.build((record("doc-allowed", "ev-other", generation_id="gen-3"),), "gen-3")
    original = text_index.analyze

    def guarded_analyze(text):
        assert text == allowed.searchable_text
        return original(text)

    monkeypatch.setattr(text_index, "analyze", guarded_analyze)
    ids = frozenset({"doc-allowed"})
    assert [hit.record for hit in index.search(("pressure",), ids, "gen-2", 10)] == [allowed]
    assert index.search(("pressure",), frozenset({"unknown"}), "gen-2", 10) == ()
    assert index.search(("pressure",), ids, "unknown", 10) == ()
    assert index.search(
        ("pressure",), ids, "gen-2", 10, document_targets=(DocumentTarget("doc-allowed", "v2"),),
    ) == ()


def test_repeated_terms_and_ties_have_identical_ranks(index):
    records = (
        record("doc-allowed", "ev-c", text="Pressure pressure"),
        record("doc-allowed", "ev-b"),
        record("doc-allowed", "ev-a"),
        record("doc-hidden", "ev-0", text="Pressure pressure"),
    )
    index.build(records, "gen-2")
    hits = index.search(("pressure", "pressure"), frozenset({"doc-allowed"}), "gen-2", 3)
    assert [(hit.record.evidence.evidence_id, hit.score, hit.rank) for hit in hits] == [
        ("ev-c", 1.0, 1), ("ev-a", 0.5, 2), ("ev-b", 0.5, 3),
    ]
    assert index.search(('"pressure" OR *',), frozenset({"doc-allowed"}), "gen-2", 3) == ()


def test_invalid_build_keeps_prior_generation(index):
    original = record("doc-allowed", "ev-1")
    index.build((original,), "gen-2")
    for replacement in (
        (original, original),
        (original, record("doc-allowed", "wrong-version", document_version="v2")),
        (record("doc-allowed", "wrong-generation", generation_id="gen-3"),),
    ):
        with pytest.raises(ValueError):
            index.build(replacement, "gen-2")
    ids = frozenset({"doc-allowed"})
    assert [hit.record for hit in index.search(("pressure",), ids, "gen-2", 1)] == [original]
    index.build((), "gen-2")
    assert index.search(("pressure",), ids, "gen-2", 1) == ()


def test_sqlite_failed_transaction_keeps_prior_generation():
    from raglab.track_c.text_index import SqliteTextIndex

    index = SqliteTextIndex()
    original = record("doc-allowed", "ev-1")
    index.build((original,), "gen-2")
    index._connection.execute(
        "CREATE TRIGGER fail_insert BEFORE INSERT ON text_records "
        "WHEN new.evidence_id = 'ev-fail' BEGIN SELECT RAISE(ABORT, 'failed insert'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="failed insert"):
        index.build((record("doc-allowed", "ev-2"), record("doc-allowed", "ev-fail")), "gen-2")
    hits = index.search(("pressure",), frozenset({"doc-allowed"}), "gen-2", 1)
    assert [hit.record for hit in hits] == [original]


def test_public_search_requires_immutable_scopes_and_valid_limits(index):
    with pytest.raises(ValueError, match="frozenset"):
        index.search(("pressure",), {"doc-allowed"}, "gen-2", 1)
    with pytest.raises(ValueError, match="tuple"):
        index.search(("pressure",), frozenset(), "gen-2", 1, document_targets=[])
    with pytest.raises(ValueError, match="limit"):
        index.search((), frozenset(), "gen-2", True)

def test_index_works_as_a_text_adapter_on_worker_thread(index):
    from concurrent.futures import ThreadPoolExecutor

    original = record("doc-allowed", "ev-1")
    index.build((original,), "gen-2")
    with ThreadPoolExecutor(max_workers=1) as pool:
        hits = pool.submit(index.search, ("pressure",), frozenset({"doc-allowed"}), "gen-2", 1).result()
    assert index.channel == "text"
    assert [hit.record for hit in hits] == [original]
