"""Parse native workbook cells without launching Excel or evaluating formulas."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import Element

from .coordinates import (
    MAX_COLUMN, XML_WHITESPACE, cell_coordinates, column_name, numbered_rows, positive_index,
)
from .errors import InvalidPackage, PackageLimitExceeded
from .layout import parse_layout
from .models import CellObservation, SheetObservation, WorkbookObservation
from .opc import OpcPackage, Relationship

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORKSHEET_REL = f"{REL_TYPE}/worksheet"
SHARED_STRINGS_REL = f"{REL_TYPE}/sharedStrings"
WORKBOOK_PART = "xl/workbook.xml"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
CELL_TYPES = {"b", "d", "e", "inlineStr", "n", "s", "str"}
SHEET_STATES = {"visible", "hidden", "veryHidden"}
MAX_SHEET_ID = 65_534


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


def _single_child(node: Element, tag: str, error_message: str) -> Element | None:
    children = node.iterfind(f"{S}{tag}")
    child = next(children, None)
    if next(children, None) is not None:
        raise InvalidPackage(error_message)
    return child


def _cell(node: Element, reference: str, shared: tuple[str, ...]) -> CellObservation:
    kind = node.get("t", "n")
    if kind not in CELL_TYPES:
        raise InvalidPackage("unsupported cell data type")
    formula_node = _single_child(node, "f", "conflicting cell contents")
    value_node = _single_child(node, "v", "conflicting cell contents")
    inline = _single_child(node, "is", "conflicting cell contents")
    if kind == "inlineStr":
        if formula_node is not None or value_node is not None:
            raise InvalidPackage("conflicting cell contents")
    elif inline is not None:
        raise InvalidPackage("conflicting cell contents")
    if (formula_node is not None and len(formula_node)) or (value_node is not None and len(value_node)):
        raise InvalidPackage("invalid cell contents")

    formula = formula_node.text if formula_node is not None else None
    raw = (value_node.text or None) if value_node is not None else None
    formula_state = "literal"
    if formula_node is not None:
        formula_state = "cached_not_recalculated" if raw is not None else "cached_missing"

    display = raw
    numeric = None
    if kind == "inlineStr":
        display = _rich_text(inline) if inline is not None else None
    elif raw is not None:
        if kind == "s":
            display = _shared_value(raw, shared)
        elif kind == "n":
            numeric = _numeric_value(raw)

    return CellObservation(reference, display, numeric, formula, formula_state, kind, node.get("s"))


def _cell_position(raw: str | None, row: int, previous_column: int) -> tuple[int, str]:
    if raw is None:
        column = previous_column + 1
        if column > MAX_COLUMN:
            raise InvalidPackage("cell reference exceeds worksheet bounds")
        return column, f"{column_name(column)}{row}"

    reference_row, column = cell_coordinates(raw)
    if reference_row != row:
        raise InvalidPackage("cell reference is outside its worksheet row")
    return column, raw


def _sheet_cells(
    root: Element, shared: tuple[str, ...], remaining_cells: int,
) -> dict[str, CellObservation]:
    if root.tag != f"{S}worksheet":
        raise InvalidPackage("invalid worksheet part")
    sheet_data = _single_child(root, "sheetData", "duplicate worksheet cell table")
    cells: dict[str, CellObservation] = {}
    if sheet_data is None:
        return cells

    for row, row_element in numbered_rows(sheet_data.iterfind(f"{S}row")):
        column = 0
        for node in row_element.iterfind(f"{S}c"):
            if len(cells) >= remaining_cells:
                raise PackageLimitExceeded("workbook cell count exceeds limit")
            column, reference = _cell_position(node.get("r"), row, column)
            if reference in cells:
                raise InvalidPackage("duplicate cell reference")
            cells[reference] = _cell(node, reference, shared)
    return cells


def _date_system(root: Element) -> str:
    properties = _single_child(root, "workbookPr", "duplicate workbook properties")
    raw = properties.get("date1904", "false") if properties is not None else "false"
    date_1904 = raw.strip(XML_WHITESPACE)
    if date_1904 in {"1", "true"}:
        return "1904"
    if date_1904 in {"0", "false"}:
        return "1900"
    raise InvalidPackage("invalid date1904 value")


def parse_workbook(package: OpcPackage) -> WorkbookObservation:
    root = package.read_xml(WORKBOOK_PART)
    if root.tag != f"{S}workbook":
        raise InvalidPackage("invalid workbook part")
    date_system = _date_system(root)
    sheet_list = _single_child(root, "sheets", "duplicate workbook sheet list")
    relationships = {rel.relationship_id: rel for rel in package.relationships(WORKBOOK_PART)}
    shared = _shared_strings(package, relationships)

    sheets: list[SheetObservation] = []
    sheet_ids: set[int] = set()
    sheet_names: set[str] = set()
    used_relationships: set[str] = set()
    used_parts: set[str] = set()
    remaining_cells = package.limits.max_workbook_cells
    sheet_nodes = sheet_list.iterfind(f"{S}sheet") if sheet_list is not None else ()
    for node in sheet_nodes:
        relationship_id = node.get(f"{R}id", "")
        rel = relationships.get(relationship_id)
        if rel is None or rel.relationship_type != WORKSHEET_REL:
            raise InvalidPackage("invalid worksheet relationship")
        if relationship_id in used_relationships or rel.target in used_parts:
            raise InvalidPackage("duplicate worksheet source")
        used_relationships.add(relationship_id)
        used_parts.add(rel.target)

        sheet_id = positive_index(node.get("sheetId", ""), MAX_SHEET_ID, "invalid worksheet metadata")
        name = node.get("name", "")
        folded_name = name.casefold()
        state = node.get("state", "visible")
        if (
            sheet_id in sheet_ids or folded_name in sheet_names or state not in SHEET_STATES
            or not name or len(name) > 31 or name.startswith("'") or name.endswith("'")
            or any(character in "[]:*?/\\" or ord(character) < 32 for character in name)
        ):
            raise InvalidPackage("invalid worksheet metadata")
        sheet_ids.add(sheet_id)
        sheet_names.add(folded_name)
        sheet_root = package.read_xml(rel.target)
        cells = _sheet_cells(sheet_root, shared, remaining_cells)
        remaining_cells -= len(cells)
        sheet = SheetObservation(
            stable_id=f"sheet-{sheet_id}",
            relationship_id=relationship_id,
            name=name,
            state=state,
            part_name=rel.target,
            cells=cells,
        )
        sheets.append(parse_layout(package, sheet, sheet_root))
    return WorkbookObservation(package.source_hash(), date_system, tuple(sheets))
