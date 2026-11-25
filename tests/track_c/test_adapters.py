from dataclasses import replace

import pytest

from raglab.contracts import EvidenceRole
from raglab.track_a.evidence import candidate_to_evidence
from raglab.track_b.charts import parse_workbook_charts
from raglab.track_b.evidence import workbook_evidence
from raglab.track_b.opc import OpcPackage
from raglab.track_b.workbook import parse_workbook
from raglab.track_c.adapters import from_track_a, from_track_b
from tests.fixtures.xlsx_factory import write_chart_workbook
from tests.track_a.test_evidence import _inputs
from tests.track_c.helpers import evidence


def test_visual_adapter_preserves_label_target_relation():
    candidate, provenance = _inputs()
    original = candidate_to_evidence(candidate, provenance)
    record = from_track_a(original, "doc-a")

    assert record.document_id == "doc-a"
    assert record.channel == "visual"
    assert record.evidence is original
    assert record.searchable_text == "A-12"
    assert record.relation_kinds == ("label_targets",)
    assert from_track_a(replace(original, relations=()), "doc-a").relation_kinds == ()


def test_visual_adapter_indexes_explicit_relation_metadata():
    original = replace(evidence(metadata={"normalized_label": "A", "relation_kind": "label_targets"}), text=None)
    record = from_track_a(original, "doc-a")

    assert record.relation_kinds == ("label_targets",)
    assert record.searchable_text == "A"
    assert record.evidence is original


@pytest.fixture
def workbook_and_charts(tmp_path):
    with OpcPackage.open(write_chart_workbook(tmp_path / "chart.xlsx")) as package:
        workbook = parse_workbook(package)
        return workbook, parse_workbook_charts(package, workbook)


def test_chart_adapter_indexes_series_and_typed_source_range(workbook_and_charts):
    workbook, charts = workbook_and_charts
    _, original, _ = workbook_evidence(workbook, charts, "v1", "gen-1", "acl-1")
    record = from_track_b(original, "doc-b")

    assert record.channel == "structure"
    assert record.document_id == "doc-b"
    assert record.evidence is original
    assert record.evidence.regions == ()
    assert record.structured_key == "'Data'!$B$2:$B$5"
    assert record.structured_value == original.text
    assert record.relation_kinds == ("legend", "series", "value")
    assert all(relation.target in record.searchable_text for relation in original.relations)


def test_chart_adapter_does_not_promote_other_relation_targets(workbook_and_charts):
    workbook, charts = workbook_and_charts
    _, original, _ = workbook_evidence(workbook, charts, "v1", "gen-1", "acl-1")
    original = replace(
        original, text=None, metadata={},
        relations=tuple(relation for relation in original.relations if relation.role is not EvidenceRole.VALUE),
    )
    record = from_track_b(original, "doc-b")

    assert record.structured_key == "series"
    assert record.structured_value is None
    assert record.relation_kinds == ("legend", "series")


def test_native_adapter_handles_explicit_source_range():
    original = evidence(metadata={"chart_title": "Pressure", "source_range": "Data!$B$2:$B$5"})
    record = from_track_b(original, "doc-b")

    assert record.structured_key == "Data!$B$2:$B$5"
    assert "Pressure" in record.searchable_text
    assert record.evidence is original


def test_cell_adapter_indexes_locator_display_label_and_unit():
    original = evidence(metadata={"label": "Pressure", "unit": "MPa", "formula": "A1+A2"})
    record = from_track_b(original, "doc-b")

    assert record.structured_key == "B7"
    assert record.structured_value == "12.5"
    assert record.unit == "MPa"
    assert record.searchable_text == "12.5 Pressure MPa"
    assert record.evidence is original
