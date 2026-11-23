# Track B Native Tables and Charts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, CPU-only Track B adapter that safely reads synthetic CSV/XLSX files, extracts cells and workbook structure, relates native Excel charts to their source ranges, emits `EvidenceRegionV1` records, and answers narrow cell/table/chart evidence queries without executing document code or recalculating formulas.

**Architecture:** The adapter reads XLSX as an OPC ZIP package with explicit size and relationship safety limits, then converts workbook XML into immutable Track B observations. Formula cells preserve both the formula text and the stored cached value; no formula, macro, external link, or data connection is executed. Workbook observations are mapped into the common evidence contract and consumed by deterministic query/evaluation modules.

**Tech Stack:** Python 3.11 standard library (`zipfile`, `xml.etree.ElementTree`, `csv`, `decimal`, `dataclasses`, `hashlib`, `json`), common `raglab.contracts` package, and `pytest` as the development-only test runner.

## Global Constraints

- Actual security documents may be used only inside the company; this repository contains synthetic fixtures and evaluation scaffolding only.
- The first Track B wedge covers native cells, ranges, tables, and chart series/legend/source-range relationships; raster chart interpretation is not part of this plan.
- No VBA, macro, formula, external workbook link, query table, data connection, or embedded object is executed.
- Formula results are read only from the cached `<v>` value and are labeled `cached_not_recalculated`; a missing cached value remains missing.
- Excel semantic locators use workbook hash, stable sheet identifier, and A1 cell/range; visual coordinates are absent until a separately approved renderer supplies a matching artifact hash.
- Local execution performs zero external transmissions and does not use a model or remote API.
- Deterministic cell/text lookup must meet P95 <= 5 seconds on the approved L16 profile and remain within the <=12 GiB process working-set limit.
- Results return at most three evidence candidates or an explicit `판단 불가` state.
- All document names, cell values, formulas, and chart titles are excluded from routine logs; tests assert on synthetic values only.
- Track B quality gates are native cell-value accuracy >=98%, deterministic calculation accuracy >=95% for the supported operation set, and chart series-to-legend accuracy >=90%; production decisions require confidence-interval gates from the approved evaluation plan.
- This plan depends on the common contracts plan producing `EvidenceRegionV1`, `EvidenceRole`, `ExtractionMethod`, `NativeLocator`, and schema validation in `src/raglab/contracts/evidence.py`.

---

## File Structure

```text
src/raglab/track_b/__init__.py          public Track B exports
src/raglab/track_b/errors.py            stable safe-reader and extraction errors
src/raglab/track_b/models.py            immutable workbook/cell/chart observations
src/raglab/track_b/opc.py               bounded ZIP and relationship reader
src/raglab/track_b/workbook.py          workbook, sheet, shared-string, and cell parser
src/raglab/track_b/layout.py            merged/hidden/filtered/table metadata parser
src/raglab/track_b/charts.py            chart/series/legend/source-range parser
src/raglab/track_b/csv_adapter.py       deterministic CSV-to-cell observations
src/raglab/track_b/evidence.py          observation-to-EvidenceRegionV1 adapter
src/raglab/track_b/query.py             narrow deterministic query operations
src/raglab/track_b/evaluate.py          Track B golden-set metrics
tests/fixtures/xlsx_factory.py           in-memory synthetic OPC package factory
tests/track_b/test_opc.py                package security and relationship tests
tests/track_b/test_workbook.py           shared string, formula, date, and sheet tests
tests/track_b/test_layout.py             merge/hidden/filter/table tests
tests/track_b/test_charts.py             series/legend/source-range tests
tests/track_b/test_csv_adapter.py        CSV encoding and rectangularization tests
tests/track_b/test_evidence.py           EvidenceRegionV1 conformance tests
tests/track_b/test_query.py              lookup/aggregate/abstention tests
tests/track_b/test_evaluate.py           metric and threshold tests
docs/track-b-native-contract.md          supported and rejected Excel semantics
```

### Task 1: Safe OPC package reader and synthetic fixture factory

**Files:**
- Create: `src/raglab/track_b/__init__.py`
- Create: `src/raglab/track_b/errors.py`
- Create: `src/raglab/track_b/opc.py`
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/xlsx_factory.py`
- Create: `tests/track_b/test_opc.py`

**Interfaces:**
- Consumes: a local `Path` to a synthetic `.xlsx` and `OpcLimits(max_entries, max_entry_bytes, max_total_bytes, max_ratio)`.
- Produces: `OpcPackage.open(path, limits)`, `OpcPackage.read_xml(part_name)`, `OpcPackage.relationships(part_name)`, and stable `TrackBError` subclasses.

- [ ] **Step 1: Write the failing traversal, macro, external-link, and ZIP-limit tests**

```python
# tests/track_b/test_opc.py
from pathlib import Path

import pytest

from raglab.track_b.errors import ForbiddenPart, PackageLimitExceeded, UnsafeRelationship
from raglab.track_b.opc import OpcLimits, OpcPackage
from tests.fixtures.xlsx_factory import write_xlsx


def test_rejects_macro_part(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "macro.xlsx", extra_parts={"xl/vbaProject.bin": b"x"})
    with pytest.raises(ForbiddenPart, match="xl/vbaProject.bin"):
        OpcPackage.open(path)


