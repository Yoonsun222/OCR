"""Normalize visible labels for exact comparison."""

import unicodedata


def normalize_label(value: str) -> str:
    """Fold width, case, and dashes while preserving internal whitespace."""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("−", "-").replace("‐", "-")
    value = value.strip()
    unpunctuated = value.strip("?.,")
    return unpunctuated if unpunctuated == unpunctuated.strip() else value
