"""Parse explicit location questions and match complete OCR tokens."""

import re

from .models import OcrToken, PartQuery
from .normalization import normalize_label

_PATTERNS = (
    re.compile(r"\s*(?P<label>[^\s?？]{1,64})\s*는\s*어디에\s*있어\s*[?？]?\s*"),
    re.compile(r"\s*where\s+is\s+(?P<label>[^\s?？]{1,64})\s*\?\s*", re.IGNORECASE),
    re.compile(r"\s*(?P<label>[^\s?？]{1,64})\s*は\s*どこですか\s*[?？]?\s*"),
)


def parse_part_query(raw: str) -> PartQuery:
    for pattern in _PATTERNS:
        match = pattern.fullmatch(raw)
        if match:
            label = normalize_label(match.group("label"))
            if label and not any(character.isspace() for character in label):
                return PartQuery(raw, label)
    raise ValueError("question must use a supported explicit-label template")


def matching_tokens(query: PartQuery, tokens: tuple[OcrToken, ...]) -> tuple[OcrToken, ...]:
    return tuple(token for token in tokens if normalize_label(token.text) == query.label)