def test_rejects_external_relationship(tmp_path: Path) -> None:
    rels = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId9" Type="urn:test" Target="https://example.invalid/data" TargetMode="External"/>
    </Relationships>"""
    path = write_xlsx(tmp_path / "external.xlsx", extra_parts={"xl/_rels/workbook.xml.rels": rels.encode()})
    package = OpcPackage.open(path)
    with pytest.raises(UnsafeRelationship, match="External"):
        package.relationships("xl/workbook.xml")


def test_rejects_total_uncompressed_size_over_limit(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "large.xlsx", extra_parts={"xl/media/blob.bin": b"a" * 128})
    limits = OpcLimits(max_entries=64, max_entry_bytes=256, max_total_bytes=64, max_ratio=100.0)
    with pytest.raises(PackageLimitExceeded, match="total uncompressed"):
        OpcPackage.open(path, limits)
```

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run: `python -m pytest tests/track_b/test_opc.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'raglab.track_b'`.

- [ ] **Step 3: Implement stable errors, the bounded reader, and the fixture factory**

```python
# src/raglab/track_b/errors.py
class TrackBError(Exception):
    code = "TRACK_B_ERROR"


class InvalidPackage(TrackBError):
    code = "TRACK_B_INVALID_PACKAGE"


class PackageLimitExceeded(TrackBError):
    code = "TRACK_B_PACKAGE_LIMIT"


class ForbiddenPart(TrackBError):
    code = "TRACK_B_FORBIDDEN_PART"


class UnsafeRelationship(TrackBError):
    code = "TRACK_B_UNSAFE_RELATIONSHIP"
```

```python
# src/raglab/track_b/opc.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, ZipInfo
import xml.etree.ElementTree as ET

from .errors import ForbiddenPart, InvalidPackage, PackageLimitExceeded, UnsafeRelationship

REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
FORBIDDEN_SUFFIXES = ("vbaproject.bin", "oleobject.bin")


@dataclass(frozen=True)
class OpcLimits:
    max_entries: int = 4096
    max_entry_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_ratio: float = 100.0


@dataclass(frozen=True)
class Relationship:
    relationship_id: str
    relationship_type: str
    target: str


class OpcPackage:
    def __init__(self, path: Path, limits: OpcLimits, names: frozenset[str]) -> None:
        self.path = path
        self.limits = limits
        self.names = names

    @classmethod
    def open(cls, path: Path, limits: OpcLimits = OpcLimits()) -> "OpcPackage":
        try:
            with ZipFile(path) as archive:
                infos = archive.infolist()
                cls._validate_infos(infos, limits)
                names = frozenset(info.filename for info in infos)
        except BadZipFile as exc:
            raise InvalidPackage("input is not a valid OPC ZIP package") from exc
        if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
            raise InvalidPackage("required workbook parts are missing")
        for name in names:
            lower = name.lower()
            if lower.endswith(FORBIDDEN_SUFFIXES) or "/embeddings/" in lower:
                raise ForbiddenPart(name)
        return cls(path, limits, names)

    @staticmethod
    def _validate_infos(infos: list[ZipInfo], limits: OpcLimits) -> None:
        if len(infos) > limits.max_entries:
            raise PackageLimitExceeded("entry count exceeds limit")
        total = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts:
                raise InvalidPackage(f"unsafe part name: {info.filename}")
            if info.file_size > limits.max_entry_bytes:
                raise PackageLimitExceeded(f"entry exceeds limit: {info.filename}")
            total += info.file_size
            compressed = max(info.compress_size, 1)
            if info.file_size / compressed > limits.max_ratio:
                raise PackageLimitExceeded(f"compression ratio exceeds limit: {info.filename}")
        if total > limits.max_total_bytes:
            raise PackageLimitExceeded("total uncompressed size exceeds limit")

    def read_bytes(self, part_name: str) -> bytes:
        if part_name not in self.names:
            raise InvalidPackage(f"missing part: {part_name}")
        with ZipFile(self.path) as archive:
            return archive.read(part_name)

    def read_xml(self, part_name: str) -> ET.Element:
        try:
            return ET.fromstring(self.read_bytes(part_name))
        except ET.ParseError as exc:
            raise InvalidPackage(f"invalid XML part: {part_name}") from exc

    def relationships(self, source_part: str) -> tuple[Relationship, ...]:
        source = PurePosixPath(source_part)
        rels_name = str(source.parent / "_rels" / f"{source.name}.rels")
        if rels_name not in self.names:
            return ()
        root = self.read_xml(rels_name)
        output: list[Relationship] = []
        for node in root.findall(f"{REL_NS}Relationship"):
            if node.attrib.get("TargetMode") == "External":
                raise UnsafeRelationship(f"External relationship: {node.attrib.get('Target', '')}")
            target = node.attrib["Target"].replace("\\", "/")
            combined = _resolve_target(source, target)
            output.append(Relationship(node.attrib["Id"], node.attrib["Type"], combined))
        return tuple(output)


def _resolve_target(source: PurePosixPath, target: str) -> str:
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise UnsafeRelationship(f"absolute relationship target: {target}")
    parts = list(source.parent.parts)
    for part in target_path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise UnsafeRelationship(f"relationship escapes package root: {target}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise UnsafeRelationship(f"empty relationship target: {target}")
    return "/".join(parts)
```

```python
# tests/fixtures/xlsx_factory.py
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

CONTENT_TYPES = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
WORKBOOK = b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'


def write_xlsx(path: Path, extra_parts: dict[str, bytes] | None = None) -> Path:
    parts = {"[Content_Types].xml": CONTENT_TYPES, "xl/workbook.xml": WORKBOOK}
    parts.update(extra_parts or {})
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return path
```

- [ ] **Step 4: Export the package API and run the test**

```python
# src/raglab/track_b/__init__.py
from .opc import OpcLimits, OpcPackage, Relationship

__all__ = ["OpcLimits", "OpcPackage", "Relationship"]
```

Run: `python -m pytest tests/track_b/test_opc.py -v`

Expected: `3 passed` and no outbound network activity.

- [ ] **Step 5: Commit the safe package boundary**

```bash
git add src/raglab/track_b tests/fixtures tests/track_b/test_opc.py
git commit -m "feat(track-b): add bounded OPC package reader"
```

### Task 2: Workbook, shared-string, and cell observation parser

**Files:**
- Create: `src/raglab/track_b/models.py`
- Create: `src/raglab/track_b/workbook.py`
- Modify: `tests/fixtures/xlsx_factory.py`
- Create: `tests/track_b/test_workbook.py`

**Interfaces:**
- Consumes: `OpcPackage` and workbook/sheet XML parts.
- Produces: `WorkbookObservation`, `SheetObservation`, and `CellObservation` where `CellObservation.formula_state` is `literal`, `cached_not_recalculated`, or `cached_missing`.

- [ ] **Step 1: Add a synthetic workbook fixture and failing behavior tests**

```python
# tests/track_b/test_workbook.py
from decimal import Decimal
from pathlib import Path

from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_native_workbook


def test_parses_shared_string_number_and_formula_cache(tmp_path: Path) -> None:
    package = OpcPackage.open(write_native_workbook(tmp_path / "book.xlsx"))
    workbook = parse_workbook(package)
    sheet = workbook.sheets[0]
    assert sheet.stable_id == "sheet-1"
    assert sheet.cells["A1"].display_value == "매출"
    assert sheet.cells["B2"].numeric_value == Decimal("12.5")
    assert sheet.cells["C2"].formula == "SUM(B2:B3)"
    assert sheet.cells["C2"].display_value == "20"
    assert sheet.cells["C2"].formula_state == "cached_not_recalculated"


def test_does_not_invent_missing_formula_cache(tmp_path: Path) -> None:
    package = OpcPackage.open(write_native_workbook(tmp_path / "book.xlsx"))
    cell = parse_workbook(package).sheets[0].cells["D2"]
    assert cell.formula == "B2*2"
    assert cell.display_value is None
    assert cell.formula_state == "cached_missing"
```

- [ ] **Step 2: Run the focused tests and confirm the missing-parser failure**

Run: `python -m pytest tests/track_b/test_workbook.py -v`

Expected: collection fails because `raglab.track_b.workbook` does not exist.

- [ ] **Step 3: Add immutable observation models**

```python
# src/raglab/track_b/models.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CellObservation:
    reference: str
    display_value: str | None
    numeric_value: Decimal | None
    formula: str | None
    formula_state: str
    data_type: str
    style_id: str | None


@dataclass(frozen=True)
class SheetObservation:
    stable_id: str
    relationship_id: str
    name: str
    state: str
    part_name: str
    cells: dict[str, CellObservation]
    merged_ranges: tuple[str, ...] = ()
    hidden_rows: frozenset[int] = frozenset()
    hidden_columns: frozenset[str] = frozenset()
    auto_filter_range: str | None = None


@dataclass(frozen=True)
class WorkbookObservation:
    source_hash: str
    date_system: str
    sheets: tuple[SheetObservation, ...]
```

- [ ] **Step 4: Implement native workbook parsing without recalculation**

```python
# src/raglab/track_b/workbook.py
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from .errors import InvalidPackage
from .models import CellObservation, SheetObservation, WorkbookObservation
from .opc import OpcPackage

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WORKSHEET_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"


def _shared_strings(package: OpcPackage) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in package.names:
        return ()
    root = package.read_xml("xl/sharedStrings.xml")
    return tuple("".join(node.itertext()) for node in root.findall(f"{S}si"))


def _cell(node, shared: tuple[str, ...]) -> CellObservation:
    reference = node.attrib["r"]
    kind = node.attrib.get("t", "n")
    formula_node = node.find(f"{S}f")
    value_node = node.find(f"{S}v")
    formula = formula_node.text if formula_node is not None else None
    raw = value_node.text if value_node is not None else None
    if kind == "s" and raw is not None:
        display = shared[int(raw)]
        numeric = None
    elif kind == "inlineStr":
        inline = node.find(f"{S}is")
        display = "".join(inline.itertext()) if inline is not None else ""
        numeric = None
    else:
        display = raw
        try:
            numeric = Decimal(raw) if raw is not None else None
        except InvalidOperation:
            numeric = None
    formula_state = "literal" if formula is None else ("cached_not_recalculated" if raw is not None else "cached_missing")
    return CellObservation(reference, display, numeric, formula, formula_state, kind, node.attrib.get("s"))


def parse_workbook(package: OpcPackage) -> WorkbookObservation:
    shared = _shared_strings(package)
    workbook_root = package.read_xml("xl/workbook.xml")
    rels = {rel.relationship_id: rel for rel in package.relationships("xl/workbook.xml")}
    workbook_properties = workbook_root.find(f"{S}workbookPr")
    date_system = "1904" if workbook_properties is not None and workbook_properties.attrib.get("date1904") == "1" else "1900"
    sheets: list[SheetObservation] = []
    for ordinal, sheet_node in enumerate(workbook_root.findall(f"{S}sheets/{S}sheet"), start=1):
        relationship_id = sheet_node.attrib[f"{R}id"]
        relationship = rels.get(relationship_id)
        if relationship is None or relationship.relationship_type != WORKSHEET_REL:
            raise InvalidPackage(f"invalid worksheet relationship: {relationship_id}")
        root = package.read_xml(relationship.target)
        cells = {_cell(node, shared).reference: _cell(node, shared) for node in root.findall(f".//{S}c")}
        sheets.append(SheetObservation(
            stable_id=f"sheet-{sheet_node.attrib.get('sheetId', ordinal)}",
            relationship_id=relationship_id,
            name=sheet_node.attrib["name"],
            state=sheet_node.attrib.get("state", "visible"),
            part_name=relationship.target,
            cells=cells,
        ))
    source_hash = sha256(package.path.read_bytes()).hexdigest()
    return WorkbookObservation(source_hash, date_system, tuple(sheets))
```

- [ ] **Step 5: Extend `write_native_workbook` with fixed OPC XML and run tests**

```python
# append to tests/fixtures/xlsx_factory.py
def write_native_workbook(path: Path) -> Path:
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>""".encode()
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
       Target="worksheets/sheet1.xml"/></Relationships>""".encode()
    shared = """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <si><t>매출</t></si></sst>""".encode("utf-8")
    sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c></row>
        <row r="2"><c r="B2"><v>12.5</v></c><c r="C2"><f>SUM(B2:B3)</f><v>20</v></c><c r="D2"><f>B2*2</f></c></row>
        <row r="3"><c r="B3"><v>7.5</v></c></row>
      </sheetData></worksheet>""".encode()
    return write_xlsx(path, {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": relationships,
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet,
    })
