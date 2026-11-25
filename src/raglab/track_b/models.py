"""Passive observations of native workbook data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType


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
class TableObservation:
    table_id: str
    name: str
    display_name: str
    reference: str
    header_row_count: int
    totals_row_count: int


@dataclass(frozen=True)
class SheetObservation:
    stable_id: str
    relationship_id: str
    name: str
    state: str
    part_name: str
    cells: Mapping[str, CellObservation]
    merged_ranges: tuple[str, ...] = ()
    hidden_rows: frozenset[int] = frozenset()
    hidden_columns: frozenset[str] = frozenset()
    auto_filter_range: str | None = None
    tables: tuple[TableObservation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", MappingProxyType(dict(self.cells)))


@dataclass(frozen=True)
class WorkbookObservation:
    source_hash: str
    date_system: str
    sheets: tuple[SheetObservation, ...]


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
    source_hash: str
    sheet_part_name: str
