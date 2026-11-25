from dataclasses import asdict, dataclass
from math import isfinite


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


def evaluate_cases(cases: tuple[EvaluationCase, ...], elapsed_seconds: float) -> TrackBMetrics:
    """Score independent observations. An absent denominator yields None."""
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("elapsed time must be finite and nonnegative") from None
    if type(elapsed_seconds) not in (int, float) or not isfinite(elapsed) or elapsed < 0:
        raise ValueError("elapsed time must be finite and nonnegative")

    totals = dict.fromkeys(("cell", "calculation", "chart_relation", "abstain"), 0)
    correct = totals.copy()
    for case in cases:
        if case.category not in totals:
            raise ValueError("unknown evaluation category")
        if not case.independent_observation or (case.category == "abstain" and case.actual is not None):
            continue
        totals[case.category] += 1
        correct[case.category] += case.actual == case.expected

    def rate(category: str) -> float | None:
        return correct[category] / totals[category] if totals[category] else None

    return TrackBMetrics(rate("cell"), rate("calculation"), rate("chart_relation"), rate("abstain"), elapsed)
