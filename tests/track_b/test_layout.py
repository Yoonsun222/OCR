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
