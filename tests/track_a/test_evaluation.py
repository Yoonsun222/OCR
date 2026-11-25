import json
import socket
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import pytest

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.contracts.evidence import EvidenceRole, NativeLocator
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore
from raglab.track_a.evaluation import EvaluationCase, EvaluationObservation, evaluate
from raglab.track_a.models import ArtifactProvenance, GroundingRequest
from raglab.track_a.normalization import normalize_label
from raglab.track_a.service import ApprovedArtifact, GroundingService
from raglab.track_a.svg_regions import read_svg_regions
from raglab.track_a.tesseract_tsv import parse_tsv

FIXTURES = Path(__file__).parent / "fixtures"
GOLD_BOX = (.25, .25, .25, .25)


def test_target_recall_requires_label_and_iou_in_the_same_top_three_candidate() -> None:
    cases = tuple(
        EvaluationCase(str(i), "en", "native", "VERIFIED", "a-12", GOLD_BOX)
        for i in range(3)
    )
    distant = (.75, .75, .25, .25)
    half_iou = (.25, .25, .125, .25)
    observations = (
        EvaluationObservation("0", "VERIFIED", ("Ａ－１２", "wrong"), (distant, GOLD_BOX), 3., 90, 0),
        EvaluationObservation("1", "VERIFIED", ("wrong", "wrong", "wrong", "A-12"),
                              (GOLD_BOX, GOLD_BOX, GOLD_BOX, GOLD_BOX), 1., 100, 0),
        EvaluationObservation("2", "VERIFIED", ("wrong", "A-12"), (distant, half_iou), 2., 80, 0),
    )

    metrics = evaluate(cases, observations)

    assert metrics.label_recall_at_3 == pytest.approx(2 / 3)
    assert metrics.target_recall_at_3 == pytest.approx(1 / 3)
    assert metrics.p50_latency_seconds == 2.
    assert metrics.p95_latency_seconds == 3.
    assert metrics.maximum_working_set_bytes == 100


def test_absent_and_abstain_are_scored_by_exact_state() -> None:
    case_states = ("PART_ABSENT", "PART_ABSENT", "ABSTAIN", "ABSTAIN")
    response_states = ("PART_ABSENT", "ABSTAIN", "ABSTAIN", "PART_ABSENT")
    cases = tuple(
        EvaluationCase(str(i), "ja", "native", state, "部品c", None)
        for i, state in enumerate(case_states)
    )
    observations = tuple(
        EvaluationObservation(str(i), state, (), (), 1., 10, 0)
        for i, state in enumerate(response_states)
    )

    metrics = evaluate(cases, observations)

    assert metrics.absent_precision == metrics.absent_recall == .5
    assert metrics.abstention_rate == metrics.abstention_recall == .5
    assert metrics.response_state_accuracy == .5


@pytest.mark.parametrize(
    ("dataset", "size", "power_analysis_approved", "expected"),
    [
        ("synthetic", 150, True, "EXPLORATORY"),
        ("frozen_blind", 149, False, "EXPLORATORY"),
        ("frozen_blind", 150, False, "DECISION"),
        ("frozen_blind", 1, True, "DECISION"),
    ],
)
def test_decision_eligibility_depends_on_data_and_sample_size(
    dataset, size, power_analysis_approved, expected
) -> None:
    cases = tuple(
        EvaluationCase(str(i), "en", "native", "PART_ABSENT", "a-12", None)
        for i in range(size)
    )
    observations = tuple(
        EvaluationObservation(case.case_id, "PART_ABSENT", (), (), .1, 10, 0)
        for case in cases
    )

    assert evaluate(
        cases, observations, dataset=dataset, power_analysis_approved=power_analysis_approved
    ).status == expected


def test_security_counters_are_independent_hard_gates() -> None:
    case = EvaluationCase("c", "en", "native", "VERIFIED", "a-12", GOLD_BOX)
    clean = EvaluationObservation("c", "VERIFIED", ("a-12",), (GOLD_BOX,), .1, 10, 0)
    assert evaluate((case,), (clean,)).security_gate_passed
    for counter in ("acl_leaks", "outbound_events", "tampered_artifact_loads", "unsupported_critical_claims"):
        metrics = evaluate((case,), (replace(clean, **{counter: 1}),))
        assert not metrics.security_gate_passed
        assert metrics.to_dict()[counter] == 1


