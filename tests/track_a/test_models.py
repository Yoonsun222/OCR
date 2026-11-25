from dataclasses import FrozenInstanceError

import pytest

from raglab.track_a.models import GroundingResponse, OcrToken, TargetRegion


@pytest.mark.parametrize("box", [(0.9, 0.2, 0.2, 0.1), (0.1, float("nan"), 0.2, 0.1)])
def test_ocr_token_rejects_invalid_geometry(box) -> None:
    with pytest.raises(ValueError, match="normalized box"):
        OcrToken("A", "en", box, 1.0)


@pytest.mark.parametrize("box", [(.5, .5, 1e-20, .1), (.5, .5, .1, 1e-20)])
def test_ocr_token_rejects_box_whose_corners_round_together(box) -> None:
    with pytest.raises(ValueError, match="normalized box"):
        OcrToken("A", "en", box, 1.0)


@pytest.mark.parametrize("polygon", [((0.2, 0.2),) * 3, ((0., 0.), (.5, .5), (1., 1.))])
def test_target_rejects_zero_area(polygon) -> None:
    with pytest.raises(ValueError, match="positive area"):
        TargetRegion("r", polygon, "part")


def test_model_copies_mutable_polygon() -> None:
    points = [[0., 0.], [1., 0.], [.5, .5]]
    target = TargetRegion("r", points, "part")
    points[0][0] = .5
    assert target.polygon[0] == (0., 0.)
    with pytest.raises(FrozenInstanceError):
        target.kind = "other"


def test_verified_response_requires_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        GroundingResponse("VERIFIED", (), "reason")


@pytest.mark.parametrize("polygon", [
    ((0., 0.), (.8, .8), (0., .8), (1., 0.)),
    ((0., 0.), (1., 0.), (1., 1.), (1., 0.), (0., 1.)),
    ((0., 0.), (1., 0.), (.5, 0.), (.5, 1.), (0., 1.)),
    ((0., 0.), (1., 0.), (1., 1.), (.5, 0.), (0., 1.)),
], ids=["crossing", "repeated", "overlap", "touch"])
def test_target_rejects_nonsimple_polygon(polygon) -> None:
    with pytest.raises(ValueError, match="simple polygon"):
        TargetRegion("r", polygon, "part")


def test_closed_polygon_is_stored_without_duplicate_endpoint() -> None:
    points = ((0., 0.), (1., 0.), (.5, .5), (1., 1.), (0., 1.))
    target = TargetRegion("r", points + points[:1], "part")
    assert target.polygon == points


def test_target_bounds_polygon_validation_work() -> None:
    def unbounded_points():
        for _ in range(258):
            yield 0., 0.
        pytest.fail("polygon validation read past the vertex limit")

    with pytest.raises(ValueError, match="vertex limit"):
        TargetRegion("r", unbounded_points(), "part")
