from pathlib import Path

import pytest

from raglab.track_b.errors import InvalidPackage
from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_layout_workbook


def test_preserves_merge_hidden_filter_and_table_metadata(tmp_path: Path) -> None:
    with OpcPackage.open(write_layout_workbook(tmp_path / "layout.xlsx")) as package:
        sheet, plain_sheet = parse_workbook(package).sheets
    assert sheet.merged_ranges == ("A1:B1",)
    assert sheet.hidden_rows == frozenset({4})
    assert sheet.hidden_columns == frozenset({"D", "E"})
    assert sheet.auto_filter_range == "A1:C9"
    assert len(sheet.tables) == 1
    table = sheet.tables[0]
    assert (table.table_id, table.name, table.display_name, table.reference) == (
        "1", "Sales", "Sales_Display", "A1:C9",
    )
    assert (table.header_row_count, table.totals_row_count) == (1, 1)
    assert plain_sheet.tables == ()


@pytest.mark.parametrize("reference", ["XFE1", "A1048577", "B2:A1", "Other!A1:B2"])
def test_rejects_invalid_layout_range(tmp_path: Path, reference: str) -> None:
    worksheet = f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <autoFilter ref="{reference}"/></worksheet>""".encode()
    with OpcPackage.open(write_layout_workbook(
        tmp_path / "layout.xlsx", {"xl/worksheets/data.xml": worksheet},
    )) as package, pytest.raises(InvalidPackage):
        parse_workbook(package)


def test_rejects_wrong_table_relationship_type(tmp_path: Path) -> None:
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdTable" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
       Target="../tables/table1.xml"/></Relationships>"""
    with OpcPackage.open(write_layout_workbook(
        tmp_path / "layout.xlsx", {"xl/worksheets/_rels/data.xml.rels": relationships},
    )) as package, pytest.raises(InvalidPackage):
        parse_workbook(package)


def test_table_display_name_is_used_when_name_is_omitted(tmp_path: Path) -> None:
    table = b"""<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      id="1" displayName="Sales_Display" ref="XFC1048575:XFD1048576"/>"""
    with OpcPackage.open(write_layout_workbook(
        tmp_path / "layout.xlsx", {"xl/tables/table1.xml": table},
    )) as package:
        result, = parse_workbook(package).sheets[0].tables
    assert result.name == result.display_name == "Sales_Display"
    assert result.reference == "XFC1048575:XFD1048576"
    assert (result.header_row_count, result.totals_row_count) == (1, 0)


def test_rejects_reused_table_target_across_sheets(tmp_path: Path) -> None:
    worksheet = b"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <tableParts count="1"><tablePart r:id="shared"/></tableParts></worksheet>"""
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="shared" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
       Target="../tables/table1.xml"/></Relationships>"""
    with OpcPackage.open(write_layout_workbook(tmp_path / "layout.xlsx", {
        "xl/worksheets/report.xml": worksheet,
        "xl/worksheets/_rels/report.xml.rels": relationships,
    })) as package, pytest.raises(InvalidPackage):
        parse_workbook(package)


@pytest.mark.parametrize("markers", [
    '<autoFilter ref="A1:B2"/><autoFilter ref="A2:B2"/>',
    "<tableParts/><tableParts/>",
    '<tableParts count="1"/>',
])
def test_rejects_ambiguous_layout_declarations(tmp_path: Path, markers: str) -> None:
    worksheet = f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      {markers}</worksheet>""".encode()
    with OpcPackage.open(write_layout_workbook(
        tmp_path / "layout.xlsx", {"xl/worksheets/data.xml": worksheet},
    )) as package, pytest.raises(InvalidPackage):
        parse_workbook(package)


@pytest.mark.parametrize("table_columns", [
    '<tableColumns count="2"><tableColumn id="1" name="A"/></tableColumns>',
    '<tableColumns count="1"><tableColumn id="1" name="A"/></tableColumns>',
])
def test_rejects_table_column_count_mismatch(tmp_path: Path, table_columns: str) -> None:
    table = f"""<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      id="1" displayName="Sales" ref="A1:B2">{table_columns}</table>""".encode()
    with OpcPackage.open(write_layout_workbook(
        tmp_path / "layout.xlsx", {"xl/tables/table1.xml": table},
    )) as package, pytest.raises(InvalidPackage):
        parse_workbook(package)


@pytest.mark.parametrize(("table_id", "name", "display_name"), [
    ("+001", "Different", "Different_Display"),
    ("2", "sALES", "Different_Display"),
    ("2", "Different", "SALES_DISPLAY"),
    ("2", "Sales_Display", "Different_Display"),
])
def test_rejects_duplicate_table_identity_across_sheets(
    tmp_path: Path, table_id: str, name: str, display_name: str,
) -> None:
    worksheet = b"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <tableParts count="1"><tablePart r:id="second"/></tableParts></worksheet>"""
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="second" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
       Target="../tables/table2.xml"/></Relationships>"""
    table = f"""<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      id="{table_id}" name="{name}" displayName="{display_name}" ref="A1:B2"/>""".encode()
    with OpcPackage.open(write_layout_workbook(tmp_path / "layout.xlsx", {
        "xl/worksheets/report.xml": worksheet,
        "xl/worksheets/_rels/report.xml.rels": relationships,
        "xl/tables/table2.xml": table,
    })) as package, pytest.raises(InvalidPackage, match="duplicate table identity"):
        parse_workbook(package)
