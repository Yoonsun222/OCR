"""Read passive worksheet layout and table metadata."""

from dataclasses import replace
from itertools import accumulate
from xml.etree.ElementTree import Element

from .coordinates import (
    MAX_COLUMN, XML_WHITESPACE, column_name, numbered_rows, positive_index, range_coordinates,
)
from .errors import InvalidPackage
from .models import SheetObservation, TableObservation
from .opc import OpcPackage

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
TABLE_REL = f"{REL_TYPE}/table"
TABLE_REL_ID = f"{{{REL_TYPE}}}id"


def _is_hidden(node: Element) -> bool:
    hidden = node.get("hidden", "false").strip(XML_WHITESPACE)
    if hidden not in {"0", "1", "false", "true"}:
        raise InvalidPackage("invalid worksheet visibility")
    return hidden in {"1", "true"}


def _hidden_columns(root: Element) -> frozenset[str]:
    # Record the edges of each hidden interval so overlapping spans cost O(1) each.
    edges: list[int] = []
    for node in root.iterfind(f"{S}cols/{S}col"):
        first = positive_index(node.get("min", ""), MAX_COLUMN, "invalid worksheet columns")
        last = positive_index(node.get("max", ""), MAX_COLUMN, "invalid worksheet columns")
        if last < first:
            raise InvalidPackage("invalid worksheet column order")
        if _is_hidden(node):
            if not edges:
                edges = [0] * (MAX_COLUMN + 1)
            edges[first - 1] += 1
            edges[last] -= 1
    return frozenset(column_name(index) for index, depth in enumerate(accumulate(edges), 1) if depth)


def _reference(node: Element) -> str:
    reference = node.get("ref", "")
    range_coordinates(reference)
    return reference


def _table_row_count(table: Element, attribute: str, default: str, maximum: int) -> int:
    raw = table.get(attribute, default)
    normalized = raw.strip(XML_WHITESPACE).removeprefix("+")
    if normalized and all(character == "0" for character in normalized):
        return 0
    return positive_index(raw, maximum, "invalid table row count")


def _tables(package: OpcPackage, part: str, root: Element) -> tuple[TableObservation, ...]:
    relationships = {rel.relationship_id: rel for rel in package.relationships(part)}
    tables: list[TableObservation] = []
    targets: set[str] = set()
    table_ids: set[int] = set()
    for node in root.iterfind(f"{S}tableParts/{S}tablePart"):
        rel = relationships.get(node.get(TABLE_REL_ID, ""))
        if rel is None or rel.relationship_type != TABLE_REL or rel.target in targets:
            raise InvalidPackage("invalid or duplicate table relationship")
        targets.add(rel.target)
        table = package.read_xml(rel.target)
        if table.tag != f"{S}table":
            raise InvalidPackage("invalid table part")
        table_id = table.get("id", "")
        numeric_id = positive_index(table_id, 4_294_967_295, "invalid table ID")
        display_name = table.get("displayName", "")
        name = table.get("name", display_name)
        if numeric_id in table_ids or not name.strip() or not display_name.strip():
            raise InvalidPackage("invalid table metadata")
        table_ids.add(numeric_id)
        reference = table.get("ref", "")
        top, _, bottom, _ = range_coordinates(reference)
        height = bottom - top + 1
        header = _table_row_count(table, "headerRowCount", "1", height)
        totals = _table_row_count(table, "totalsRowCount", "0", height)
        if header + totals > height:
            raise InvalidPackage("table rows exceed table range")
        tables.append(TableObservation(table_id, name, display_name, reference, header, totals))
    return tuple(tables)


def parse_layout(package: OpcPackage, sheet: SheetObservation, root: Element) -> SheetObservation:
    filter_node = root.find(f"{S}autoFilter")
    return replace(
        sheet,
        merged_ranges=tuple(_reference(node) for node in root.iterfind(f"{S}mergeCells/{S}mergeCell")),
        hidden_rows=frozenset(
            number for number, node in numbered_rows(root.iterfind(f"{S}sheetData/{S}row")) if _is_hidden(node)
        ),
        hidden_columns=_hidden_columns(root),
        auto_filter_range=_reference(filter_node) if filter_node is not None else None,
        tables=_tables(package, sheet.part_name, root),
    )
