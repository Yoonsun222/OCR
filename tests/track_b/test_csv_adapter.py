from hashlib import sha256
from pathlib import Path

import pytest

from raglab.track_b.csv_adapter import parse_csv
from raglab.track_b.errors import InvalidPackage, PackageLimitExceeded


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_csv_becomes_one_sheet_with_literal_a1_cells(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "표.csv"
    raw = '품목,수량\r\n"A\r\nB",003\r\n=1+2,\r\n'.encode(encoding)
    path.write_bytes(raw)
    workbook = parse_csv(path)
    assert workbook.sheets[0].source_hash == workbook.source_hash == sha256(raw).hexdigest()
    assert workbook.date_system == "not_applicable"
    sheet, = workbook.sheets
    assert sheet.name == "표"
    assert sheet.cells["A1"].display_value == "품목"
    assert sheet.cells["A2"].display_value == "A\r\nB"
    assert sheet.cells["B2"].display_value == "003"
    assert sheet.cells["B2"].numeric_value is None
    assert sheet.cells["B2"].data_type == "str"
    assert sheet.cells["A3"].formula is None
    assert sheet.cells["A3"].display_value == "=1+2"
    assert sheet.cells["B3"].display_value == ""


def test_csv_rejects_column_outside_excel_bounds(tmp_path: Path) -> None:
    path = tmp_path / "wide.csv"
    path.write_bytes(b"," * 16_383)
    assert "XFD1" in parse_csv(path).sheets[0].cells
    path.write_bytes(b"," * 16_384)
    with pytest.raises(PackageLimitExceeded):
        parse_csv(path)


def test_csv_rejects_directory_and_invalid_encoding(tmp_path: Path) -> None:
    with pytest.raises(InvalidPackage):
        parse_csv(tmp_path)
    path = tmp_path / "invalid.csv"
    path.write_bytes(b"\xff")
    with pytest.raises(InvalidPackage):
        parse_csv(path)


def test_csv_enforces_pinned_file_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raglab.track_b import csv_adapter

    path = tmp_path / "capped.csv"
    path.write_bytes(b"A,B\n")
    monkeypatch.setattr(csv_adapter, "MAX_CSV_BYTES", 4)
    assert parse_csv(path).sheets[0].cells["B1"].display_value == "B"
    path.write_bytes(b"A,B\n\n")
    with pytest.raises(PackageLimitExceeded):
        parse_csv(path)