```

Run: `python -m pytest tests/track_b/test_workbook.py tests/track_b/test_opc.py -v`

Expected: `5 passed`.

- [ ] **Step 6: Commit native workbook observations**

```bash
git add src/raglab/track_b/models.py src/raglab/track_b/workbook.py tests/fixtures/xlsx_factory.py tests/track_b/test_workbook.py
git commit -m "feat(track-b): parse native workbook cell observations"
```

### Task 3: Layout metadata, tables, and CSV parity

**Files:**
- Create: `src/raglab/track_b/layout.py`
- Create: `src/raglab/track_b/csv_adapter.py`
- Modify: `src/raglab/track_b/models.py`
- Modify: `src/raglab/track_b/workbook.py`
- Modify: `tests/fixtures/xlsx_factory.py`
- Create: `tests/track_b/test_layout.py`
- Create: `tests/track_b/test_csv_adapter.py`

**Interfaces:**
- Consumes: `SheetObservation`, worksheet XML, optional table relationships, and UTF-8/UTF-8-BOM CSV files.
- Produces: populated merged/hidden/filter/table metadata plus `parse_csv(path) -> WorkbookObservation`.

- [ ] **Step 1: Write failing layout and CSV tests**

```python
# tests/track_b/test_layout.py
from pathlib import Path

from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_layout_workbook


