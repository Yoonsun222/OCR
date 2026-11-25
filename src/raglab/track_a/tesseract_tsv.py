"""Parse recorded Tesseract word rows without invoking an OCR engine."""

import csv
import io
import math

from .models import OcrToken

_MAX_CHARACTERS = 1024 * 1024
_HEADER = (
    "level", "page_num", "block_num", "par_num", "line_num", "word_num",
    "left", "top", "width", "height", "conf", "text",
)


def parse_tsv(text: str, width: int, height: int) -> tuple[OcrToken, ...]:
    if any(type(size) is not int or size <= 0 for size in (width, height)):
        raise ValueError("artifact dimensions must be positive integers")
    if not isinstance(text, str) or len(text) > _MAX_CHARACTERS:
        raise ValueError("recorded TSV exceeds the input limit")
    rows = csv.reader(io.StringIO(text, newline=""), delimiter="\t", quoting=csv.QUOTE_NONE)
    result: list[OcrToken] = []
    page_number: int | None = None
    try:
        if tuple(next(rows, ())) != _HEADER:
            raise ValueError("invalid header")
        for row in rows:
            if len(row) != len(_HEADER) or row[0] not in ("1", "2", "3", "4", "5"):
                raise ValueError("invalid row")
            page = row[1]
            if not page.isascii() or not page.isdecimal():
                raise ValueError("invalid page number")
            current_page = int(page)
            if current_page <= 0 or (page_number is not None and page_number != current_page):
                raise ValueError("expected one positive page number")
            page_number = current_page
            if row[0] != "5":
                continue
            confidence = float(row[10])
            value = row[11].strip()
            left, top, box_width, box_height = map(int, row[6:10])
            if not math.isfinite(confidence) or not -1 <= confidence <= 100:
                raise ValueError("invalid confidence")
            box = (left / width, top / height, box_width / width, box_height / height)
            if confidence >= 0 and value:
                result.append(OcrToken(value, "und", box, confidence / 100))
    except (csv.Error, OverflowError, ValueError):
        raise ValueError("invalid recorded OCR TSV") from None
    return tuple(result)
