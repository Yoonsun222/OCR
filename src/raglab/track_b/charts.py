"""Read chart titles and series references from native worksheet drawings."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree.ElementTree import Element

from .coordinates import XML_WHITESPACE, positive_index
from .errors import InvalidPackage
from .models import ChartObservation, ChartSeriesObservation, SheetObservation, WorkbookObservation
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


def _one(parent: Element, tag: str) -> Element | None:
    children = parent.iterfind(f"{C}{tag}")
    first = next(children, None)
    if next(children, None) is not None:
        raise InvalidPackage(f"duplicate chart {tag}")
    return first


def _choice(parent: Element, label: str, *tags: str) -> Element | None:
    choices = [child for tag in tags if (child := _one(parent, tag)) is not None]
    if len(choices) > 1:
        raise InvalidPackage(f"conflicting chart {label}")
    return choices[0] if choices else None


def _series_number(node: Element, tag: str) -> int:
    child = _one(node, tag)
    text = child.get("val", "") if child is not None else ""
    digits = text.strip(XML_WHITESPACE).removeprefix("+")
    if not digits or not digits.isascii() or not digits.isdecimal():
        raise InvalidPackage("invalid chart series index or order")
    significant = digits.lstrip("0") or "0"
    if len(significant) > 10:
        raise InvalidPackage("invalid chart series index or order")
    number = int(significant)
    if number > 2**32 - 1:
        raise InvalidPackage("invalid chart series index or order")
    return number


def _scalar(node: Element, tag: str) -> str | None:
    element = _one(node, tag)
    if element is None:
        return None
    if len(element):
        raise InvalidPackage(f"invalid chart {tag}")
    return element.text


def _label(node: Element) -> tuple[str | None, str | None]:
    tx = _one(node, "tx")
    if tx is None:
        return None, None
    content = _choice(tx, "label", "rich", "strRef", "v")
    if content is None:
        return None, None
    if content.tag == f"{C}rich":
        return "".join(t.text or "" for t in content.iter(f"{A}t")) or None, None
    if content.tag == f"{C}v":
        return _scalar(tx, "v"), None
    formula = _scalar(content, "f")
    cache = _one(content, "strCache")
    if cache is None:
        return None, formula
    points = cache.iterfind(f"{C}pt")
    point = next(points, None)
    if next(points, None) is not None:
        raise InvalidPackage("ambiguous chart label cache")
    return _scalar(point, "v") if point is not None else None, formula


def _source_formula(
    series: Element, label: str, containers: tuple[str, ...], *references: str,
) -> str | None:
    container = _choice(series, label, *containers)
    if container is None:
        return None
    reference = _choice(container, label, *references)
    if reference is None or reference.tag.endswith("Lit"):
        return None
    return _scalar(reference, "f")


def _series(node: Element) -> ChartSeriesObservation:
    legend_text, title_formula = _label(node)
    return ChartSeriesObservation(
        index=_series_number(node, "idx"),
        order=_series_number(node, "order"),
        legend_text=legend_text,
        title_formula=title_formula,
        category_formula=_source_formula(
            node, "category", ("cat", "xVal"),
            "strRef", "numRef", "multiLvlStrRef", "numLit", "strLit",
        ),
        value_formula=_source_formula(node, "value", ("val", "yVal"), "numRef", "numLit"),
    )


def _drawing_charts(package: OpcPackage, drawing_part: str) -> tuple[str, ...]:
    drawing = _read_part(package, drawing_part, f"{XDR}wsDr")
    relationships = {rel.relationship_id: rel for rel in package.relationships(drawing_part)}
    anchors = {f"{XDR}{kind}Anchor" for kind in ("absolute", "oneCell", "twoCell")}
    graphic_path = f"{XDR}graphicFrame/{A}graphic/{A}graphicData"
    targets: list[str] = []
    for anchor in drawing:
        if anchor.tag not in anchors:
            continue
        for graphic in anchor.iterfind(graphic_path):
            if graphic.get("uri") == CHART_NS and (chart := _one(graphic, "chart")) is not None:
                targets.append(_target(chart, relationships, "chart"))
    return tuple(targets)


def _drawing_source(package: OpcPackage, sheet_part: str) -> str | None:
    root = _read_part(package, sheet_part, f"{S}worksheet")
    drawings = root.iterfind(f"{S}drawing")
    first = next(drawings, None)
    if next(drawings, None) is not None:
        raise InvalidPackage("duplicate worksheet drawing")
    if first is None:
        return None
    relationships = {rel.relationship_id: rel for rel in package.relationships(sheet_part)}
    return _target(first, relationships, "drawing")


@dataclass(frozen=True)
class _WorksheetCharts:
    stable_id: str
    name: str
    sheet_part: str
    chart_parts: tuple[str, ...]


def _workbook_chart_parts(package: OpcPackage) -> dict[str, _WorksheetCharts]:
    workbook = _read_part(package, WORKBOOK_PART, f"{S}workbook")
    relationships = {rel.relationship_id: rel for rel in package.relationships(WORKBOOK_PART)}
    worksheet_parts: set[str] = set()
    drawing_parts: set[str] = set()
    chart_parts: set[str] = set()
    ownership: dict[str, _WorksheetCharts] = {}
    for entry in workbook.iterfind(f"{S}sheets/{S}sheet"):
        part = _target(entry, relationships, "worksheet")
        if part in worksheet_parts:
            raise InvalidPackage("duplicate worksheet source")
        worksheet_parts.add(part)
        drawing = _drawing_source(package, part)
        charts: tuple[str, ...] = ()
        if drawing is not None:
            if drawing in drawing_parts:
                raise InvalidPackage("duplicate workbook drawing source")
            drawing_parts.add(drawing)
            charts = _drawing_charts(package, drawing)
            for chart in charts:
                if chart in chart_parts:
                    raise InvalidPackage("duplicate workbook chart source")
                chart_parts.add(chart)
        error = "invalid chart worksheet identity"
        sheet_id = positive_index(entry.get("sheetId", ""), MAX_SHEET_ID, error)
        ownership[entry.attrib[REL_ID]] = _WorksheetCharts(
            f"sheet-{sheet_id}", entry.get("name", ""), part, charts,
        )
    return ownership


def _observe_chart(
    package: OpcPackage, part: str, ordinal: int, sheet: SheetObservation, source_hash: str,
) -> ChartObservation | None:
    root = _read_part(package, part, f"{C}chartSpace")
    package.relationships(part)
    chart = _one(root, "chart")
    plot_area = _one(chart, "plotArea") if chart is not None else None
    if plot_area is None:
        raise InvalidPackage("missing chart plot area")
    plot_groups = (
        node for node in plot_area if node.tag.startswith(C) and node.tag.endswith("Chart")
    )
    chart_type = next(plot_groups, None)
    if next(plot_groups, None) is not None:
        raise InvalidPackage("unsupported multiple chart plot groups")
    if chart_type is None:
        return None
    title = _one(chart, "title")
    title_text, _ = _label(title) if title is not None else (None, None)
    series = tuple(_series(node) for node in chart_type.iterfind(f"{C}ser"))
    if len({item.order for item in series}) != len(series):
        raise InvalidPackage("duplicate chart series order")
    if len({item.index for item in series}) != len(series):
        raise InvalidPackage("duplicate chart series index")
    return ChartObservation(
        chart_id=f"chart-{ordinal}",
        sheet_stable_id=sheet.stable_id,
        part_name=part,
        chart_type=chart_type.tag.removeprefix(C),
        title=title_text,
        series=tuple(sorted(series, key=lambda item: item.order)),
        source_hash=source_hash,
        sheet_part_name=sheet.part_name,
    )


def _sheet_charts(
    package: OpcPackage,
    sheet: SheetObservation,
    source_hash: str,
    source: _WorksheetCharts | None,
) -> tuple[ChartObservation, ...]:
    if (
        source is None
        or sheet.stable_id != source.stable_id
        or sheet.name != source.name
        or sheet.part_name != source.sheet_part
    ):
        raise InvalidPackage("invalid chart worksheet identity")
    observations: list[ChartObservation] = []
    for ordinal, part in enumerate(source.chart_parts, start=1):
        observation = _observe_chart(package, part, ordinal, sheet, source_hash)
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


def parse_charts(package: OpcPackage, sheet: SheetObservation) -> tuple[ChartObservation, ...]:
    """Audit workbook-wide part ownership, then observe the requested sheet's charts."""
    source_hash = package.source_hash()
    if sheet.source_hash != source_hash:
        raise InvalidPackage("chart worksheet source hash mismatch")
    ownership = _workbook_chart_parts(package)
    return _sheet_charts(package, sheet, source_hash, ownership.get(sheet.relationship_id))


def parse_workbook_charts(
    package: OpcPackage, workbook: WorkbookObservation,
) -> tuple[ChartObservation, ...]:
    """Observe every sheet's charts with one archive hash and ownership audit."""
    source_hash = package.source_hash()
    if workbook.source_hash != source_hash or any(
        sheet.source_hash != source_hash for sheet in workbook.sheets
    ):
        raise InvalidPackage("chart workbook source hash mismatch")
    ownership = _workbook_chart_parts(package)
    if len(workbook.sheets) != len(ownership) or {
        sheet.relationship_id for sheet in workbook.sheets
    } != ownership.keys():
        raise InvalidPackage("invalid chart workbook worksheet identities")
    return tuple(
        chart
        for sheet in workbook.sheets
        for chart in _sheet_charts(package, sheet, source_hash, ownership[sheet.relationship_id])
    )
