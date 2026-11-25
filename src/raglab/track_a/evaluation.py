"""Score frozen Track A case annotations and ranked observations."""

from dataclasses import asdict, dataclass
from math import ceil, isfinite
from typing import Literal

from .models import Box, validate_box
from .normalization import normalize_label

State = Literal["VERIFIED", "PART_ABSENT", "ABSTAIN"]
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
    language: str
    scan_quality: str
    expected_state: State
    expected_label: str
    expected_box: Box | None

    def __post_init__(self) -> None:
        _require_text(self.case_id, self.language, self.scan_quality, self.expected_label)
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
    unsupported_critical_claims: int
    acl_leaks: int = 0
    outbound_events: int = 0
    tampered_artifact_loads: int = 0

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
        for field in ("peak_working_set_bytes", *_SECURITY_COUNTERS):
            count = getattr(self, field)
            if type(count) is not int or count < 0:
                raise ValueError(f"{field} must be a nonnegative integer")


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
    unsupported_critical_claims: int
    acl_leaks: int
    outbound_events: int
    tampered_artifact_loads: int
    security_gate_passed: bool
    status: Literal["EXPLORATORY", "DECISION"]
    slices: dict[str, dict[str, int]]

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
    totals = {
        field: sum(getattr(observation, field) for observation in observations)
        for field in _SECURITY_COUNTERS
    }
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
        security_gate_passed=not any(totals.values()),
        status="DECISION" if decision_eligible else "EXPLORATORY",
        slices=slices,
    )
