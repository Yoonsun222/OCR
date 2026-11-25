import json
from pathlib import Path

import pytest

from raglab.contracts import EvidenceRole
from raglab.track_b.charts import parse_workbook_charts
from raglab.track_b.evaluate import EvaluationCase, evaluate_cases
from raglab.track_b.evidence import workbook_evidence
from raglab.track_b.opc import OpcPackage
from raglab.track_b.query import TrackBQuery, answer_query
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_chart_workbook, write_native_workbook


def test_metrics_exclude_repeats_and_measure_abstention_precision():
    cases = (
        EvaluationCase("cell", "12.5", "12.5", True),
        EvaluationCase("cell", "7.5", "8.0", True),
        EvaluationCase("cell", "7.5", "8.0", False),
        EvaluationCase("chart_relation", "Data!B2:B5", "Data!B2:B5", True),
        EvaluationCase("abstain", None, None, True),
        EvaluationCase("abstain", "supported", None, True),
        EvaluationCase("abstain", None, "unsupported answer", True),
    )
    metrics = evaluate_cases(cases, elapsed_seconds=.25)
    assert json.loads(json.dumps(metrics.to_dict(), allow_nan=False)) == {
        "cell_value_accuracy": .5,
        "chart_relation_accuracy": 1.0,
        "calculation_accuracy": None,
        "abstention_precision": .5,
        "elapsed_seconds": .25,
    }
    assert evaluate_cases((), 0).abstention_precision is None


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf"), True])
def test_metrics_reject_invalid_elapsed(elapsed):
    with pytest.raises(ValueError, match="elapsed"):
        evaluate_cases((), elapsed)


def _evidence(path: Path):
    with OpcPackage.open(path) as package:
        workbook = parse_workbook(package)
        charts = parse_workbook_charts(package, workbook)
        return workbook_evidence(workbook, charts, "doc-v1", "gen-1", "acl-1")


def test_golden_metrics_consume_real_synthetic_workbook_and_chart_outputs(tmp_path: Path):
    evidence = _evidence(write_native_workbook(tmp_path / "book.xlsx"))
    lookup = answer_query(TrackBQuery("lookup", "sheet-1", "A1"), evidence)
    aggregate = answer_query(TrackBQuery("sum", "sheet-1", "B2:B3"), evidence)
    missing = answer_query(TrackBQuery("lookup", "sheet-1", "D2"), evidence)
    assert (lookup.status, aggregate.status, missing.status) == ("answered", "answered", "판단 불가")

    charts = _evidence(write_chart_workbook(tmp_path / "chart.xlsx"))
    series = next(item for item in charts if item.role is EvidenceRole.SERIES)
    value_source = next(relation.target for relation in series.relations if relation.role is EvidenceRole.VALUE)
    metrics = evaluate_cases((
        EvaluationCase("cell", "매출", lookup.answer, True),
        EvaluationCase("calculation", "20.0", aggregate.answer, True),
        EvaluationCase("chart_relation", "'Data'!$B$2:$B$5", value_source, True),
        EvaluationCase("abstain", None, missing.answer, True),
        EvaluationCase("abstain", "매출", lookup.answer, True),
    ), 0)
    assert metrics.cell_value_accuracy == metrics.calculation_accuracy == metrics.chart_relation_accuracy == 1.0
    assert metrics.abstention_precision == 1.0
