from dataclasses import replace
from decimal import localcontext

import pytest

from raglab.contracts import EvidenceRole, NativeLocator
from raglab.track_b.query import TrackBQuery, answer_query

from .helpers import numeric_evidence


def test_lookup_returns_exact_cell_text_and_evidence() -> None:
    evidence = numeric_evidence({"B2": "12.5", "B3": "other"})
    result = answer_query(TrackBQuery("lookup", "sheet-1", "B3"), evidence)
    assert result.status == "answered"
    assert result.answer == "other"
    assert result.candidates == evidence[1:]
    assert result.operation == "deterministic_lookup"


@pytest.mark.parametrize(
    ("operation", "answer"),
    [("sum", "20.0"), ("min", "7.5"), ("max", "12.5"), ("average", "10.0")],
)
def test_aggregates_use_only_the_explicit_range(operation: str, answer: str) -> None:
    evidence = numeric_evidence({"B2": "12.5", "B3": "7.5", "B4": "1000"})
    result = answer_query(TrackBQuery(operation, "sheet-1", "B2:B3"), evidence)
    assert result.status == "answered"
    assert result.answer == answer
    assert result.operation == f"deterministic_{operation}"
    assert result.candidates == evidence[:2]


def test_rectangular_aggregate_caps_candidates_in_coordinate_order() -> None:
    evidence = numeric_evidence({"AA2": "1", "Z2": "2", "AA3": "3", "Z3": "4"})
    query = TrackBQuery("sum", "sheet-1", "Z2:AA3")
    result = answer_query(query, evidence)
    assert result == answer_query(query, evidence[::-1])
    assert result.answer == "10"
    assert tuple(item.native_locator.cell_range for item in result.candidates) == ("Z2", "AA2", "Z3")


@pytest.mark.parametrize(
    "value", [None, "unknown", "", "NaN", "sNaN", "Infinity", "1e999999", "9" * 257]
)
def test_aggregate_abstains_if_any_text_is_not_supported(value: str | None) -> None:
    evidence = numeric_evidence({"B2": "12.5", "B3": "7.5"})
    result = answer_query(
        TrackBQuery("average", "sheet-1", "B2:B3"),
        (evidence[0], replace(evidence[1], text=value)),
    )
    assert result.status == "판단 불가"
    assert result.answer is None


def test_aggregate_abstains_for_missing_cell_even_with_duplicate() -> None:
    (cell,) = numeric_evidence({"B2": "12.5"})
    query = TrackBQuery("sum", "sheet-1", "B2:B3")
    assert answer_query(query, (cell,)).status == "판단 불가"
    assert answer_query(query, (cell, cell)).status == "판단 불가"


@pytest.mark.parametrize("field", ["source_hash", "document_version", "generation_id", "acl_snapshot_id"])
def test_aggregate_does_not_combine_mismatched_evidence(field: str) -> None:
    first, second = numeric_evidence({"B2": "12.5", "B3": "7.5"})
    result = answer_query(
        TrackBQuery("sum", "sheet-1", "B2:B3"), (first, replace(second, **{field: "different"}))
    )
    assert result.status == "판단 불가"


def test_lookup_abstains_for_missing_or_ambiguous_text() -> None:
    (cell,) = numeric_evidence({"B2": "12.5"})
    query = TrackBQuery("lookup", "sheet-1", "B2")
    for evidence in ((), (replace(cell, text=None),), (cell, cell)):
        assert answer_query(query, evidence).status == "판단 불가"


def test_lookup_ignores_other_sheets_and_non_cell_evidence() -> None:
    (cell,) = numeric_evidence({"B2": "12.5"})
    unrelated = (
        replace(cell, native_locator=NativeLocator.sheet_range("sheet-2", "B2")),
        replace(cell, native_locator=NativeLocator.sheet_range("sheet-1", "B2:B3")),
        replace(cell, native_locator=NativeLocator.sheet_object("sheet-1", "chart-1")),
        replace(cell, role=EvidenceRole.LABEL),
    )
    query = TrackBQuery("lookup", "sheet-1", "B2")
    assert answer_query(query, unrelated).status == "판단 불가"
    assert answer_query(query, unrelated + (cell,)).candidates == (cell,)


@pytest.mark.parametrize(
    ("operation", "reference"),
    [("lookup", "B2:B2"), ("sum", "B2:B1"), ("sum", "A0"), ("sum", "XFE1"),
     ("sum", "B2:+B3"), ("sum", "B2:B3:B4"), ("sum", "A1:XFD1048576")],
)
def test_invalid_or_unfillable_ranges_abstain(operation: str, reference: str) -> None:
    result = answer_query(TrackBQuery(operation, "sheet-1", reference), numeric_evidence({"B2": "1"}))
    assert result.status == "판단 불가"
    assert result.answer is None


def test_unsupported_operation_abstains() -> None:
    result = answer_query(TrackBQuery("median", "sheet-1", "B2"), numeric_evidence({"B2": "1"}))
    assert result.status == "판단 불가"
    assert result.operation == "unsupported_operation"
    assert result.candidates == ()


def test_aggregation_uses_stored_cache_and_a_fixed_decimal_context() -> None:
    first, second, third = numeric_evidence({"B2": "1", "B3": "0", "B4": "0"})
    cached = replace(first, metadata={"formula": "1+1", "formula_state": "cached_not_recalculated"})
    query = TrackBQuery("average", "sheet-1", "B2:B4")
    with localcontext() as context:
        context.prec = 2
        result = answer_query(query, (cached, second, third))
    assert result.answer == "0.3333333333333333333333333333"


def test_aggregate_preserves_small_terms_after_large_terms_cancel() -> None:
    evidence = numeric_evidence({"B2": "1e200", "B3": "1", "B4": "-1e200"})
    result = answer_query(TrackBQuery("sum", "sheet-1", "B2:B4"), evidence)
    assert result.answer == "1"


def test_conflicting_duplicate_evidence_abstains_independent_of_input_order() -> None:
    (cell,) = numeric_evidence({"B2": "12.5"})
    conflicting = replace(cell, text="99")
    query = TrackBQuery("lookup", "sheet-1", "B2")
    result = answer_query(query, (cell, conflicting))
    assert result.status == "판단 불가"
    assert result == answer_query(query, (conflicting, cell))
