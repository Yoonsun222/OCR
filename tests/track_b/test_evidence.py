import json
from dataclasses import replace
from pathlib import Path

import pytest

from raglab.contracts import EvidenceRegionV1, EvidenceRole, ExtractionMethod, NativeLocator
from raglab.track_b.charts import parse_workbook_charts
from raglab.track_b.errors import InvalidPackage
from raglab.track_b.evidence import workbook_evidence
from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_chart_workbook


@pytest.fixture
def chart_workbook(tmp_path: Path):
    with OpcPackage.open(write_chart_workbook(tmp_path / "chart.xlsx")) as package:
        workbook = parse_workbook(package)
        return workbook, parse_workbook_charts(package, workbook)


def test_cell_and_series_use_common_native_evidence(chart_workbook) -> None:
    workbook, charts = chart_workbook
    evidence = workbook_evidence(workbook, charts, "doc-v1", "gen-1", "acl-7")
    cell, series, literal_legend = evidence
    assert cell.native_locator == NativeLocator.sheet_range("sheet-1", "B2")
    assert cell.role is EvidenceRole.VALUE
    assert cell.text == "1"
    assert cell.metadata["sheet_state"] == "visible"
    assert series.native_locator == NativeLocator.sheet_object("sheet-1", "chart-1:series:0")
    assert series.role is EvidenceRole.SERIES
    assert {relation.role: relation.target for relation in series.relations} == {
        EvidenceRole.LEGEND: "'Data'!$B$1",
        EvidenceRole.SERIES: "'Data'!$A$2:$A$5",
        EvidenceRole.VALUE: "'Data'!$B$2:$B$5",
    }
    assert literal_legend.text == "제품 B"
    assert {relation.role for relation in literal_legend.relations} == {
        EvidenceRole.SERIES, EvidenceRole.VALUE,
    }
    for item in evidence:
        assert isinstance(item, EvidenceRegionV1)
        assert item.regions == ()
        assert item.extraction_method is ExtractionMethod.NATIVE
        assert (item.source_hash, item.document_version, item.generation_id, item.acl_snapshot_id) == (
            workbook.source_hash, "doc-v1", "gen-1", "acl-7",
        )
        assert all(relation.target_region_id is None for relation in item.relations)
        assert json.loads(json.dumps(item.to_dict(), allow_nan=False)) == item.to_dict()


def test_evidence_ids_are_stable_and_chart_ids_are_worksheet_local(chart_workbook) -> None:
    workbook, (chart,) = chart_workbook
    other_sheet = replace(
        workbook.sheets[0], stable_id="sheet-2", name="Other", part_name="xl/worksheets/sheet2.xml",
    )
    workbook = replace(workbook, sheets=(*workbook.sheets, other_sheet))
    other_chart = replace(
        chart, sheet_stable_id=other_sheet.stable_id, sheet_part_name=other_sheet.part_name,
        part_name="xl/charts/chart2.xml",
    )
    charts = (chart, other_chart)
    evidence = workbook_evidence(workbook, charts, "doc-v1", "gen-1", "acl-7")
    assert evidence == workbook_evidence(workbook, charts, "doc-v1", "gen-1", "acl-7")
    assert len({item.evidence_id for item in evidence}) == len(evidence)


@pytest.mark.parametrize(
    "formula",
    ["'[Other.xlsx]Data'!B2", "'Data:Other'!B2", "Missing!B2", "Data!XFE1",
     "Data!A1048577", "Data!B5:B2", "Data!NamedRange", "=SUM(Data!B2)", "B2"],
)
def test_omits_unproven_chart_relations(chart_workbook, formula: str) -> None:
    workbook, (chart,) = chart_workbook
    first = replace(chart.series[0], value_formula=formula)
    chart = replace(chart, series=(first,))
    _, series = workbook_evidence(workbook, (chart,), "doc-v1", "gen-1", "acl-7")
    assert {relation.role for relation in series.relations} == {EvidenceRole.LEGEND, EvidenceRole.SERIES}


def test_accepts_explicit_local_reference_with_quoted_sheet_name(chart_workbook) -> None:
    workbook, (chart,) = chart_workbook
    sheet = replace(workbook.sheets[0], name="O'Brien Data")
    workbook = replace(workbook, sheets=(sheet,))
    reference = "='O''Brien Data'!$b$2"
    series = replace(chart.series[0], value_formula=reference)
    chart = replace(chart, series=(series,))
    _, evidence = workbook_evidence(workbook, (chart,), "doc-v1", "gen-1", "acl-7")
    assert [(relation.role, relation.target) for relation in evidence.relations] == [
        (EvidenceRole.VALUE, reference),
    ]


@pytest.mark.parametrize(
    "changes",
    [{"source_hash": "b" * 64}, {"sheet_stable_id": "sheet-99"}, {"sheet_part_name": "wrong.xml"}],
)
def test_rejects_chart_with_mismatched_source_or_owner(chart_workbook, changes) -> None:
    workbook, (chart,) = chart_workbook
    with pytest.raises(InvalidPackage, match="chart evidence"):
        workbook_evidence(workbook, (replace(chart, **changes),), "doc-v1", "gen-1", "acl-7")


def test_rejects_mixed_workbook_sheet_sources(chart_workbook) -> None:
    workbook, _ = chart_workbook
    sheet = replace(workbook.sheets[0], source_hash="b" * 64)
    with pytest.raises(InvalidPackage, match="sheet evidence"):
        workbook_evidence(replace(workbook, sheets=(sheet,)), (), "doc-v1", "gen-1", "acl-7")
