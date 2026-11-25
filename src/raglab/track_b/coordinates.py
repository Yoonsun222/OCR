"""Excel worksheet coordinates shared by the native and CSV readers."""

from collections.abc import Iterable, Iterator
import re
from xml.etree.ElementTree import Element

from .errors import InvalidPackage

MAX_ROW = 1_048_576
MAX_COLUMN = 16_384
XML_WHITESPACE = " \t\r\n"
CELL_REFERENCE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")


def positive_index(raw: str, maximum: int, error_message: str) -> int:
    normalized = raw.strip(XML_WHITESPACE).removeprefix("+").lstrip("0")
    if not normalized.isascii() or not normalized.isdecimal() or len(normalized) > len(str(maximum)):
        raise InvalidPackage(error_message)
    value = int(normalized)
    if value > maximum:
        raise InvalidPackage(error_message)
    return value


def column_name(column: int) -> str:
    letters = ""
    while column:
        column, offset = divmod(column - 1, 26)
        letters = chr(ord("A") + offset) + letters
    return letters


def cell_coordinates(reference: str) -> tuple[int, int]:
    match = CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise InvalidPackage("invalid cell reference")
    letters, raw_row = match.groups()
    column = 0
    for letter in letters:
        column = column * 26 + ord(letter) - ord("A") + 1
    row = int(raw_row)
    if column > MAX_COLUMN or row > MAX_ROW:
        raise InvalidPackage("cell reference exceeds worksheet bounds")
    return row, column


def range_coordinates(reference: str) -> tuple[int, int, int, int]:
    start, separator, end = reference.partition(":")
    top, left = cell_coordinates(start)
    bottom, right = cell_coordinates(end) if separator else (top, left)
    if bottom < top or right < left:
        raise InvalidPackage("invalid worksheet range order")
    return top, left, bottom, right


def numbered_rows(rows: Iterable[Element]) -> Iterator[tuple[int, Element]]:
    row = 0
    for element in rows:
        explicit = element.get("r")
        next_row = row + 1 if explicit is None else positive_index(explicit, MAX_ROW, "invalid worksheet row")
        if next_row < row or next_row > MAX_ROW:
            raise InvalidPackage("invalid worksheet row order or bounds")
        row = next_row
        yield row, element