def test_preserves_merge_hidden_filter_and_table_metadata(tmp_path: Path) -> None:
    sheet = parse_workbook(OpcPackage.open(write_layout_workbook(tmp_path / "layout.xlsx"))).sheets[0]
    assert sheet.merged_ranges == ("A1:B1",)
    assert sheet.hidden_rows == frozenset({4})
    assert sheet.hidden_columns == frozenset({"D"})
    assert sheet.auto_filter_range == "A1:C9"
    assert [(table.name, table.reference) for table in sheet.tables] == [("Sales", "A1:C9")]
```

```python
# tests/track_b/test_csv_adapter.py
from pathlib import Path

from raglab.track_b.csv_adapter import parse_csv


def test_csv_becomes_one_sheet_with_a1_cells(tmp_path: Path) -> None:
    path = tmp_path / "표.csv"
    path.write_text("품목,수량\nA,3\nB,5\n", encoding="utf-8-sig")
    sheet = parse_csv(path).sheets[0]
    assert sheet.name == "표"
    assert sheet.cells["A1"].display_value == "품목"
    assert sheet.cells["B3"].display_value == "5"
```

- [ ] **Step 2: Run tests and confirm missing metadata/adapter failures**

Run: `python -m pytest tests/track_b/test_layout.py tests/track_b/test_csv_adapter.py -v`

Expected: failures show missing `SheetObservation.tables` and missing `raglab.track_b.csv_adapter`.

- [ ] **Step 3: Add table metadata and layout parsing**

```python
# append to src/raglab/track_b/models.py before SheetObservation
@dataclass(frozen=True)
class TableObservation:
    table_id: str
    name: str
    display_name: str
    reference: str
    header_row_count: int
    totals_row_count: int
```

Update `SheetObservation` so `tables: tuple[TableObservation, ...] = ()`. Add `parse_layout(package, sheet, root) -> SheetObservation` in `layout.py`; it must:

```python
# src/raglab/track_b/layout.py
from dataclasses import replace

from .models import SheetObservation, TableObservation
from .opc import OpcPackage

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def parse_layout(package: OpcPackage, sheet: SheetObservation, root) -> SheetObservation:
    merged = tuple(node.attrib["ref"] for node in root.findall(f"{S}mergeCells/{S}mergeCell"))
    hidden_rows = frozenset(int(node.attrib["r"]) for node in root.findall(f"{S}sheetData/{S}row") if node.attrib.get("hidden") == "1")
    hidden_columns: set[str] = set()
    for node in root.findall(f"{S}cols/{S}col"):
        if node.attrib.get("hidden") == "1" and node.attrib.get("min") == node.attrib.get("max"):
            hidden_columns.add(_column_name(int(node.attrib["min"])))
    filter_node = root.find(f"{S}autoFilter")
    tables: list[TableObservation] = []
    rels = {rel.relationship_id: rel for rel in package.relationships(sheet.part_name)}
    for node in root.findall(f"{S}tableParts/{S}tablePart"):
        rel = rels[node.attrib[f"{R}id"]]
        table = package.read_xml(rel.target)
        tables.append(TableObservation(
            table.attrib["id"], table.attrib["name"], table.attrib["displayName"], table.attrib["ref"],
            int(table.attrib.get("headerRowCount", "1")), int(table.attrib.get("totalsRowCount", "0")),
        ))
    return replace(
        sheet,
        merged_ranges=merged,
        hidden_rows=hidden_rows,
        hidden_columns=frozenset(hidden_columns),
        auto_filter_range=filter_node.attrib["ref"] if filter_node is not None else None,
        tables=tuple(tables),
    )


def _column_name(number: int) -> str:
    output = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        output = chr(65 + remainder) + output
    return output
```

Call `parse_layout` from `parse_workbook` after constructing each sheet.

- [ ] **Step 4: Implement CSV observations without type guessing**

```python
# src/raglab/track_b/csv_adapter.py
from csv import reader
from hashlib import sha256
from pathlib import Path

from .models import CellObservation, SheetObservation, WorkbookObservation


def _column_name(number: int) -> str:
    output = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        output = chr(65 + remainder) + output
    return output


def parse_csv(path: Path) -> WorkbookObservation:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    cells: dict[str, CellObservation] = {}
    for row_number, row in enumerate(reader(text.splitlines()), start=1):
        for column_number, value in enumerate(row, start=1):
            reference = f"{_column_name(column_number)}{row_number}"
            cells[reference] = CellObservation(reference, value, None, None, "literal", "str", None)
    sheet = SheetObservation("sheet-csv-1", "csv", path.stem, "visible", path.name, cells)
    return WorkbookObservation(sha256(raw).hexdigest(), "not_applicable", (sheet,))
```

- [ ] **Step 5: Add the fixed layout fixture and run tests**

```python
# append to tests/fixtures/xlsx_factory.py
def write_layout_workbook(path: Path) -> Path:
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>""".encode()
    workbook_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
       Target="worksheets/sheet1.xml"/></Relationships>""".encode()
    sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <cols><col min="4" max="4" hidden="1"/></cols>
      <sheetData><row r="1"/><row r="4" hidden="1"/></sheetData>
      <mergeCells><mergeCell ref="A1:B1"/></mergeCells>
      <autoFilter ref="A1:C9"/>
      <tableParts count="1"><tablePart r:id="rIdTable"/></tableParts>
      </worksheet>""".encode()
    sheet_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdTable" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
       Target="../tables/table1.xml"/></Relationships>""".encode()
    table = """<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      id="1" name="Sales" displayName="Sales" ref="A1:C9" headerRowCount="1" totalsRowCount="0"/>""".encode()
    return write_xlsx(path, {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/worksheets/sheet1.xml": sheet,
        "xl/worksheets/_rels/sheet1.xml.rels": sheet_rels,
        "xl/tables/table1.xml": table,
    })
```

Run: `python -m pytest tests/track_b/test_layout.py tests/track_b/test_csv_adapter.py tests/track_b/test_workbook.py -v`

Expected: all tests pass; CSV numeric-looking text remains `data_type == "str"`.

- [ ] **Step 6: Commit structural metadata and CSV support**

```bash
git add src/raglab/track_b tests/fixtures/xlsx_factory.py tests/track_b/test_layout.py tests/track_b/test_csv_adapter.py
git commit -m "feat(track-b): preserve sheet layout and CSV evidence"
```

### Task 4: Native chart series, legend, and source-range relationships

**Files:**
- Modify: `src/raglab/track_b/models.py`
- Create: `src/raglab/track_b/charts.py`
- Modify: `tests/fixtures/xlsx_factory.py`
- Create: `tests/track_b/test_charts.py`

**Interfaces:**
- Consumes: drawing relationships from a `SheetObservation` and native `chart*.xml` parts.
- Produces: `ChartObservation` and ordered `ChartSeriesObservation` values containing title formula, category formula, value formula, cached legend text, and series order.

- [ ] **Step 1: Write the failing chart relationship test**

```python
# tests/track_b/test_charts.py
from pathlib import Path

