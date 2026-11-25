"""Answer explicit worksheet queries from authorized native evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context, Decimal, DecimalException, DivisionByZero, InvalidOperation, Overflow,
    ROUND_HALF_EVEN, localcontext,
)
from itertools import islice

from raglab.contracts import EvidenceRegionV1, EvidenceRole, ExtractionMethod

from .coordinates import cell_coordinates, range_coordinates
from .errors import InvalidPackage
from .opc import OpcLimits

_MAX_CELL_REFERENCE_LENGTH = len("XFD1048576")
_MAX_NUMERIC_TEXT_LENGTH = 256
_MAX_NUMERIC_EXPONENT = 1000
_MAX_RANGE_CELLS = OpcLimits().max_workbook_cells


@dataclass(frozen=True)
class TrackBQuery:
    operation: str
    sheet_id: str
    cell_range: str


@dataclass(frozen=True)
class TrackBAnswer:
    status: str
    answer: str | None
    candidates: tuple[EvidenceRegionV1, ...]
    operation: str


def _numeric_text(text: str | None) -> Decimal:
    if text is None or len(text) > _MAX_NUMERIC_TEXT_LENGTH:
        raise ValueError("missing or oversized numeric text")
    value = Decimal(text)
    if not value.is_finite() or abs(value.as_tuple().exponent) > _MAX_NUMERIC_EXPONENT:
        raise ValueError("unsupported decimal magnitude")
    return value


def _aggregate(operation: str, values: list[Decimal]) -> Decimal:
    if operation == "min":
        return min(values)
    if operation == "max":
        return max(values)

    # Align all coefficients without rounding and allow for a carry from every term.
    lowest_place = min(value.as_tuple().exponent for value in values)
    highest_place = max(value.adjusted() for value in values)
    precision = highest_place - lowest_place + len(str(len(values))) + 1
    context = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-9999,
        Emax=9999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    with localcontext(context) as calculation:
        total = sum(islice(values, 1, None), values[0])
        if operation == "average":
            calculation.prec = 28
            return total / len(values)
        return total


def _identity(item: EvidenceRegionV1) -> tuple[str, str, str, str]:
    return item.source_hash, item.document_version, item.generation_id, item.acl_snapshot_id


def answer_query(query: TrackBQuery, evidence: tuple[EvidenceRegionV1, ...]) -> TrackBAnswer:
    """Use supplied authorized cells and aggregate finite decimal text.

    Formula cells can contribute their stored display text. This performs no
    formula recalculation or Excel cell-type coercion. Lookup requests use one
    canonical A1 address, and aggregate requests may use a rectangular A1 range.
    """
    if query.operation not in ("lookup", "sum", "min", "max", "average"):
        return TrackBAnswer("판단 불가", None, (), "unsupported_operation")

    operation = f"deterministic_{query.operation}"
    empty = TrackBAnswer("판단 불가", None, (), operation)
    reference = query.cell_range
    if len(reference) > 2 * _MAX_CELL_REFERENCE_LENGTH + 1:
        return empty
    if query.operation == "lookup" and ":" in reference:
        return empty
    try:
        top, left, bottom, right = range_coordinates(reference)
    except InvalidPackage:
        return empty
    required_cells = (bottom - top + 1) * (right - left + 1)
    if required_cells > _MAX_RANGE_CELLS:
        return empty

    selected: list[tuple[tuple[int, int], EvidenceRegionV1]] = []
    for item in evidence:
        locator = item.native_locator
        if (
            locator.kind != "sheet_range"
            or locator.sheet_id != query.sheet_id
            or item.role != EvidenceRole.VALUE
            or item.extraction_method != ExtractionMethod.NATIVE
        ):
            continue
        try:
            row, column = cell_coordinates(locator.cell_range)
        except InvalidPackage:
            continue
        if top <= row <= bottom and left <= column <= right:
            selected.append(((row, column), item))

    if len({cell for cell, _ in selected}) != len(selected):
        return empty

    selected.sort(key=lambda entry: entry[0])
    candidates = tuple(item for _, item in selected[:3])
    abstain = TrackBAnswer("판단 불가", None, candidates, operation)
    if len(selected) != required_cells:
        return abstain
    if len({_identity(item) for _, item in selected}) != 1:
        return abstain
    if any(item.metadata.get("formula_state") == "cached_missing" for _, item in selected):
        return abstain

    if query.operation == "lookup":
        value = selected[0][1].text
        return TrackBAnswer("answered", value, candidates, operation) if value is not None else abstain

    try:
        values = [_numeric_text(item.text) for _, item in selected]
        answer = _aggregate(query.operation, values)
    except (DecimalException, ValueError):
        return abstain
    return TrackBAnswer("answered", format(answer, "G"), candidates, operation)
