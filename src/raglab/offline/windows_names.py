from bisect import bisect_left
from collections.abc import Iterable
from ctypes import wintypes as win
from functools import cache, cmp_to_key

from raglab.offline.windows_paths import bind, system_dll


@cache
def _ordinal_api():
    return bind(
        system_dll("kernel32.dll"), "CompareStringOrdinal", win.INT,
        win.LPCWSTR, win.INT, win.LPCWSTR, win.INT, win.BOOL,
    )


def windows_name_compare(left: str, right: str) -> int:
    comparison = _ordinal_api()(left, -1, right, -1, True)
    if comparison not in {1, 2, 3}:
        raise ValueError("Windows ordinal filename comparison failed")
    return comparison - 2


_FILENAME_ORDER = cmp_to_key(windows_name_compare)


class WindowsFilenameIndex:
    """Match Windows ordinal names while preserving each name's original spelling."""

    def __init__(self, names: Iterable[str]):
        self._keys = sorted(map(_FILENAME_ORDER, names))
        if any(left == right for left, right in zip(self._keys, self._keys[1:])):
            raise ValueError("duplicate Windows ordinal filename")

    def find(self, name: str) -> str | None:
        key = _FILENAME_ORDER(name)
        index = bisect_left(self._keys, key)
        if index < len(self._keys) and self._keys[index] == key:
            return self._keys[index].obj
        return None
