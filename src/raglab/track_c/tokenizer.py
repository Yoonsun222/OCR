"""Bounded, deterministic lexical analysis for mixed Korean, English, and Japanese."""

from collections.abc import Iterator
from itertools import groupby, islice
from unicodedata import normalize

MAX_CHARACTERS = 8_192
MAX_TOKENS = 2_048


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _tokens(text: str) -> Iterator[str]:
    for alphanumeric, characters in groupby(text, key=str.isalnum):
        if not alphanumeric:
            continue
        run = "".join(characters)
        if len(run) >= 3 and all(map(_is_cjk, run)):
            for index in range(len(run) - 2):
                yield run[index : index + 3]
        else:
            yield run


def analyze(text: str) -> tuple[str, ...]:
    """Return normalized words and overlapping trigrams for unmixed CJK runs."""
    normalized = normalize("NFKC", text[:MAX_CHARACTERS]).casefold()
    return tuple(islice(_tokens(normalized), MAX_TOKENS))