def test_invalid_frozen_inputs_cannot_silently_distort_metrics() -> None:
    case = EvaluationCase("c", "ko", "native", "VERIFIED", "부품b", GOLD_BOX)
    observation = EvaluationObservation("c", "VERIFIED", ("부품B",), (GOLD_BOX,), .1, 10, 0)
    for cases, observations in (
        ((), ()),
        ((case,), ()),
        ((case, case), (observation,)),
        ((case,), (observation, observation)),
        ((case,), (replace(observation, case_id="other"),)),
    ):
        with pytest.raises(ValueError):
            evaluate(cases, observations)
    for invalid in (
        {"candidate_boxes": ()},
        {"candidate_boxes": ((.9, .9, .5, .5),)},
        {"latency_seconds": float("nan")},
        {"peak_working_set_bytes": -1},
        {"outbound_events": -1},
        {"response_state": "MISSING"},
    ):
        with pytest.raises(ValueError):
            replace(observation, **invalid)
    with pytest.raises(ValueError):
        replace(case, expected_box=None)


class RecordedOcr:
    def __init__(self, data, recorded, *, after_read=None):
        self.data = data
        self.recorded = recorded
        self.after_read = after_read
        self.reads = 0

    def read(self, data):
        self.reads += 1
        assert data == self.data
        tokens = parse_tsv(self.recorded, 1000, 500)
        if self.after_read:
            self.after_read()
        return tokens


class SyntheticGeometry:
    def __init__(self, path):
        self.path = path
        self.reads = 0

    def read(self, data):
        self.reads += 1
        self.path.write_bytes(data)
        return read_svg_regions(str(self.path))


def _authorized_fixture():
    store = MemoryPolicyStore()
    store.allow("synthetic-user", "synthetic-document", group_hash="a" * 64)
    guard = AuthorizationGuard.policy_only(store)
    pin = guard.pin("synthetic-user", "a" * 64, "synthetic-document", "v1", "gen-1")
    data = (FIXTURES / "parts_ko_en_ja.svg").read_bytes()
    fingerprint = sha256(data).hexdigest()
    provenance = ArtifactProvenance(
        fingerprint, pin.document_version, pin.generation_id, "fixture-artifact", fingerprint,
        "image/svg+xml", "synthetic-fixture", "1", "b" * 64, 1000, 500, 144, 0, "crop",
        ((1., 0., 0., 0., 1., 0., 0., 0., 1.),), NativeLocator.page(1), pin.query_snapshot_id,
    )
    approved = ApprovedArtifact(pin.document_id, "fixture.svg", provenance, data)
    return store, guard, pin, approved


