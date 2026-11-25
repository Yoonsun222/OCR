import pytest

from raglab.track_a.models import OcrToken, TargetRegion
from raglab.track_a.ranker import rank_candidates


def rect(name: str, x: float) -> TargetRegion:
    return TargetRegion(name, ((x, .2), (x + .1, .2), (x + .1, .4), (x, .4)), "part")


def test_distance_order_and_limit() -> None:
    token = OcrToken("A", "en", (.1, .25, .08, .04), .98)
    targets = (rect("far", .7), rect("near", .2), rect("x", .85), rect("middle", .45))
    ranked = rank_candidates((token,), targets, 3)

    assert tuple(item.target.region_id for item in ranked) == ("near", "middle", "far")
    assert ranked[0].score > ranked[1].score > ranked[2].score


@pytest.mark.parametrize("limit", [0, 4, True, 2.5])
def test_limit_requires_an_integer_from_one_to_three(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        rank_candidates((), (), limit)


def test_scores_combine_distance_confidence_and_containment() -> None:
    target = TargetRegion("part", ((.25, .25), (.5, .25), (.5, .5), (.25, .5)), "part")
    inside = OcrToken("A", "en", (.3125, .3125, .125, .125), .8)
    outside = OcrToken("A", "en", (.5625, .3125, .125, .125), .8)
    capped = OcrToken("A", "en", inside.box, 1)

    ranked = rank_candidates((outside, inside, capped), (target,))

    assert tuple(item.token for item in ranked) == (capped, inside, outside)
    assert tuple(item.score for item in ranked) == pytest.approx((1, .97, .695))


def test_concave_polygon_excludes_label_in_open_notch() -> None:
    target = TargetRegion(
        "part",
        ((.125, .125), (.875, .125), (.875, .875), (.625, .875),
         (.625, .375), (.375, .375), (.375, .875), (.125, .875)),
        "part",
    )
    label = OcrToken("A", "en", (.4375, .53125, .125, .125), .8)

    (candidate,) = rank_candidates((label,), (target,), limit=1)

    assert candidate.score == pytest.approx(.848125)


def test_equal_scores_keep_alternatives_ordered_by_target_and_token() -> None:
    z_token = OcrToken("Z", "en", (.1, .25, .08, .04), .98)
    a_token = OcrToken("A", "en", z_token.box, z_token.confidence)
    z_target, a_target = rect("z", .2), rect("a", .2)

    ranked = rank_candidates((z_token, a_token), (z_target, a_target))

    assert ranked == rank_candidates((a_token, z_token), (a_target, z_target))
    assert len({item.score for item in ranked}) == 1
    assert tuple((item.target.region_id, item.token.text) for item in ranked) == (
        ("a", "A"), ("a", "Z"), ("z", "A"),
    )


def test_empty_inputs_have_no_candidates() -> None:
    token = OcrToken("A", "en", (.1, .25, .08, .04), .98)

    assert rank_candidates((token,), ()) == ()
    assert rank_candidates((), (rect("near", .2),)) == ()
