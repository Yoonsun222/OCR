"""Observe local UTF-8 CSV records as literal worksheet values."""

from csv import Error as CsvError, reader
from hashlib import sha256
from io import StringIO
from pathlib import Path

from raglab.offline.windows_paths import WindowsLocalPaths

from .coordinates import MAX_COLUMN, MAX_ROW, column_name
from .errors import InvalidPackage, PackageLimitExceeded
from .models import CellObservation, SheetObservation, WorkbookObservation
from .opc import OpcLimits

MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_CSV_CELLS = OpcLimits().max_workbook_cells


def _local_bytes(path: Path) -> bytes:
    paths = WindowsLocalPaths()
    try:
        local = paths.validate(path)
        with paths.pin(local, reject_aliases=True) as pinned:
            if pinned.stream is None or pinned.info is None:
                raise InvalidPackage("CSV input must be a regular local file")
            if pinned.info.size > MAX_CSV_BYTES:
                raise PackageLimitExceeded("CSV byte count exceeds limit")
            return pinned.read_bytes(MAX_CSV_BYTES)
    except (OSError, ValueError):
        raise InvalidPackage("unable to read local CSV file") from None


def parse_csv(path: Path) -> WorkbookObservation:
    raw = _local_bytes(path)
    cells: dict[str, CellObservation] = {}
    try:
        with StringIO(raw.decode("utf-8-sig"), newline="") as source:
            for row_number, fields in enumerate(reader(source, strict=True), 1):
                if row_number > MAX_ROW or len(fields) > MAX_COLUMN:
                    raise PackageLimitExceeded("CSV exceeds worksheet bounds")
                if len(cells) + len(fields) > MAX_CSV_CELLS:
                    raise PackageLimitExceeded("CSV cell count exceeds limit")
                for column, value in enumerate(fields, 1):
                    reference = f"{column_name(column)}{row_number}"
                    cells[reference] = CellObservation(reference, value, None, None, "literal", "str", None)
    except (UnicodeDecodeError, CsvError):
        raise InvalidPackage("invalid UTF-8 CSV data") from None

    sheet = SheetObservation("sheet-csv-1", "csv", path.stem, "visible", path.name, cells)
    return WorkbookObservation(sha256(raw).hexdigest(), "not_applicable", (sheet,))
