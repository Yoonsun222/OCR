from dataclasses import replace

import pytest

from raglab.indexing.generation import GenerationState
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore
from raglab.track_c.models import (
    DocumentTarget,
    FusionResult,
    IndexRecord,
    MixedQuery,
    MixedResponse,
    PinnedMixedQuery,
    ResponseState,
    SearchHit,
)
from tests.track_c.helpers import candidate, evidence, generation, pinned_query


@pytest.mark.parametrize("bad_id", ["", " ", 7])
def test_document_target_rejects_invalid_identity(bad_id):
    with pytest.raises(ValueError, match="document_id"):
        DocumentTarget(bad_id, "v1")
    with pytest.raises(ValueError, match="document_version"):
        DocumentTarget("doc-1", bad_id)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("text", "  ", "query text"),
        ("text", None, "query text"),
        ("principal_id", "", "principal_id"),
        ("group_hash", b"g" * 64, "group_hash"),
        ("group_hash", " " * 64, "group_hash"),
        ("group_hash", "short", "group_hash"),
        ("targets", (), "target"),
        ("targets", ("doc-1",), "target"),
        ("targets", (DocumentTarget("doc-1", "v1"), DocumentTarget("doc-1", "v2")), "unique"),
        ("limit", True, "limit"),
        ("limit", 2.0, "limit"),
        ("limit", 4, "limit"),
    ],
)
def test_mixed_query_rejects_invalid_fields(field, value, message):
    with pytest.raises(ValueError, match=message):
        replace(pinned_query().request, **{field: value})


def test_mixed_query_copies_caller_target_collection():
    target = DocumentTarget("doc-1", "v1")
    targets = [target]
    request = MixedQuery("pressure", "user-1", "g" * 64, targets)
    targets.clear()
    assert request.targets == (target,)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("document_id", "doc-2", "target"),
        ("document_version", "v2", "version"),
        ("generation_id", "gen-2", "generation"),
        ("principal_id", "user-2", "principal"),
        ("group_hash", "h" * 64, "group"),
        ("policy_epoch", True, "epoch"),
        ("policy_epoch", -1, "epoch"),
        ("query_snapshot_id", " ", "snapshot"),
    ],
)
def test_pinned_query_rejects_mismatched_or_invalid_pin(field, value, message):
    pinned = pinned_query()
    pin = replace(pinned.pins[0], **{field: value})
    with pytest.raises(ValueError, match=message):
        replace(pinned, pins=(pin,))


def test_pinned_query_requires_exactly_one_pin_per_target():
    pinned = pinned_query()
    for pins in ((), pinned.pins * 2, (object(),)):
        with pytest.raises(ValueError, match="pin"):
            replace(pinned, pins=pins)


def test_pinned_query_rejects_inactive_generation():
    pinned = pinned_query()
    inactive = replace(pinned.generation, state=GenerationState.VALIDATED)
    with pytest.raises(ValueError, match="active"):
        replace(pinned, generation=inactive)


def test_pinned_query_preserves_distinct_common_guard_document_snapshots():
    first = DocumentTarget("doc-1", "v1")
    second = DocumentTarget("doc-2", "v2")
    request = MixedQuery("pressure", "user-1", "g" * 64, (first, second))
    store = MemoryPolicyStore()
    guard = AuthorizationGuard.policy_only(store)
    active = generation()
    pins = []
    for target in request.targets:
        store.allow(request.principal_id, target.document_id, group_hash=request.group_hash)
        pins.append(guard.pin(
            request.principal_id, request.group_hash, target.document_id,
            target.document_version, active.generation_id,
        ))
    expected = tuple(pins)

    pinned = PinnedMixedQuery(request, active, pins)
    pins.clear()
    assert pinned.pins == expected
    assert expected[0].query_snapshot_id != expected[1].query_snapshot_id


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf"), True, "1"])
def test_ranked_result_types_reject_invalid_scores(score):
    record = IndexRecord(evidence(), "doc-1", "text", "pressure")
    with pytest.raises(ValueError, match="score"):
        SearchHit(record, score, 1)
    with pytest.raises(ValueError, match="score"):
        candidate(score=score)


@pytest.mark.parametrize("rank", [0, -1, True, 1.0, "1"])
def test_ranked_result_types_require_positive_integer_ranks(rank):
    record = IndexRecord(evidence(), "doc-1", "text", "pressure")
    with pytest.raises(ValueError, match="rank"):
        SearchHit(record, 0.8, rank)
    with pytest.raises(ValueError, match="rank"):
        candidate(rank=rank)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence", None),
        ("document_id", " "),
        ("channel", "audio"),
        ("structured_key", 12),
        ("structured_value", 12.5),
        ("unit", 7),
    ],
)
def test_index_and_candidate_require_typed_evidence_fields(field, value):
    record = IndexRecord(evidence(), "doc-1", "text", "pressure")
    with pytest.raises(ValueError, match=field):
        replace(record, **{field: value})
    with pytest.raises(ValueError, match=field):
        replace(candidate(), **{field: value})


def test_index_record_copies_relation_kinds():
    kinds = ["label"]
    record = IndexRecord(evidence(), "doc-1", "text", "pressure", relation_kinds=kinds)
    kinds.clear()
    assert record.relation_kinds == ("label",)


def test_candidate_exposes_evidence_generation_id():
    assert candidate().generation_id == "gen-1"


def test_fusion_copies_caller_candidates_and_keys():
    item = candidate()
    candidates = [item]
    keys = ["Pressure"]
    fusion = FusionResult(candidates, True, keys)
    candidates.clear()
    keys.clear()
    assert fusion.candidates == (item,)
    assert fusion.conflict_keys == ("Pressure",)


def test_response_copies_caller_evidence_and_diagnostics():
    item = evidence()
    items = [item]
    diagnostics = {"results": 1}
    result = MixedResponse(ResponseState.VERIFIED, "12.5 MPa", items, "VERIFIED", diagnostics)
    items.clear()
    diagnostics["results"] = 2

    assert result.evidence == (item,)
    assert result.diagnostics == {"results": 1}
    with pytest.raises(TypeError):
        result.diagnostics["results"] = 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "VERIFIED"),
        ("answer", None),
        ("evidence", ("ev-1",)),
        ("evidence", (evidence(),) * 4),
        ("reason_code", " "),
        ("diagnostics", {"results": True}),
        ("diagnostics", {1: 1}),
    ],
)
def test_response_rejects_invalid_fields(field, value):
    response = MixedResponse(ResponseState.VERIFIED, "12.5 MPa", (evidence(),), "VERIFIED")
    with pytest.raises(ValueError, match=field):
        replace(response, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [("candidates", (evidence(),)), ("has_conflict", 1), ("conflict_keys", (7,))],
)
def test_fusion_validates_runtime_input(field, value):
    with pytest.raises(ValueError, match=field):
        replace(FusionResult((candidate(),), False), **{field: value})


def test_search_hit_requires_index_record():
    with pytest.raises(ValueError, match="record"):
        SearchHit(evidence(), 0.0, 1)
