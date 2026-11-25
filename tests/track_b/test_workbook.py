"""Workbook observations use only values stored in the local package."""

from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from raglab.track_b.errors import InvalidPackage, PackageLimitExceeded, UnsafeRelationship
from raglab.track_b.opc import OpcLimits, OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_native_workbook, write_xlsx


def test_parses_shared_string_number_and_formula_cache(tmp_path: Path) -> None:
    path = write_native_workbook(tmp_path / "book.xlsx")
    with OpcPackage.open(path) as package:
        workbook = parse_workbook(package)
    assert workbook.source_hash == sha256(path.read_bytes()).hexdigest()
    sheet = workbook.sheets[0]
    assert sheet.stable_id == "sheet-1"
    assert sheet.cells["A1"].display_value == "매출"
    assert sheet.cells["B2"].numeric_value == Decimal("12.5")
    assert sheet.cells["C2"].formula == "SUM(B2:B3)"
    assert sheet.cells["C2"].display_value == "20"
    assert sheet.cells["C2"].formula_state == "cached_not_recalculated"


def test_missing_and_shared_follower_formula_caches(tmp_path: Path) -> None:
    with OpcPackage.open(write_native_workbook(tmp_path / "book.xlsx")) as package:
        sheet = parse_workbook(package).sheets[0]
    assert sheet.cells["D2"].formula == "B2*2"
    assert sheet.cells["D2"].display_value is None
    assert sheet.cells["D2"].formula_state == "cached_missing"
    assert sheet.cells["C3"].formula is None
    assert sheet.cells["C3"].display_value == "0"
    assert sheet.cells["C3"].formula_state == "cached_not_recalculated"
    assert sheet.cells["D3"].display_value is None
    assert sheet.cells["D3"].formula_state == "cached_missing"


def test_observes_all_sheets_and_preserves_cell_types(tmp_path: Path) -> None:
    with OpcPackage.open(write_native_workbook(tmp_path / "book.xlsx")) as package:
        workbook = parse_workbook(package)
    assert workbook.date_system == "1904"
    first, second = workbook.sheets
    assert (first.name, first.state) == ("Data", "visible")
    assert (second.stable_id, second.name, second.state) == ("sheet-9", "Summary", "veryHidden")
    assert second.part_name == "xl/worksheets/report.xml"
    assert second.cells["A1"].display_value == " Total Sales "
    assert second.cells["A2"].display_value == "45"
    assert second.cells["A2"].numeric_value is None
    assert second.cells["B2"].display_value == "1"
    assert second.cells["B2"].numeric_value is None
    assert second.cells["C2"].display_value == "#VALUE!"
    assert second.cells["C2"].numeric_value is None
    assert second.cells["D2"].style_id == "2"
    assert second.cells["D2"].numeric_value == Decimal("100")
    with pytest.raises(TypeError):
        second.cells["A1"] = second.cells["A2"]


def test_invalid_shared_string_indices_fail_safely(tmp_path: Path) -> None:
    sheet = b"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <sheetData><row r="1"><c r="A1" t="s"><v>-1</v></c></row></sheetData>
        </worksheet>"""
    path = write_native_workbook(tmp_path / "book.xlsx", {"xl/worksheets/data.xml": sheet})
    with OpcPackage.open(path) as package:
        with pytest.raises(InvalidPackage, match="shared string index"):
            parse_workbook(package)


def test_workbook_relationships_reject_external_targets(tmp_path: Path) -> None:
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
        <Relationship Id="rId1" Type="urn:test" Target="https://example.invalid" TargetMode="External"/>
        </Relationships>"""
    path = write_native_workbook(tmp_path / "book.xlsx", {"xl/_rels/workbook.xml.rels": relationships})
    with OpcPackage.open(path) as package:
        with pytest.raises(UnsafeRelationship, match="External"):
            parse_workbook(package)


def test_hash_uses_the_open_package_and_requires_live_handle(tmp_path: Path, monkeypatch) -> None:
    path = write_xlsx(tmp_path / "book.xlsx")
    expected = sha256(path.read_bytes()).hexdigest()
    with OpcPackage.open(path) as package:
        monkeypatch.setattr(package, "path", tmp_path / "unrelated.xlsx")
        assert package.source_hash() == expected
        assert package.read_xml("xl/workbook.xml").tag.endswith("workbook")
    with pytest.raises(InvalidPackage, match="closed"):
        package.source_hash()


def test_limits_physical_container_even_with_small_uncompressed_payload(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "book.xlsx")
    with pytest.raises(PackageLimitExceeded, match="physical container"):
        OpcPackage.open(path, OpcLimits(max_container_bytes=path.stat().st_size - 1))
