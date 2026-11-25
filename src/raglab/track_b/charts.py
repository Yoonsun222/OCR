"""Read chart titles and series references from native worksheet drawings."""

from __future__ import annotations

from collections.abc import Iterator
from xml.etree.ElementTree import Element

from .coordinates import XML_WHITESPACE, positive_index
from .errors import InvalidPackage
from .models import ChartObservation, ChartSeriesObservation, SheetObservation
from .opc import OpcPackage, Relationship
from .workbook import MAX_SHEET_ID, REL_TYPE, S, WORKBOOK_PART

REL_ID = f"{{{REL_TYPE}}}id"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
C = f"{{{CHART_NS}}}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"


def _target(node: Element, relationships: dict[str, Relationship], kind: str) -> str:
    relationship = relationships.get(node.get(REL_ID, ""))
    if relationship is None or relationship.relationship_type != f"{REL_TYPE}/{kind}":
        raise InvalidPackage(f"invalid {kind} relationship")
    return relationship.target


def _read_part(package: OpcPackage, part: str, tag: str) -> Element:
    root = package.read_xml(part)
    if root.tag != tag:
        raise InvalidPackage("invalid worksheet drawing or chart part")
    return root


def _verify_sheet(package: OpcPackage, sheet: SheetObservation) -> None:
    workbook = _read_part(package, WORKBOOK_PART, f"{S}workbook")
    relationships = {rel.relationship_id: rel for rel in package.relationships(WORKBOOK_PART)}
    entries = [
        entry for entry in workbook.iterfind(f"{S}sheets/{S}sheet")
        if entry.get(REL_ID) == sheet.relationship_id
    ]
    error = "invalid chart worksheet identity"
    if len(entries) != 1:
        raise InvalidPackage(error)
    entry = entries[0]
    sheet_id = positive_index(entry.get("sheetId", ""), MAX_SHEET_ID, error)
    if (
        sheet.stable_id != f"sheet-{sheet_id}"
        or entry.get("name") != sheet.name
        or _target(entry, relationships, "worksheet") != sheet.part_name
    ):
        raise InvalidPackage(error)


def _series_number(node: Element, tag: str) -> int:
    child = node.find(f"{C}{tag}")
    text = child.get("val", "") if child is not None else ""
    digits = text.strip(XML_WHITESPACE).removeprefix("+")
    if not digits or not digits.isascii() or not digits.isdecimal():
        raise InvalidPackage("invalid chart series index or order")
    significant = digits.lstrip("0") or "0"
    if len(significant) > 10 or int(significant) > 2**32 - 1:
        raise InvalidPackage("invalid chart series index or order")
    return int(significant)


def _label(node: Element | None) -> str | None:
    if node is None:
        return None
    for path in (f"{C}rich//{A}t", f"{C}strRef/{C}strCache/{C}pt/{C}v", f"{C}v"):
        value = "".join(child.text or "" for child in node.iterfind(path))
        if value:
            return value
    return None


def _series(node: Element) -> ChartSeriesObservation:
    return ChartSeriesObservation(
        index=_series_number(node, "idx"),
        order=_series_number(node, "order"),
        legend_text=_label(node.find(f"{C}tx")),
        title_formula=node.findtext(f"{C}tx/{C}strRef/{C}f") or None,
        category_formula=(
            node.findtext(f"{C}cat/{C}strRef/{C}f")
            or node.findtext(f"{C}cat/{C}numRef/{C}f")
            or node.findtext(f"{C}cat/{C}multiLvlStrRef/{C}f")
            or node.findtext(f"{C}xVal/{C}numRef/{C}f")
            or node.findtext(f"{C}xVal/{C}strRef/{C}f")
            or None
        ),
        value_formula=(
            node.findtext(f"{C}val/{C}numRef/{C}f")
            or node.findtext(f"{C}yVal/{C}numRef/{C}f")
            or None
        ),
    )


def _chart_graphics(drawing: Element) -> Iterator[Element]:
    anchors = {f"{XDR}{kind}Anchor" for kind in ("absolute", "oneCell", "twoCell")}
    graphic_path = f"{XDR}graphicFrame/{A}graphic/{A}graphicData"
    for anchor in drawing:
        if anchor.tag in anchors:
            for graphic in anchor.iterfind(graphic_path):
                if graphic.get("uri") == CHART_NS:
                    yield from graphic.iterfind(f"{C}chart")


def parse_charts(package: OpcPackage, sheet: SheetObservation) -> tuple[ChartObservation, ...]:
    """Read chart observations whose parts are explicitly linked from the worksheet."""
    root = _read_part(package, sheet.part_name, f"{S}worksheet")
    drawings = root.findall(f"{S}drawing")
    if not drawings:
        return ()
    if len(drawings) != 1:
        raise InvalidPackage("duplicate worksheet drawing")

    _verify_sheet(package, sheet)
    sheet_rels = {rel.relationship_id: rel for rel in package.relationships(sheet.part_name)}
    drawing_part = _target(drawings[0], sheet_rels, "drawing")
    drawing = _read_part(package, drawing_part, f"{XDR}wsDr")
    drawing_rels = {rel.relationship_id: rel for rel in package.relationships(drawing_part)}
    observations: list[ChartObservation] = []
    used_parts: set[str] = set()
    source_hash: str | None = None
    for ordinal, graphic in enumerate(_chart_graphics(drawing), start=1):
        part = _target(graphic, drawing_rels, "chart")
        if part in used_parts:
            raise InvalidPackage("duplicate worksheet chart source")
        used_parts.add(part)
        chart = _read_part(package, part, f"{C}chartSpace")
        package.relationships(part)
        plot_area = chart.find(f"{C}chart/{C}plotArea")
        if plot_area is None:
            raise InvalidPackage("missing chart plot area")
        chart_type = next(
            (node for node in plot_area if node.tag.startswith(C) and node.tag.endswith("Chart")),
            None,
        )
        if chart_type is None:
            continue
        series = tuple(_series(node) for node in chart_type.iterfind(f"{C}ser"))
        if len({item.order for item in series}) != len(series):
            raise InvalidPackage("duplicate chart series order")
        if len({item.index for item in series}) != len(series):
            raise InvalidPackage("duplicate chart series index")
        if source_hash is None:
            source_hash = package.source_hash()
        observations.append(ChartObservation(
            chart_id=f"chart-{ordinal}",
            sheet_stable_id=sheet.stable_id,
            part_name=part,
            chart_type=chart_type.tag.removeprefix(C),
            title=_label(chart.find(f"{C}chart/{C}title/{C}tx")),
            series=tuple(sorted(series, key=lambda item: item.order)),
            source_hash=source_hash,
            sheet_part_name=sheet.part_name,
        ))
    return tuple(observations)
