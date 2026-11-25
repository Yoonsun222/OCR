"""Parse native workbook cells without launching Excel or evaluating formulas."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from xml.etree.ElementTree import Element

from .errors import InvalidPackage
from .models import CellObservation, SheetObservation, WorkbookObservation
from .opc import OpcPackage, Relationship

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORKSHEET_REL = f"{REL_TYPE}/worksheet"
SHARED_STRINGS_REL = f"{REL_TYPE}/sharedStrings"
WORKBOOK_PART = "xl/workbook.xml"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
CELL_REFERENCE = re.compile(r"[A-Z]{1,3}[1-9][0-9]{0,6}")


def _rich_text(node: Element) -> str:
    chunks: list[str] = []
    for child in node:
        if child.tag == f"{S}t":
            chunks.append(child.text or "")
        elif child.tag == f"{S}r":
            chunks.append(child.findtext(f"{S}t", ""))
    return "".join(chunks)


def _shared_strings(package: OpcPackage, relationships: dict[str, Relationship]) -> tuple[str, ...]:
    targets = [rel.target for rel in relationships.values() if rel.relationship_type == SHARED_STRINGS_REL]
    if len(targets) > 1:
        raise InvalidPackage("multiple shared string parts")
    part = targets[0] if targets else SHARED_STRINGS_PART
    if part not in package.names:
        return ()
    root = package.read_xml(part)
    if root.tag != f"{S}sst":
        raise InvalidPackage("invalid shared string part")
    return tuple(_rich_text(node) for node in root.iterfind(f"{S}si"))


def _shared_value(raw: str, shared: tuple[str, ...]) -> str:
    try:
        index = int(raw)
        if index >= 0:
            return shared[index]
    except (ValueError, IndexError):
        pass
    raise InvalidPackage("invalid shared string index")


def _numeric_value(raw: str) -> Decimal | None:
    try:
        numeric = Decimal(raw)
    except InvalidOperation:
        return None
    return numeric if numeric.is_finite() else None


def _cell(node: Element, shared: tuple[str, ...]) -> CellObservation:
    reference = node.get("r", "")
    if CELL_REFERENCE.fullmatch(reference) is None:
        raise InvalidPackage("invalid cell reference")

    kind = node.get("t", "n")
    formula_node = node.find(f"{S}f")
    formula = formula_node.text if formula_node is not None else None
    raw = node.findtext(f"{S}v") or None
    formula_state = "literal"
    if formula_node is not None:
        formula_state = "cached_not_recalculated" if raw is not None else "cached_missing"

    display = raw
    numeric = None
    if kind == "inlineStr" and formula_node is None:
        inline = node.find(f"{S}is")
        display = _rich_text(inline) if inline is not None else None
    elif raw is not None:
        if kind == "s":
            display = _shared_value(raw, shared)
        elif kind == "n":
            numeric = _numeric_value(raw)

    return CellObservation(reference, display, numeric, formula, formula_state, kind, node.get("s"))


def _sheet_cells(package: OpcPackage, part: str, shared: tuple[str, ...]) -> dict[str, CellObservation]:
    root = package.read_xml(part)
    if root.tag != f"{S}worksheet":
        raise InvalidPackage("invalid worksheet part")
    cells: dict[str, CellObservation] = {}
    for node in root.iterfind(f"{S}sheetData/{S}row/{S}c"):
        cell = _cell(node, shared)
        if cell.reference in cells:
            raise InvalidPackage("duplicate cell reference")
        cells[cell.reference] = cell
    return cells


def parse_workbook(package: OpcPackage) -> WorkbookObservation:
    root = package.read_xml(WORKBOOK_PART)
    if root.tag != f"{S}workbook":
        raise InvalidPackage("invalid workbook part")
    relationships = {rel.relationship_id: rel for rel in package.relationships(WORKBOOK_PART)}
    shared = _shared_strings(package, relationships)
    properties = root.find(f"{S}workbookPr")
    date_1904 = properties is not None and properties.get("date1904") in {"1", "true"}

    sheets: list[SheetObservation] = []
    sheet_ids: set[str] = set()
    for ordinal, node in enumerate(root.iterfind(f"{S}sheets/{S}sheet"), start=1):
        relationship_id = node.get(f"{R}id", "")
        rel = relationships.get(relationship_id)
        if rel is None or rel.relationship_type != WORKSHEET_REL:
            raise InvalidPackage("invalid worksheet relationship")
        sheet_id = node.get("sheetId", str(ordinal))
        name = node.get("name")
        if not name or not sheet_id or sheet_id in sheet_ids:
            raise InvalidPackage("invalid worksheet metadata")
        sheet_ids.add(sheet_id)
        sheets.append(SheetObservation(
            stable_id=f"sheet-{sheet_id}",
            relationship_id=relationship_id,
            name=name,
            state=node.get("state", "visible"),
            part_name=rel.target,
            cells=_sheet_cells(package, rel.target, shared),
        ))
    return WorkbookObservation(package.source_hash(), "1904" if date_1904 else "1900", tuple(sheets))
