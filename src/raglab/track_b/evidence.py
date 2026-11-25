"""Map workbook observations to the shared native evidence contract."""

from __future__ import annotations

import re
from hashlib import sha256

from raglab.contracts import (
    EvidenceRegionV1,
    EvidenceRelation,
    EvidenceRole,
    ExtractionMethod,
    NativeLocator,
)

from .coordinates import range_coordinates
from .errors import InvalidPackage
from .models import ChartObservation, WorkbookObservation

_A1_CELL = r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}"
_LOCAL_REFERENCE = re.compile(
    rf"=?(?:'(?P<quoted>(?:[^'\[\]:\r\n]|'')+)'|(?P<plain>[\w.]+))!"
    rf"(?P<cells>{_A1_CELL}(?::{_A1_CELL})?)"
)


def _evidence_id(source_hash: str, locator: NativeLocator, role: EvidenceRole) -> str:
    address = locator.cell_range if locator.kind == "sheet_range" else locator.object_id
    key = f"{source_hash}|{locator.kind}|{locator.sheet_id}|{address}|{role.value}"
    return "ev-" + sha256(key.encode("utf-8")).hexdigest()[:24]


def _is_local_range(formula: str, sheet_names: set[str]) -> bool:
    match = _LOCAL_REFERENCE.fullmatch(formula)
    if match is None:
        return False
    quoted = match["quoted"]
    sheet_name = quoted.replace("''", "'") if quoted is not None else match["plain"]
    if sheet_name.casefold() not in sheet_names:
        return False
    try:
        range_coordinates(match["cells"].replace("$", "").upper())
    except InvalidPackage:
        return False
    return True


def workbook_evidence(
    workbook: WorkbookObservation,
    charts: tuple[ChartObservation, ...],
    document_version: str,
    generation_id: str,
    acl_snapshot_id: str,
) -> tuple[EvidenceRegionV1, ...]:
    """Emit native evidence for a single source without resolving chart formulas.

    Chart relations are limited to explicitly sheet-qualified A1 references in
    this workbook. Unsupported references are omitted, while series observations
    and their cached legends remain available.
    """
    sheets = {sheet.stable_id: sheet for sheet in workbook.sheets}
    sheet_names = {sheet.name.casefold() for sheet in workbook.sheets}
    source_hash = workbook.source_hash
    if any(sheet.source_hash != source_hash for sheet in workbook.sheets):
        raise InvalidPackage("sheet evidence source hash mismatch")
    if len(sheets) != len(workbook.sheets) or len(sheet_names) != len(workbook.sheets):
        raise InvalidPackage("duplicate sheet evidence identity")

    output: list[EvidenceRegionV1] = []
    for sheet in workbook.sheets:
        for reference, cell in sorted(sheet.cells.items()):
            locator = NativeLocator.sheet_range(sheet.stable_id, reference)
            output.append(EvidenceRegionV1(
                evidence_id=_evidence_id(source_hash, locator, EvidenceRole.VALUE),
                source_hash=source_hash,
                document_version=document_version,
                generation_id=generation_id,
                native_locator=locator,
                role=EvidenceRole.VALUE,
                text=cell.display_value,
                regions=(),
                relations=(),
                extraction_method=ExtractionMethod.NATIVE,
                confidence=1.0,
                acl_snapshot_id=acl_snapshot_id,
                metadata={
                    "formula": cell.formula,
                    "formula_state": cell.formula_state,
                    "sheet_state": sheet.state,
                },
            ))
    chart_locators: set[NativeLocator] = set()
    for chart in charts:
        sheet = sheets.get(chart.sheet_stable_id)
        if chart.source_hash != source_hash or sheet is None or chart.sheet_part_name != sheet.part_name:
            raise InvalidPackage("chart evidence source or worksheet identity mismatch")
        for series in chart.series:
            object_id = f"{chart.chart_id}:series:{series.order}"
            locator = NativeLocator.sheet_object(chart.sheet_stable_id, object_id)
            if locator in chart_locators:
                raise InvalidPackage("duplicate chart evidence locator")
            chart_locators.add(locator)
            relation_inputs = (
                (EvidenceRole.LEGEND, series.title_formula, series.legend_text),
                (EvidenceRole.SERIES, series.category_formula, chart.chart_type),
                (EvidenceRole.VALUE, series.value_formula, None),
            )
            relations = tuple(
                EvidenceRelation(role, target, note)
                for role, target, note in relation_inputs
                if target is not None and _is_local_range(target, sheet_names)
            )
            output.append(EvidenceRegionV1(
                evidence_id=_evidence_id(source_hash, locator, EvidenceRole.SERIES),
                source_hash=source_hash,
                document_version=document_version,
                generation_id=generation_id,
                native_locator=locator,
                role=EvidenceRole.SERIES,
                text=series.legend_text,
                regions=(),
                relations=relations,
                extraction_method=ExtractionMethod.NATIVE,
                confidence=1.0,
                acl_snapshot_id=acl_snapshot_id,
                metadata={"chart_title": chart.title, "chart_part": chart.part_name},
            ))
    return tuple(output)
