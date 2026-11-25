import pytest

from raglab.track_a.matcher import matching_tokens, parse_part_query
from raglab.track_a.models import OcrToken
from raglab.track_a.normalization import normalize_label


@pytest.mark.parametrize(
    ("question", "label"),
    [
        ("A-12는 어디에 있어?", "a-12"),
        ("Where is A-12?", "a-12"),
        ("部品Cはどこですか？", "部品c"),
    ],
)
def test_multilingual_templates_preserve_raw_question(question: str, label: str) -> None:
    query = parse_part_query(question)
    assert query.raw == question
    assert query.label == label


def test_label_normalization_handles_width_case_and_dash_variants() -> None:
    assert normalize_label(" Ａ－１２ ") == "a-12"
    assert normalize_label("A−12") == "a-12"
    assert normalize_label("A‐12") == "a-12"


def test_match_compares_entire_ocr_tokens_and_preserves_order() -> None:
    tokens = (
        OcrToken("Ａ－１２", "en", (0.1, 0.1, 0.1, 0.05), 0.9),
        OcrToken("A-120", "en", (0.3, 0.1, 0.1, 0.05), 0.9),
        OcrToken("A- 12", "en", (0.5, 0.1, 0.1, 0.05), 0.9),
        OcrToken("X-A-12", "en", (0.6, 0.1, 0.1, 0.05), 0.9),
        OcrToken("a-12", "en", (0.7, 0.1, 0.1, 0.05), 0.9),
        OcrToken(". A-12 .", "en", (0.8, 0.1, 0.1, 0.05), 0.9),
    )
    assert matching_tokens(parse_part_query("Where is A-12?"), tokens) == (tokens[0], tokens[4])


@pytest.mark.parametrize(
    "question",
    [
        "Find the part that controls pressure",
        "Before we start, where is A-12?",
        "Where is A-12 and B-12?",
        "Where is ...?",
    ],
)
def test_requires_a_complete_explicit_label_template(question: str) -> None:
    with pytest.raises(ValueError, match="explicit-label"):
        parse_part_query(question)