from raglab.track_b.charts import parse_charts
from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_chart_workbook


def test_links_chart_series_legend_and_source_ranges(tmp_path: Path) -> None:
    package = OpcPackage.open(write_chart_workbook(tmp_path / "chart.xlsx"))
    sheet = parse_workbook(package).sheets[0]
    chart = parse_charts(package, sheet)[0]
    assert chart.chart_id == "chart-1"
    assert chart.title == "분기 매출"
    assert chart.chart_type == "barChart"
    assert chart.series[0].order == 0
    assert chart.series[0].legend_text == "제품 A"
    assert chart.series[0].title_formula == "'Data'!$B$1"
    assert chart.series[0].category_formula == "'Data'!$A$2:$A$5"
    assert chart.series[0].value_formula == "'Data'!$B$2:$B$5"
```

- [ ] **Step 2: Run the test and confirm the missing chart module**

Run: `python -m pytest tests/track_b/test_charts.py -v`

Expected: collection fails because `raglab.track_b.charts` does not exist.

- [ ] **Step 3: Add chart observation models**

```python
# append to src/raglab/track_b/models.py
@dataclass(frozen=True)
class ChartSeriesObservation:
    index: int
    order: int
    legend_text: str | None
    title_formula: str | None
    category_formula: str | None
    value_formula: str | None


@dataclass(frozen=True)
class ChartObservation:
    chart_id: str
    sheet_stable_id: str
    part_name: str
    chart_type: str
    title: str | None
    series: tuple[ChartSeriesObservation, ...]
```

- [ ] **Step 4: Implement explicit drawing-to-chart relationship traversal**

```python
# src/raglab/track_b/charts.py
from __future__ import annotations

from .models import ChartObservation, ChartSeriesObservation, SheetObservation
from .opc import OpcPackage

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"


def _text(root) -> str | None:
    if root is None:
        return None
    nodes = root.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}t")
    nodes += root.findall(f".//{C}v")
    values = [node.text for node in nodes if node.text]
    return "".join(values) or None


def _formula(node, path: str) -> str | None:
    found = node.find(path)
    return found.text if found is not None else None


def parse_charts(package: OpcPackage, sheet: SheetObservation) -> tuple[ChartObservation, ...]:
    sheet_root = package.read_xml(sheet.part_name)
    drawing_node = sheet_root.find(f"{S}drawing")
    if drawing_node is None:
        return ()
    sheet_rels = {rel.relationship_id: rel for rel in package.relationships(sheet.part_name)}
    drawing = sheet_rels[drawing_node.attrib[f"{R}id"]]
    drawing_root = package.read_xml(drawing.target)
    drawing_rels = {rel.relationship_id: rel for rel in package.relationships(drawing.target)}
    output: list[ChartObservation] = []
    for ordinal, graphic in enumerate(drawing_root.findall(f".//{C}chart"), start=1):
        chart_rel = drawing_rels[graphic.attrib[f"{R}id"]]
        root = package.read_xml(chart_rel.target)
        chart_type_node = next((node for node in root.findall(f".//{C}plotArea/*") if node.tag.endswith("Chart")), None)
        if chart_type_node is None:
            continue
        series: list[ChartSeriesObservation] = []
        for node in chart_type_node.findall(f"{C}ser"):
            index = int(node.find(f"{C}idx").attrib["val"])
            order = int(node.find(f"{C}order").attrib["val"])
            title_formula = _formula(node, f"{C}tx/{C}strRef/{C}f")
            legend_text = _text(node.find(f"{C}tx")) if node.find(f"{C}tx") is not None else None
            series.append(ChartSeriesObservation(
                index,
                order,
                legend_text,
                title_formula,
                _formula(node, f"{C}cat/{C}strRef/{C}f") or _formula(node, f"{C}cat/{C}numRef/{C}f"),
                _formula(node, f"{C}val/{C}numRef/{C}f"),
            ))
        output.append(ChartObservation(
            f"chart-{ordinal}", sheet.stable_id, chart_rel.target,
            chart_type_node.tag.rsplit("}", 1)[-1], _text(root.find(f".//{C}title")), tuple(sorted(series, key=lambda item: item.order)),
        ))
    return tuple(output)
