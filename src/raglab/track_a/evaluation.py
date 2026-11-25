"""Score frozen Track A case annotations and ranked observations."""

from dataclasses import asdict, dataclass
from math import ceil, isfinite
from typing import Literal

from .models import Box, validate_box
from .normalization import normalize_label

State = Literal["VERIFIED", "PART_ABSENT", "ABSTAIN"]
Language = Literal["en", "ko", "ja"]
ScanQuality = Literal["native", "clean_scan", "degraded_scan", "synthetic-duplicate"]
SecurityGateStatus = Literal["NOT_RUN", "PASS", "FAIL"]

_LANGUAGES = ("en", "ko", "ja")
_SCAN_QUALITIES = ("native", "clean_scan", "degraded_scan", "synthetic-duplicate")
_STATES = ("VERIFIED", "PART_ABSENT", "ABSTAIN")
_SECURITY_COUNTERS = (
    "acl_leaks", "outbound_events", "tampered_artifact_loads", "unsupported_critical_claims",
)


def _require_text(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("evaluation text must be nonempty")


def _box(value: Box) -> Box:
    try:
        box = tuple(value)
    except TypeError:
        raise ValueError("evaluation box must have four coordinates") from None
    validate_box(box)
    return box


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    language: Language
    scan_quality: ScanQuality
    expected_state: State
    expected_label: str
    expected_box: Box | None

    def __post_init__(self) -> None:
        _require_text(self.case_id, self.expected_label)
        if type(self.language) is not str or self.language not in _LANGUAGES:
            raise ValueError("unsupported evaluation language")
        if type(self.scan_quality) is not str or self.scan_quality not in _SCAN_QUALITIES:
            raise ValueError("unsupported evaluation scan quality")
        if self.expected_state not in _STATES:
            raise ValueError("unknown expected response state")
        if (self.expected_state == "VERIFIED") != (self.expected_box is not None):
            raise ValueError("exactly the verified cases require a reference box")
        if self.expected_box is not None:
            object.__setattr__(self, "expected_box", _box(self.expected_box))


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    """Candidate labels and target boxes share rank indices."""

    case_id: str
    response_state: State
    candidate_labels: tuple[str, ...]
    candidate_boxes: tuple[Box, ...]
    latency_seconds: float
    peak_working_set_bytes: int
    unsupported_critical_claims: int | None = None
    acl_leaks: int | None = None
    outbound_events: int | None = None
    tampered_artifact_loads: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.case_id)
        if self.response_state not in _STATES:
            raise ValueError("unknown observed response state")
        if isinstance(self.candidate_labels, str):
            raise ValueError("candidate labels must be a sequence of strings")
        try:
            labels = tuple(self.candidate_labels)
            boxes = tuple(_box(box) for box in self.candidate_boxes)
        except TypeError:
            raise ValueError("candidate labels and boxes must be sequences") from None
        _require_text(*labels)
        if len(labels) != len(boxes):
            raise ValueError("candidate labels and boxes must be paired by rank")
        object.__setattr__(self, "candidate_labels", labels)
        object.__setattr__(self, "candidate_boxes", boxes)
        latency = self.latency_seconds
        if type(latency) not in (float, int) or latency < 0 or not isfinite(latency):
            raise ValueError("latency must be finite and nonnegative")
        if type(self.peak_working_set_bytes) is not int or self.peak_working_set_bytes < 0:
            raise ValueError("peak working set must be a nonnegative integer")
        for field in _SECURITY_COUNTERS:
            count = getattr(self, field)
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError(f"{field} must be a nonnegative integer or unmeasured")


@dataclass(frozen=True, slots=True)
class TrackAMetrics:
    label_recall_at_3: float
    target_recall_at_3: float
    absent_precision: float
    absent_recall: float
    abstention_rate: float
    abstention_recall: float
    response_state_accuracy: float
    p50_latency_seconds: float
    p95_latency_seconds: float
    maximum_working_set_bytes: int
    unsupported_critical_claims: int | None
    acl_leaks: int | None
    outbound_events: int | None
    tampered_artifact_loads: int | None
    security_gate_status: SecurityGateStatus
    status: Literal["EXPLORATORY", "DECISION"]
    slices: dict[str, dict[str, int]]

    @property
    def security_gate_passed(self) -> bool | None:
        if self.security_gate_status == "NOT_RUN":
            return None
        return self.security_gate_status == "PASS"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _iou(a: Box, b: Box) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    width = max(0., min(ax + aw, bx + bw) - max(ax, bx))
    height = max(0., min(ay + ah, by + bh) - max(ay, by))
    intersection = width * height
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.


