from dataclasses import replace
from pathlib import Path

import pytest

from raglab.track_b.charts import parse_charts
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
    assert chart.source_hash == workbook.source_hash
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