```

- [ ] **Step 5: Add the fixed chart fixture and run tests**

```python
# append to tests/fixtures/xlsx_factory.py
def write_chart_workbook(path: Path) -> Path:
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>""".encode()
    workbook_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
       Target="worksheets/sheet1.xml"/></Relationships>""".encode()
    sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheetData><row r="2"><c r="B2"><v>1</v></c></row></sheetData><drawing r:id="rIdDraw"/></worksheet>""".encode()
    sheet_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdDraw" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
       Target="../drawings/drawing1.xml"/></Relationships>""".encode()
    drawing = """<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
      xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <xdr:twoCellAnchor><xdr:graphicFrame><c:chart r:id="rIdChart"/></xdr:graphicFrame></xdr:twoCellAnchor></xdr:wsDr>""".encode()
    drawing_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdChart" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
       Target="../charts/chart1.xml"/></Relationships>""".encode()
    chart = """<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart>
      <c:title><c:tx><c:rich><a:p><a:r><a:t>분기 매출</a:t></a:r></a:p></c:rich></c:tx></c:title>
      <c:plotArea><c:barChart><c:ser><c:idx val="0"/><c:order val="0"/>
      <c:tx><c:strRef><c:f>'Data'!$B$1</c:f><c:strCache><c:pt idx="0"><c:v>제품 A</c:v></c:pt></c:strCache></c:strRef></c:tx>
      <c:cat><c:strRef><c:f>'Data'!$A$2:$A$5</c:f></c:strRef></c:cat>
      <c:val><c:numRef><c:f>'Data'!$B$2:$B$5</c:f></c:numRef></c:val>
      </c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>""".encode("utf-8")
    return write_xlsx(path, {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/worksheets/sheet1.xml": sheet,
        "xl/worksheets/_rels/sheet1.xml.rels": sheet_rels,
        "xl/drawings/drawing1.xml": drawing,
        "xl/drawings/_rels/drawing1.xml.rels": drawing_rels,
        "xl/charts/chart1.xml": chart,
    })
```

Run: `python -m pytest tests/track_b/test_charts.py tests/track_b/test_opc.py -v`

Expected: all chart and package safety tests pass.

- [ ] **Step 6: Commit native chart relationships**

```bash
git add src/raglab/track_b/models.py src/raglab/track_b/charts.py tests/fixtures/xlsx_factory.py tests/track_b/test_charts.py
git commit -m "feat(track-b): extract native chart series relationships"
```

### Task 5: EvidenceRegionV1 adapter and schema conformance

**Files:**
- Create: `src/raglab/track_b/evidence.py`
- Create: `tests/track_b/test_evidence.py`

**Interfaces:**
- Consumes: the stable public exports `EvidenceRegionV1`, `EvidenceRelation`, `EvidenceRole`, `ExtractionMethod`, and `NativeLocator` from `raglab.contracts`; Track B workbook and chart observations; caller-supplied `document_version`, `generation_id`, and `acl_snapshot_id`.
- Produces: immutable envelope-level cell and chart evidence with `NativeLocator.sheet_range` or `NativeLocator.sheet_object`, `regions=()` because no renderer artifact exists, ACL snapshot identity on the envelope, and typed relations whose targets are explicit native formulas/ranges.

- [ ] **Step 1: Write failing Evidence contract tests**

```python
# tests/track_b/test_evidence.py
from pathlib import Path

from raglab.track_b.charts import parse_charts
from raglab.track_b.evidence import workbook_evidence
from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_chart_workbook


def test_cell_and_chart_evidence_use_native_locators_without_fake_coordinates(tmp_path: Path) -> None:
    package = OpcPackage.open(write_chart_workbook(tmp_path / "chart.xlsx"))
    workbook = parse_workbook(package)
    charts = parse_charts(package, workbook.sheets[0])
    evidence = workbook_evidence(workbook, charts, "doc-v1", "gen-1", "acl-7")
    cell = next(item for item in evidence if item.native_locator.cell_range == "B2")
    assert cell.source_hash == workbook.source_hash
    assert cell.native_locator.sheet_id == "sheet-1"
    assert cell.regions == ()
    assert cell.extraction_method.value == "native"
    assert cell.acl_snapshot_id == "acl-7"
    series = next(item for item in evidence if item.native_locator.object_id == "chart-1:series:0")
    assert series.regions == ()
    assert {relation.role.value for relation in series.relations} == {"legend", "series", "value"}
    assert {relation.target for relation in series.relations} == {
        "'Data'!$B$1", "'Data'!$A$2:$A$5", "'Data'!$B$2:$B$5",
    }
```

- [ ] **Step 2: Run the test and confirm the adapter is absent**

Run: `python -m pytest tests/track_b/test_evidence.py -v`

Expected: collection fails because `raglab.track_b.evidence` does not exist.

- [ ] **Step 3: Implement deterministic evidence IDs and native-only regions**

```python
# src/raglab/track_b/evidence.py
from __future__ import annotations

from hashlib import sha256

from raglab.contracts import (
    EvidenceRegionV1,
    EvidenceRelation,
    EvidenceRole,
    ExtractionMethod,
    NativeLocator,
)

from .models import ChartObservation, WorkbookObservation


def _id(source_hash: str, locator: str, role: str) -> str:
    return "ev-" + sha256(f"{source_hash}|{locator}|{role}".encode("utf-8")).hexdigest()[:24]


def workbook_evidence(
    workbook: WorkbookObservation,
    charts: tuple[ChartObservation, ...],
    document_version: str,
    generation_id: str,
    acl_snapshot_id: str,
) -> tuple[EvidenceRegionV1, ...]:
    output: list[EvidenceRegionV1] = []
    for sheet in workbook.sheets:
        for reference, cell in sorted(sheet.cells.items()):
            locator = NativeLocator.sheet_range(sheet.stable_id, reference)
            output.append(EvidenceRegionV1(
                evidence_id=_id(workbook.source_hash, f"{sheet.stable_id}!{reference}", "value"),
                source_hash=workbook.source_hash,
                document_version=document_version,
                generation_id=generation_id,
                native_locator=locator,
                role=EvidenceRole.VALUE,
                text=cell.display_value,
                regions=(),
                relations=(),
                extraction_method=ExtractionMethod.NATIVE,
                confidence=1.0,
                acl_snapshot_id=acl_snapshot_id,
                metadata={"formula": cell.formula, "formula_state": cell.formula_state, "sheet_state": sheet.state},
            ))
    for chart in charts:
        for series in chart.series:
            object_id = f"{chart.chart_id}:series:{series.order}"
            locator = NativeLocator.sheet_object(chart.sheet_stable_id, object_id)
            relation_inputs = (
                (EvidenceRole.LEGEND, series.title_formula, series.legend_text),
                (EvidenceRole.SERIES, series.category_formula, chart.chart_type),
                (EvidenceRole.VALUE, series.value_formula, None),
            )
            relations = tuple(
                EvidenceRelation(role, target, note)
                for role, target, note in relation_inputs
                if target is not None
            )
            output.append(EvidenceRegionV1(
                evidence_id=_id(workbook.source_hash, object_id, "series"),
                source_hash=workbook.source_hash,
                document_version=document_version,
                generation_id=generation_id,
                native_locator=locator,
                role=EvidenceRole.SERIES,
                text=series.legend_text,
                regions=(),
                relations=relations,
                extraction_method=ExtractionMethod.NATIVE,
                confidence=1.0,
                acl_snapshot_id=acl_snapshot_id,
                metadata={"chart_title": chart.title, "chart_part": chart.part_name},
            ))
    return tuple(output)
```

- [ ] **Step 4: Run schema conformance and Track B tests**

Run: `python -m pytest tests/contracts/test_evidence.py tests/track_b/test_evidence.py tests/track_b/test_charts.py -v`

Expected: all tests pass; every Track B item passes the common schema validator, and no item claims a pixel region without a renderer artifact.

- [ ] **Step 5: Commit the common-contract adapter**

```bash
git add src/raglab/track_b/evidence.py tests/track_b/test_evidence.py
git commit -m "feat(track-b): emit native evidence contract records"
```

### Task 6: Deterministic lookup, supported aggregates, and abstention

**Files:**
- Create: `src/raglab/track_b/query.py`
- Create: `tests/track_b/test_query.py`

**Interfaces:**
- Consumes: authorized Track B `EvidenceRegionV1` items after the common retrieval layer has enforced ACL and pinned policy/generation.
- Produces: `TrackBAnswer(status, answer, candidates, operation)` with no more than three candidates; supported operations are exact cell lookup and `sum`, `min`, `max`, `average` over explicit numeric cell ranges.

- [ ] **Step 1: Write failing lookup, aggregate, and abstention tests**

```python
# tests/track_b/test_query.py
from decimal import Decimal

from raglab.track_b.query import TrackBQuery, answer_query
from tests.track_b.helpers import numeric_evidence


def test_exact_cell_lookup_returns_one_candidate() -> None:
    evidence = numeric_evidence({"B2": "12.5", "B3": "7.5"})
    result = answer_query(TrackBQuery("lookup", "sheet-1", "B2"), evidence)
    assert result.status == "answered"
    assert result.answer == "12.5"
    assert len(result.candidates) == 1


def test_sum_uses_explicit_numeric_range_only() -> None:
    evidence = numeric_evidence({"B2": "12.5", "B3": "7.5"})
    result = answer_query(TrackBQuery("sum", "sheet-1", "B2:B3"), evidence)
    assert result.answer == "20.0"
    assert result.operation == "deterministic_sum"


def test_missing_or_non_numeric_range_abstains() -> None:
    evidence = numeric_evidence({"B2": "12.5"})
    result = answer_query(TrackBQuery("average", "sheet-1", "B2:B3"), evidence)
    assert result.status == "판단 불가"
    assert result.answer is None
```

- [ ] **Step 2: Run the focused tests and confirm the query module is absent**

Run: `python -m pytest tests/track_b/test_query.py -v`

Expected: collection fails because `raglab.track_b.query` does not exist.

- [ ] **Step 3: Implement a closed deterministic operation set**

```python
# src/raglab/track_b/query.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import mean

from raglab.contracts import EvidenceRegionV1


@dataclass(frozen=True)
class TrackBQuery:
    operation: str
    sheet_id: str
    cell_range: str


@dataclass(frozen=True)
class TrackBAnswer:
    status: str
    answer: str | None
    candidates: tuple[EvidenceRegionV1, ...]
    operation: str


def _column_number(name: str) -> int:
    number = 0
    for character in name:
        number = number * 26 + ord(character) - 64
    return number


def _split_cell(reference: str) -> tuple[str, int]:
    column = "".join(character for character in reference if character.isalpha()).upper()
    row = int("".join(character for character in reference if character.isdigit()))
    return column, row


def _in_range(reference: str, requested: str) -> bool:
    if ":" not in requested:
        return reference == requested
    start, end = requested.split(":", 1)
    column, row = _split_cell(reference)
    start_column, start_row = _split_cell(start)
    end_column, end_row = _split_cell(end)
    return _column_number(start_column) <= _column_number(column) <= _column_number(end_column) and start_row <= row <= end_row


def _range_size(requested: str) -> int:
    if ":" not in requested:
        return 1
    start, end = requested.split(":", 1)
    start_column, start_row = _split_cell(start)
    end_column, end_row = _split_cell(end)
    width = _column_number(end_column) - _column_number(start_column) + 1
    height = end_row - start_row + 1
    return width * height if width > 0 and height > 0 else 0


def answer_query(query: TrackBQuery, evidence: tuple[EvidenceRegionV1, ...]) -> TrackBAnswer:
    supported = {"lookup", "sum", "min", "max", "average"}
    if query.operation not in supported:
        return TrackBAnswer("판단 불가", None, (), "unsupported_operation")
    selected = tuple(item for item in evidence if item.native_locator.sheet_id == query.sheet_id and item.native_locator.cell_range and _in_range(item.native_locator.cell_range, query.cell_range))
    if query.operation == "lookup":
        exact = tuple(item for item in selected if item.native_locator.cell_range == query.cell_range)
        return TrackBAnswer("answered", exact[0].text, exact[:1], "deterministic_lookup") if len(exact) == 1 else TrackBAnswer("판단 불가", None, exact[:3], "deterministic_lookup")
    try:
        values = [Decimal(item.text) for item in selected if item.text is not None]
    except InvalidOperation:
        return TrackBAnswer("판단 불가", None, selected[:3], f"deterministic_{query.operation}")
    expected_count = _range_size(query.cell_range)
    if not values or len(values) != expected_count or len(selected) != expected_count:
        return TrackBAnswer("판단 불가", None, selected[:3], f"deterministic_{query.operation}")
    operations = {"sum": sum, "min": min, "max": max, "average": mean}
    answer = operations[query.operation](values)
    return TrackBAnswer("answered", str(answer), selected[:3], f"deterministic_{query.operation}")
```

- [ ] **Step 4: Add the exact synthetic Evidence helper and run tests**

```python
# tests/track_b/helpers.py
from hashlib import sha256

from raglab.contracts import EvidenceRegionV1, EvidenceRole, ExtractionMethod, NativeLocator


def numeric_evidence(values: dict[str, str]) -> tuple[EvidenceRegionV1, ...]:
    output: list[EvidenceRegionV1] = []
    for reference, value in sorted(values.items()):
        output.append(EvidenceRegionV1(
            evidence_id="ev-" + sha256(reference.encode()).hexdigest()[:24],
            source_hash="0" * 64,
            document_version="doc-test-v1",
            generation_id="gen-test-1",
            native_locator=NativeLocator.sheet_range("sheet-1", reference),
            role=EvidenceRole.VALUE,
            text=value,
            regions=(),
            relations=(),
            extraction_method=ExtractionMethod.NATIVE,
            confidence=1.0,
            acl_snapshot_id="acl-test",
            metadata={"formula": None, "formula_state": "literal"},
        ))
    return tuple(output)
```

Run: `python -m pytest tests/track_b/test_query.py tests/track_b/test_evidence.py -v`

Expected: all tests pass and unsupported operations return `판단 불가` without evaluating input as code.

- [ ] **Step 5: Commit deterministic queries**

```bash
git add src/raglab/track_b/query.py tests/track_b/helpers.py tests/track_b/test_query.py
git commit -m "feat(track-b): add deterministic evidence queries"
```

### Task 7: Golden evaluation, performance check, and operator contract

**Files:**
- Create: `src/raglab/track_b/evaluate.py`
- Create: `tests/track_b/test_evaluate.py`
- Create: `tests/track_b/test_performance.py`
- Create: `docs/track-b-native-contract.md`

**Interfaces:**
- Consumes: frozen synthetic expected cell/chart/query cases and actual Track B outputs.
- Produces: `TrackBMetrics(cell_value_accuracy, calculation_accuracy, chart_relation_accuracy, abstention_precision, elapsed_seconds)` and a JSON-safe dictionary for the common run manifest.

- [ ] **Step 1: Write failing metric and latency tests**

```python
# tests/track_b/test_evaluate.py
from raglab.track_b.evaluate import EvaluationCase, evaluate_cases


def test_metrics_count_each_observation_once() -> None:
    cases = (
        EvaluationCase("cell", "12.5", "12.5", True),
        EvaluationCase("cell", "7.5", "8.0", True),
        EvaluationCase("chart_relation", "Data!B2:B5", "Data!B2:B5", True),
        EvaluationCase("abstain", None, None, True),
    )
    metrics = evaluate_cases(cases, elapsed_seconds=0.25)
    assert metrics.cell_value_accuracy == 0.5
    assert metrics.chart_relation_accuracy == 1.0
    assert metrics.abstention_precision == 1.0
    assert metrics.to_dict()["elapsed_seconds"] == 0.25
```

```python
# tests/track_b/test_performance.py
from pathlib import Path
from time import perf_counter

from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_native_workbook


def test_synthetic_native_parse_finishes_under_five_seconds(tmp_path: Path) -> None:
    path = write_native_workbook(tmp_path / "book.xlsx")
    started = perf_counter()
    parse_workbook(OpcPackage.open(path))
    assert perf_counter() - started < 5.0
```

- [ ] **Step 2: Run tests and confirm the evaluation module is absent**

Run: `python -m pytest tests/track_b/test_evaluate.py tests/track_b/test_performance.py -v`

Expected: evaluation test collection fails because `raglab.track_b.evaluate` does not exist.

- [ ] **Step 3: Implement observation-level metrics without treating repeated runs as new samples**

```python
# src/raglab/track_b/evaluate.py
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EvaluationCase:
    category: str
    expected: str | None
    actual: str | None
    independent_observation: bool


@dataclass(frozen=True)
class TrackBMetrics:
    cell_value_accuracy: float | None
    calculation_accuracy: float | None
    chart_relation_accuracy: float | None
    abstention_precision: float | None
    elapsed_seconds: float

    def to_dict(self) -> dict[str, float | None]:
        return asdict(self)


def _accuracy(cases: tuple[EvaluationCase, ...], category: str) -> float | None:
    selected = [case for case in cases if case.category == category and case.independent_observation]
    return sum(case.actual == case.expected for case in selected) / len(selected) if selected else None


def evaluate_cases(cases: tuple[EvaluationCase, ...], elapsed_seconds: float) -> TrackBMetrics:
    return TrackBMetrics(
        _accuracy(cases, "cell"),
        _accuracy(cases, "calculation"),
        _accuracy(cases, "chart_relation"),
        _accuracy(cases, "abstain"),
        elapsed_seconds,
    )
```

- [ ] **Step 4: Document the exact support and rejection contract**

Create `docs/track-b-native-contract.md` containing these normative sections:

```markdown
# Track B Native Contract

## Supported

- UTF-8 and UTF-8-BOM CSV as literal text cells.
- XLSX shared strings, inline strings, numbers, booleans, formula text, and stored cached values.
- Sheet identity/state, A1 locators, merged ranges, hidden rows/columns, auto-filter ranges, and native table ranges.
- Native chart title, chart type, ordered series, legend cache, category formula, and value formula.
- Exact cell lookup and sum/min/max/average over explicit numeric ranges.

## Rejected or non-executed

- VBA, macros, embedded OLE objects, external relationships, external workbook links, data connections, query refresh, and dynamic data exchange.
- Formula evaluation or refresh. Cached values are labeled `cached_not_recalculated`; absent caches remain absent.
- Password-protected, malformed, over-limit, traversal, and high-compression-ratio packages.
- Pixel coordinates without a separately approved renderer artifact and matching hash.
- Engineering interpretation, forecast, inferred chart values, and unsupported natural-language calculations.

## Security behavior

All package relationships are internal and bounded. Routine logs contain only non-content run/document identifiers and stable error codes. ACL enforcement and final reauthorization are owned by the common query boundary.
```

- [ ] **Step 5: Run the complete Track B suite and placeholder/security scans**

Run: `python -m pytest tests/track_b -v`

Expected: all Track B tests pass.

Run: `rg -n "eval\(|exec\(|subprocess|TargetMode=.?External|vbaProject" src/raglab/track_b`

Expected: only explicit rejection constants or checks appear; no `eval`, `exec`, or subprocess invocation exists.

Run: `python -m pytest tests/contracts tests/track_b -v`

Expected: common contract and Track B suites pass together.

- [ ] **Step 6: Commit the evaluation and operator contract**

```bash
git add src/raglab/track_b/evaluate.py tests/track_b/test_evaluate.py tests/track_b/test_performance.py docs/track-b-native-contract.md
git commit -m "test(track-b): add golden metrics and native contract"
```

## Self-Review Checklist

- [ ] Every spec requirement in the Track B narrow wedge maps to a task: package safety (Task 1), cells/formulas (Task 2), sheet/table state and CSV (Task 3), charts (Task 4), common evidence (Task 5), deterministic answers (Task 6), and metrics/operator limits (Task 7).
- [ ] No plan task executes formulas, macros, links, queries, embedded code, Office automation, a model, or a network request.
- [ ] `CellObservation`, `SheetObservation`, `WorkbookObservation`, `ChartObservation`, `EvidenceRegionV1`, `TrackBQuery`, and `TrackBMetrics` names remain consistent across tasks.
- [ ] Visual coordinates are never fabricated; Track B emits native locators until an approved renderer produces a hash-bound artifact.
- [ ] The synthetic fixtures contain Korean text and chart relations but no company-derived data.
- [ ] The full test command includes common contract conformance and all Track B tests.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-11-24-track-b-native-tables-charts.md`.

Execution options after all five plans are approved:

1. **Subagent-Driven (recommended):** use `superpowers:subagent-driven-development`, dispatch a fresh implementer per task, and run specification plus code-quality review between tasks.
2. **Inline Execution:** use `superpowers:executing-plans`, execute task batches with review checkpoints, and stop before any real-document or unapproved-artifact run.
