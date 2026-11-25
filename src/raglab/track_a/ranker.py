from collections.abc import Iterator
from heapq import nsmallest
from math import hypot

from .models import GroundingCandidate, OcrToken, Point, TargetRegion


def _contains(point: Point, polygon: tuple[Point, ...]) -> bool:
    x, y = point
    inside = False
    x1, y1 = polygon[-1]
    for x2, y2 in polygon:
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        x1, y1 = x2, y2
    return inside


def _scores(
    tokens: tuple[OcrToken, ...], targets: tuple[TargetRegion, ...],
) -> Iterator[GroundingCandidate]:
    centers = []
    for target in targets:
        xs, ys = zip(*target.polygon)
        centers.append((target, sum(xs) / len(xs), sum(ys) / len(ys)))

    for token in tokens:
        x, y, width, height = token.box
        label = x + width / 2, y + height / 2
        for target, center_x, center_y in centers:
            distance = hypot(label[0] - center_x, label[1] - center_y)
            score = .65 * token.confidence + .35 * max(0.0, 1 - distance / .5)
            if _contains(label, target.polygon):
                score += .1
            yield GroundingCandidate(token, target, min(1.0, score))


def rank_candidates(
    tokens: tuple[OcrToken, ...], targets: tuple[TargetRegion, ...], limit: int = 3,
) -> tuple[GroundingCandidate, ...]:
    """Rank supported regions while retaining alternatives for the caller's decision."""
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("limit must be an integer between one and three")
    if not tokens or not targets:
        return ()
    return tuple(nsmallest(
        limit,
        _scores(tokens, targets),
        key=lambda item: (-item.score, item.target.region_id, item.token.text,
                          item.token.box, item.token.language, item.token.confidence,
                          item.target.polygon, item.target.kind),
    ))
