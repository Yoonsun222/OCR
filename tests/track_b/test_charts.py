from dataclasses import replace
from pathlib import Path

import pytest

from raglab.track_b.charts import parse_charts, parse_workbook_charts
from raglab.track_b.errors import InvalidPackage
from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_chart_workbook


def test_links_chart_series_legend_and_source_ranges(tmp_path: Path) -> None:
    with OpcPackage.open(write_chart_workbook(tmp_path / "chart.xlsx")) as package:
        workbook = parse_workbook(package)
        sheet = workbook.sheets[0]
        (chart,) = parse_charts(package, sheet)

    assert chart.chart_id == "chart-1"
    assert chart.source_hash == sheet.source_hash == workbook.source_hash
    assert chart.sheet_part_name == sheet.part_name
    assert chart.sheet_stable_id == sheet.stable_id
    assert chart.part_name == "xl/charts/chart1.xml"
    assert chart.title == "분기 매출"
    assert chart.chart_type == "barChart"
    first, second = chart.series
    assert (first.index, first.order) == (1, 0)
    assert first.legend_text == "제품 A"
    assert first.title_formula == "'Data'!$B$1"
    assert first.category_formula == "'Data'!$A$2:$A$5"
    assert first.value_formula == "'Data'!$B$2:$B$5"
    assert (second.index, second.order) == (0, 1)
    assert second.legend_text == "제품 B"
    assert second.title_formula is None
    assert second.category_formula == "'Data'!$A$2:$A$5"
    assert second.value_formula == "'Data'!$C$2:$C$5"


def test_unlinked_chart_parts_are_ignored(tmp_path: Path) -> None:
    sheet = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
    source = write_chart_workbook(tmp_path / "orphan.xlsx", {"xl/worksheets/sheet1.xml": sheet})
    with OpcPackage.open(source) as package:
        assert parse_charts(package, parse_workbook(package).sheets[0]) == ()


def test_chart_relationship_has_to_identify_a_chart(tmp_path: Path) -> None:
    wrong_type = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdChart" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        Target="../charts/chart1.xml"/>
    </Relationships>"""
    source = write_chart_workbook(
        tmp_path / "wrong-type.xlsx", {"xl/drawings/_rels/drawing1.xml.rels": wrong_type},
    )
    with OpcPackage.open(source) as package:
        workbook = parse_workbook(package)
        sheet = workbook.sheets[0]
        with pytest.raises(InvalidPackage, match="invalid chart relationship"):
            parse_charts(package, sheet)


def test_cannot_relabel_the_chart_owner(tmp_path: Path) -> None:
    with OpcPackage.open(write_chart_workbook(tmp_path / "chart.xlsx")) as package:
        sheet = parse_workbook(package).sheets[0]
        with pytest.raises(InvalidPackage, match="invalid chart worksheet identity"):
            parse_charts(package, replace(sheet, stable_id="sheet-99"))


@pytest.mark.parametrize(
    ("original", "replacement", "message"),
    [
        (b"</c:barChart>", b"</c:barChart><c:lineChart/>", "multiple chart plot groups"),
        (b"</c:title>", b"</c:title><c:title/>", "duplicate chart title"),
        (b'<c:idx val="0"/>', b'<c:idx val="0"/><c:idx val="5"/>', "duplicate chart idx"),
        (b"</c:cat>", b"<c:multiLvlStrRef/></c:cat>", "conflicting chart category"),
        (b"</c:strCache>", b'<c:pt idx="1"><c:v>Other</c:v></c:pt></c:strCache>', "ambiguous chart label cache"),
    ],
)
def test_rejects_ambiguous_chart_metadata(
    tmp_path: Path, original: bytes, replacement: bytes, message: str,
) -> None:
    part = "xl/charts/chart1.xml"
    with OpcPackage.open(write_chart_workbook(tmp_path / "original.xlsx")) as package:
        chart_xml = package.read_bytes(part)
    edited = chart_xml.replace(original, replacement, 1)
    assert edited != chart_xml
    source = write_chart_workbook(tmp_path / "ambiguous.xlsx", {part: edited})
    with OpcPackage.open(source) as package:
        sheet = parse_workbook(package).sheets[0]
        with pytest.raises(InvalidPackage, match=message):
            parse_charts(package, sheet)


@pytest.mark.parametrize("has_drawing", [True, False])
def test_rejects_sheet_from_another_archive(tmp_path: Path, has_drawing: bool) -> None:
    other_parts = {"extra.xml": b"<extra/>"}
    if not has_drawing:
        other_parts["xl/worksheets/sheet1.xml"] = (
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
        )
    with OpcPackage.open(write_chart_workbook(tmp_path / "original.xlsx")) as package:
        sheet = parse_workbook(package).sheets[0]
    with OpcPackage.open(write_chart_workbook(tmp_path / "other.xlsx", other_parts)) as package:
        with pytest.raises(InvalidPackage, match="chart worksheet source hash mismatch"):
            parse_charts(package, sheet)


@pytest.mark.parametrize("shared", ["drawing", "chart"])
def test_rejects_shared_targets_across_worksheets(tmp_path: Path, shared: str) -> None:
    drawing = "xl/drawings/drawing1.xml"
    drawing_rels = "xl/drawings/_rels/drawing1.xml.rels"
    sheet_part = "xl/worksheets/sheet1.xml"
    sheet_rels = "xl/worksheets/_rels/sheet1.xml.rels"
    workbook = "xl/workbook.xml"
    workbook_rels = "xl/_rels/workbook.xml.rels"
    source = write_chart_workbook(tmp_path / "original.xlsx")
    with OpcPackage.open(source) as package:
        parts = {
            workbook: package.read_bytes(workbook).replace(
                b"</sheets>", b'<sheet name="Extra" sheetId="2" r:id="rId2"/></sheets>',
            ),
            workbook_rels: package.read_bytes(workbook_rels).replace(
                b"</Relationships>",
                b'<Relationship Id="rId2" Target="worksheets/sheet2.xml" '
                b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                b"</Relationships>",
            ),
            sheet_part.replace("sheet1", "sheet2"): package.read_bytes(sheet_part),
            sheet_rels.replace("sheet1", "sheet2"): package.read_bytes(sheet_rels),
        }
        if shared == "chart":
            parts[sheet_rels.replace("sheet1", "sheet2")] = package.read_bytes(sheet_rels).replace(
                b"drawing1.xml", b"drawing2.xml",
            )
            parts[drawing.replace("drawing1", "drawing2")] = package.read_bytes(drawing)
            parts[drawing_rels.replace("drawing1", "drawing2")] = package.read_bytes(drawing_rels)
    with OpcPackage.open(write_chart_workbook(tmp_path / "shared.xlsx", parts)) as package:
        sheets = parse_workbook(package).sheets
        for sheet in sheets:
            with pytest.raises(InvalidPackage, match=f"duplicate workbook {shared} source"):
                parse_charts(package, sheet)


def test_workbook_charts_match_single_sheet_observations(tmp_path: Path) -> None:
    with OpcPackage.open(write_chart_workbook(tmp_path / "chart.xlsx")) as package:
        workbook = parse_workbook(package)
        assert parse_workbook_charts(package, workbook) == parse_charts(package, workbook.sheets[0])
