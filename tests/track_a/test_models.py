from dataclasses import FrozenInstanceError

import pytest

from raglab.track_a.models import GroundingResponse, OcrToken, TargetRegion


@pytest.mark.parametrize("box", [(0.9, 0.2, 0.2, 0.1), (0.1, float("nan"), 0.2, 0.1)])
def test_ocr_token_rejects_invalid_geometry(box) -> None:
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