def _peak_working_set_bytes() -> int:
    if sys.platform != "win32":
        from resource import RUSAGE_SELF, getrusage

        peak = getrusage(RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)

    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_nonpaged_pool_usage", ctypes.c_size_t),
            ("quota_nonpaged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
    get_memory.restype = wintypes.BOOL
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not get_memory(get_current_process(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters.peak_working_set_size


def _evaluate_recorded_fixture(tmp_path, monkeypatch):
    outbound_events = 0

    def block_network(*args, **kwargs):
        nonlocal outbound_events
        outbound_events += 1
        raise AssertionError("network access attempted in synthetic grounding")

    for operation in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, operation, block_network)

    store, guard, pin, approved = _authorized_fixture()

    def resolve(request_pin, artifact_id):
        return approved if (request_pin, artifact_id) == (pin, approved.provenance.artifact_id) else None

    fixture = json.loads((FIXTURES / "evaluation_cases.json").read_text(encoding="utf-8"))
    recorded = (FIXTURES / "mixed_labels.tsv").read_text(encoding="utf-8")
    # Place the recorded label centers inside their independently checked SVG targets.
    aligned = recorded.replace("\t410\t100\t90", "\t545\t160\t90").replace(
        "\t710\t100\t90", "\t830\t160\t90",
    )
    duplicate = "5\t1\t1\t1\t5\t1\t250\t350\t80\t20\t96\tA-12\n"
    cases, observations = [], []
    geometry = SyntheticGeometry(tmp_path / "approved.svg")
    for row in fixture:
        question = row.pop("question")
        layout = row.pop("recorded_layout", "baseline")
        case = EvaluationCase(**row)
        cases.append(case)
        tsv = aligned if layout == "aligned" else recorded
        if case.scan_quality == "synthetic-duplicate":
            tsv += duplicate
        service = GroundingService(guard, RecordedOcr(approved.data, tsv), geometry, resolver=resolve)
        request = GroundingRequest(question, approved.artifact_path, approved.provenance, pin)
        start = perf_counter()
        response = service.query(request)
        elapsed = perf_counter() - start
        assert response.state == case.expected_state, (case.case_id, response.reason)

        labels, boxes = [], []
        unsupported_critical_claims = 0
        for item in response.evidence:
            label, target = item.regions
            assert (label.role, target.role) == (EvidenceRole.LABEL, EvidenceRole.TARGET)
            assert item.relations[0].target == label.region_id
            assert item.relations[0].target_region_id == target.region_id
            assert item.source_hash == approved.provenance.source_hash
            assert item.acl_snapshot_id == pin.query_snapshot_id
            assert label.artifact_hash == target.artifact_hash == approved.provenance.artifact_hash
            labels.append(item.text)
            box = target.bbox
            coordinates = (box.x, box.y, box.width, box.height)
            boxes.append(coordinates)
            if (
                case.expected_box is None
                or normalize_label(item.text) != normalize_label(case.expected_label)
                or coordinates != pytest.approx(case.expected_box)
            ):
                unsupported_critical_claims += 1
        observations.append(EvaluationObservation(
            case.case_id, response.state, tuple(labels), tuple(boxes), elapsed,
            _peak_working_set_bytes(), unsupported_critical_claims,
        ))
    assert geometry.reads == 5

    secret = "synthetic-private-canary"
    tampered = replace(approved, data=secret.encode("ascii"))
    ocr = RecordedOcr(approved.data, recorded)
    service = GroundingService(guard, ocr, geometry, resolver=lambda *_: tampered)
    request = GroundingRequest("Where is A-12?", approved.artifact_path, approved.provenance, pin)
    with pytest.raises(RaglabError) as error:
        service.query(request)
    assert error.value.code is ErrorCode.ARTIFACT_HASH_MISMATCH
    tamper_error = json.dumps(error.value.to_safe_dict())
    tampered_artifact_loads = ocr.reads + geometry.reads - 5
    assert secret not in tamper_error

    ocr = RecordedOcr(approved.data, recorded, after_read=lambda: store.revoke(pin.document_id))
    service = GroundingService(guard, ocr, geometry, resolver=resolve)
    denied = replace(request, artifact_path=secret)
    with pytest.raises(RaglabError) as error:
        service.query(denied)
    assert error.value.code is ErrorCode.POLICY_DENIED
    assert ocr.reads == 0
    denial_error = json.dumps(error.value.to_safe_dict())
    with pytest.raises(RaglabError) as error:
        service.query(request)
    assert error.value.code is ErrorCode.AUTHORIZATION_CHANGED
    assert ocr.reads == 1 and geometry.reads == 5
    revocation_error = json.dumps(error.value.to_safe_dict())
    acl_leaks = sum(
        private in payload
        for private, payload in ((secret, denial_error), ("A-12", revocation_error))
    )
    observations[0] = replace(
        observations[0], acl_leaks=acl_leaks, outbound_events=outbound_events,
        tampered_artifact_loads=tampered_artifact_loads,
    )
    return evaluate(tuple(cases), tuple(observations))


def test_recorded_multilingual_grounding_and_safety_smoke(tmp_path, monkeypatch) -> None:
    metrics = _evaluate_recorded_fixture(tmp_path, monkeypatch)
    assert metrics.label_recall_at_3 == metrics.target_recall_at_3 == 1.
    assert metrics.absent_precision == metrics.absent_recall == metrics.abstention_recall == 1.
    assert metrics.abstention_rate == pytest.approx(3 / 7)
    assert metrics.response_state_accuracy == 1.
    assert metrics.security_gate_passed
    report = json.dumps(metrics.to_dict(), ensure_ascii=False)
    assert all(private not in report for private in ("synthetic-private-canary", "A-12", "부품B", "部品C"))
    assert metrics.maximum_working_set_bytes > 0
    assert metrics.slices["ja:native"] == {"cases": 3, "verified": 1}
    assert metrics.status == "EXPLORATORY"
