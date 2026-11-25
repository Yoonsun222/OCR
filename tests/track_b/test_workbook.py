"""Workbook observations use only values stored in the local package."""

from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from raglab.track_b.errors import InvalidPackage, PackageLimitExceeded, UnsafeRelationship
from raglab.track_b.models import SheetObservation
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


WORKBOOK_XML = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
    {properties}<sheets>{sheets}</sheets></workbook>"""
WORKSHEET_XML = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
    <sheetData>{rows}</sheetData></worksheet>"""


def _parse_first_sheet(tmp_path: Path, rows: str) -> SheetObservation:
    path = write_native_workbook(tmp_path / "book.xlsx", {
        "xl/worksheets/data.xml": WORKSHEET_XML.format(rows=rows).encode(),
    })
    with OpcPackage.open(path) as package:
        return parse_workbook(package).sheets[0]


@pytest.mark.parametrize(("second_relationship", "duplicate_target"), [("rId1", False), ("rId2", True)])
def test_rejects_duplicate_worksheet_sources(
    tmp_path: Path, second_relationship: str, duplicate_target: bool,
) -> None:
    parts = {"xl/workbook.xml": WORKBOOK_XML.format(
        properties="", sheets=f'<sheet name="First" sheetId="1" r:id="rId1"/>'
        f'<sheet name="Second" sheetId="2" r:id="{second_relationship}"/>',
    ).encode()}
    if duplicate_target:
        parts["xl/_rels/workbook.xml.rels"] = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
                Target="worksheets/data.xml"/>
            <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
                Target="/xl/worksheets/data.xml"/>
            <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
                Target="strings.xml"/>
            </Relationships>"""
    path = write_native_workbook(tmp_path / "book.xlsx", parts)
    with OpcPackage.open(path) as package:
        with pytest.raises(InvalidPackage, match="duplicate worksheet source"):
            parse_workbook(package)


def test_cell_budget_is_shared_across_sheets(tmp_path: Path) -> None:
    path = write_native_workbook(tmp_path / "book.xlsx")
    with OpcPackage.open(path, OpcLimits(max_workbook_cells=12)) as package:
        assert sum(len(sheet.cells) for sheet in parse_workbook(package).sheets) == 12
    with OpcPackage.open(path, OpcLimits(max_workbook_cells=11)) as package:
        with pytest.raises(PackageLimitExceeded, match="workbook cell count"):
            parse_workbook(package)


@pytest.mark.parametrize("rows", [
    '<row r="1"><c r="XFE1"/></row>',
    '<row r="1048577"><c r="A1048577"/></row>',
    '<row r="1"><c r="A2"/></row>',
    '<row r="1048576"><c r="XFD1048576"/><c/></row>',
    '<row r="0"/>',
    '<row r="1"><c r=""/></row>',
])
def test_rejects_out_of_bounds_or_inconsistent_coordinates(tmp_path: Path, rows: str) -> None:
    with pytest.raises(InvalidPackage, match="row|cell reference"):
        _parse_first_sheet(tmp_path, rows)


def test_defaults_omitted_coordinates_and_accepts_excel_grid_edge(tmp_path: Path) -> None:
    sheet = _parse_first_sheet(tmp_path, """
        <row><c><v>5</v></c><c r="C1"><v>6</v></c><c/></row>
        <row><c/></row>
        <row r="1048576"><c r="XFD1048576"/></row>
    """)
    assert list(sheet.cells) == ["A1", "C1", "D1", "A2", "XFD1048576"]
    assert sheet.cells["D1"].display_value is None
    assert sheet.cells["A1"].data_type == "n"


@pytest.mark.parametrize(("properties", "sheet"), [
    ('<workbookPr date1904="yes"/>', '<sheet name="Data" sheetId="1" r:id="rId1"/>'),
    ('<workbookPr date1904=""/>', '<sheet name="Data" sheetId="1" r:id="rId1"/>'),
    ('', '<sheet name="Data" sheetId="0" r:id="rId1"/>'),
    ('', '<sheet name="Data" sheetId="65535" r:id="rId1"/>'),
    ('', '<sheet name="Data" sheetId="one" r:id="rId1"/>'),
    ('', '<sheet name="Data" r:id="rId1"/>'),
    ('', '<sheet name="Data" sheetId="1" r:id="rId1" state="invalid"/>'),
    ('', '<sheet name="Invalid/Name" sheetId="1" r:id="rId1"/>'),
])
def test_rejects_invalid_explicit_workbook_metadata(
    tmp_path: Path, properties: str, sheet: str,
) -> None:
    path = write_native_workbook(tmp_path / "book.xlsx", {
        "xl/workbook.xml": WORKBOOK_XML.format(properties=properties, sheets=sheet).encode(),
    })
    with OpcPackage.open(path) as package:
        with pytest.raises(InvalidPackage, match="worksheet metadata|date1904"):
            parse_workbook(package)


@pytest.mark.parametrize("cell", [
    '<c t="unsupported"><v>4</v></c>',
    '<c t="n"><v>1</v><v>2</v></c>',
    '<c><f>1</f><f>2</f><v>2</v></c>',
    '<c t="inlineStr"><is><t>One</t></is><is><t>Two</t></is></c>',
    '<c t="inlineStr"><is><t>Text</t></is><v>5</v></c>',
    '<c t="inlineStr"><f>3</f></c>',
    '<c t="n"><is><t>Text</t></is></c>',
])
def test_rejects_unsupported_types_and_conflicting_cell_contents(tmp_path: Path, cell: str) -> None:
    explicit_cell = cell.replace('<c', '<c r="A1"', 1)
    with pytest.raises(InvalidPackage, match="cell data type|cell contents"):
        _parse_first_sheet(tmp_path, f'<row r="1">{explicit_cell}</row>')
