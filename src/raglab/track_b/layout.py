"""Read passive worksheet layout and table metadata."""

from dataclasses import replace
from itertools import accumulate
from unicodedata import normalize
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
MAX_TABLE_COUNT = 4_294_967_295
TABLE_REL_ID = f"{{{REL_TYPE}}}id"


class TableOwnership:
    def __init__(self) -> None:
        self.targets: set[str] = set()
        self.ids: set[int] = set()
        self.names: set[str] = set()

    def claim_target(self, target: str) -> None:
        if target in self.targets:
            raise InvalidPackage("duplicate table target")
        self.targets.add(target)

    def claim_identity(self, table_id: int, name: str, display_name: str) -> None:
        names = {normalize("NFC", value.strip().casefold()) for value in (name, display_name)}
        if table_id in self.ids or "" in names or not self.names.isdisjoint(names):
            raise InvalidPackage("invalid or duplicate table identity")
        self.ids.add(table_id)
        self.names.update(names)


def _singleton(root: Element, tag: str) -> Element | None:
    nodes = root.iterfind(f"{S}{tag}")
    first = next(nodes, None)
    if next(nodes, None) is not None:
        raise InvalidPackage(f"duplicate {tag} element")
    return first


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


def _nonnegative_index(raw: str, maximum: int) -> int:
    normalized = raw.strip(XML_WHITESPACE).removeprefix("+")
    if normalized and all(character == "0" for character in normalized):
        return 0
    return positive_index(raw, maximum, "invalid table count")


def _check_declared_count(node: Element, actual_count: int) -> None:
    count = node.get("count")
    if count is not None and _nonnegative_index(count, MAX_TABLE_COUNT) != actual_count:
        raise InvalidPackage("table declaration count mismatch")


def _tables(
    package: OpcPackage, part: str, root: Element, ownership: TableOwnership,
) -> tuple[TableObservation, ...]:
    table_parts = _singleton(root, "tableParts")
    relationships = {rel.relationship_id: rel for rel in package.relationships(part)}
    if table_parts is None:
        return ()
    entries = table_parts.findall(f"{S}tablePart")
    _check_declared_count(table_parts, len(entries))
    tables: list[TableObservation] = []
    for node in entries:
        rel = relationships.get(node.get(TABLE_REL_ID, ""))
        if rel is None or rel.relationship_type != TABLE_REL:
            raise InvalidPackage("invalid table relationship")
        ownership.claim_target(rel.target)
        table = package.read_xml(rel.target)
        if table.tag != f"{S}table":
            raise InvalidPackage("invalid table part")
        table_id = table.get("id", "")
        numeric_id = positive_index(table_id, MAX_TABLE_COUNT, "invalid table ID")
        display_name = table.get("displayName", "")
        name = table.get("name", display_name)
        ownership.claim_identity(numeric_id, name, display_name)
        reference = table.get("ref", "")
        top, left, bottom, right = range_coordinates(reference)
        columns = _singleton(table, "tableColumns")
        if columns is not None:
            actual_count = sum(1 for _ in columns.iterfind(f"{S}tableColumn"))
            _check_declared_count(columns, actual_count)
            if actual_count != right - left + 1:
                raise InvalidPackage("table column count differs from range width")
        height = bottom - top + 1
        header = _nonnegative_index(table.get("headerRowCount", "1"), height)
        totals = _nonnegative_index(table.get("totalsRowCount", "0"), height)
        if header + totals > height:
            raise InvalidPackage("table rows exceed table range")
        tables.append(TableObservation(table_id, name, display_name, reference, header, totals))
    return tuple(tables)


def parse_layout(
    package: OpcPackage, sheet: SheetObservation, root: Element, ownership: TableOwnership | None = None,
) -> SheetObservation:
    if ownership is None:
        ownership = TableOwnership()
    filter_node = _singleton(root, "autoFilter")
    return replace(
        sheet,
        merged_ranges=tuple(_reference(node) for node in root.iterfind(f"{S}mergeCells/{S}mergeCell")),
        hidden_rows=frozenset(
            number for number, node in numbered_rows(root.iterfind(f"{S}sheetData/{S}row")) if _is_hidden(node)
        ),
        hidden_columns=_hidden_columns(root),
        auto_filter_range=_reference(filter_node) if filter_node is not None else None,
        tables=_tables(package, sheet.part_name, root, ownership),
    )
