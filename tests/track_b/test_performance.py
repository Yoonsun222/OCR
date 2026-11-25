from time import perf_counter

from raglab.track_b.evidence import workbook_evidence
from raglab.track_b.opc import OpcPackage
from raglab.track_b.query import TrackBQuery, answer_query
from raglab.track_b.workbook import parse_workbook
from tests.fixtures.xlsx_factory import write_native_workbook


def test_synthetic_native_lookup_pipeline_finishes_under_five_seconds(tmp_path):
    source = write_native_workbook(tmp_path / "book.xlsx")
    started = perf_counter()
    with OpcPackage.open(source) as package:
        workbook = parse_workbook(package)
    evidence = workbook_evidence(workbook, (), "doc-v1", "gen-1", "acl-1")
    answer = answer_query(TrackBQuery("lookup", "sheet-1", "B2"), evidence)
    assert answer.answer == "12.5"
    assert perf_counter() - started < 5.0