def evaluate(
    cases: tuple[EvaluationCase, ...],
    observations: tuple[EvaluationObservation, ...],
    *,
    dataset: Literal["synthetic", "frozen_blind"] = "synthetic",
    power_analysis_approved: bool = False,
) -> TrackAMetrics:
    """Aggregate point estimates and report sample eligibility and observed violations."""
    if dataset not in ("synthetic", "frozen_blind") or type(power_analysis_approved) is not bool:
        raise ValueError("invalid evaluation dataset declaration")
    if not cases:
        raise ValueError("evaluation requires at least one case")
    case_ids = {case.case_id for case in cases}
    by_id = {observation.case_id: observation for observation in observations}
    if len(case_ids) != len(cases) or len(by_id) != len(observations) or case_ids != by_id.keys():
        raise ValueError("case and observation IDs must be unique and match")

    slices: dict[str, dict[str, int]] = {}
    expected = dict.fromkeys(_STATES, 0)
    observed = dict.fromkeys(_STATES, 0)
    correct = dict.fromkeys(_STATES, 0)
    label_hits = target_hits = 0
    for case in cases:
        observation = by_id[case.case_id]
        expected[case.expected_state] += 1
        observed[observation.response_state] += 1
        correct[case.expected_state] += int(case.expected_state == observation.response_state)

        counts = slices.setdefault(f"{case.language}:{case.scan_quality}", {"cases": 0, "verified": 0})
        counts["cases"] += 1
        counts["verified"] += int(observation.response_state == "VERIFIED")

        if case.expected_box is not None:
            label = normalize_label(case.expected_label)
            matching_boxes = [
                box for candidate, box in zip(observation.candidate_labels[:3], observation.candidate_boxes[:3])
                if normalize_label(candidate) == label
            ]
            label_hits += bool(matching_boxes)
            target_hits += any(_iou(case.expected_box, box) >= .50 for box in matching_boxes)

    latency = sorted(observation.latency_seconds for observation in observations)
    totals: dict[str, int | None] = {}
    coverage: dict[str, int] = {}
    for field in _SECURITY_COUNTERS:
        counts = [getattr(observation, field) for observation in observations]
        measured = [count for count in counts if count is not None]
        coverage[field] = len(measured)
        totals[field] = sum(measured) if measured else None

    security_status: SecurityGateStatus = "NOT_RUN"
    if any(totals.values()):
        security_status = "FAIL"
    elif all(coverage.values()) and coverage["unsupported_critical_claims"] == len(observations):
        security_status = "PASS"
    decision_eligible = dataset == "frozen_blind" and (len(cases) >= 150 or power_analysis_approved)
    return TrackAMetrics(
        label_recall_at_3=_ratio(label_hits, expected["VERIFIED"]),
        target_recall_at_3=_ratio(target_hits, expected["VERIFIED"]),
        absent_precision=_ratio(correct["PART_ABSENT"], observed["PART_ABSENT"]),
        absent_recall=_ratio(correct["PART_ABSENT"], expected["PART_ABSENT"]),
        abstention_rate=_ratio(observed["ABSTAIN"], len(cases)),
        abstention_recall=_ratio(correct["ABSTAIN"], expected["ABSTAIN"]),
        response_state_accuracy=_ratio(sum(correct.values()), len(cases)),
        p50_latency_seconds=latency[ceil(len(latency) * .50) - 1],
        p95_latency_seconds=latency[ceil(len(latency) * .95) - 1],
        maximum_working_set_bytes=max(observation.peak_working_set_bytes for observation in observations),
        **totals,
        security_gate_status=security_status,
        status="DECISION" if decision_eligible else "EXPLORATORY",
        slices=slices,
    )
