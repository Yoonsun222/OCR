"""Read bounded passive SVG rectangle fixtures from a local Windows volume."""

import math
from pathlib import Path
from xml.etree import ElementTree as ET

from raglab.offline.windows_paths import WindowsLocalPaths

from .models import TargetRegion

_MAX_BYTES = 1024 * 1024
_NS = "{http://www.w3.org/2000/svg}"
_RECT_FIELDS = {"data-region-id", "data-kind", "x", "y", "width", "height"}


class _PassiveTree(ET.TreeBuilder):
    def doctype(self, name, public_id, system_id):
        raise ValueError("SVG document types are unavailable")

    def pi(self, target, text=None):
        raise ValueError("SVG processing instructions are unavailable")


def _fixture_text(path: str) -> str:
    guard = WindowsLocalPaths()
    try:
        local = guard.validate(path)
        if local.suffix.lower() != ".svg":
            raise ValueError("SVG extension required")
        with guard.pin(local, directory_write_sharing=True, reject_aliases=True) as entry:
            return entry.read_bytes(_MAX_BYTES).decode("utf-8-sig")
    except (OSError, TypeError, ValueError):
        raise ValueError("local SVG fixture is missing, unavailable or oversized") from None


def _rectangle(node: ET.Element, vw: float, vh: float) -> TargetRegion:
    try:
        x, y, w, h = (float(node.attrib[key]) for key in ("x", "y", "width", "height"))
        if not all(map(math.isfinite, (x, y, w, h))) or w <= 0 or h <= 0:
            raise ValueError("invalid rectangle")
        polygon = ((x / vw, y / vh), ((x + w) / vw, y / vh),
                   ((x + w) / vw, (y + h) / vh), (x / vw, (y + h) / vh))
        return TargetRegion(node.attrib["data-region-id"], polygon, node.get("data-kind", "unknown"))
    except (KeyError, ValueError):
        raise ValueError("invalid normalized SVG rectangle") from None


def read_svg_regions(path: str) -> tuple[TargetRegion, ...]:
    text = _fixture_text(path)
    try:
        root = ET.fromstring(text, parser=ET.XMLParser(target=_PassiveTree()))
    except ET.ParseError:
        raise ValueError("malformed SVG fixture") from None
    if root.tag != _NS + "svg" or set(root.attrib) != {"viewBox"}:
        raise ValueError("unsupported SVG root or attributes")
    try:
        ox, oy, vw, vh = map(float, root.attrib["viewBox"].split())
        if ox != 0 or oy != 0 or vw <= 0 or vh <= 0 or not all(map(math.isfinite, (vw, vh))):
            raise ValueError("invalid viewBox")
    except ValueError:
        raise ValueError("SVG viewBox is unsupported") from None

    regions: dict[str, TargetRegion] = {}
    for node in root.iter():
        if (node.text or "").strip() or (node.tail or "").strip():
            raise ValueError("unsupported SVG text")
        if node is root or (node.tag == _NS + "g" and not node.attrib):
            continue
        if node.tag != _NS + "rect" or len(node) or node.attrib.keys() - _RECT_FIELDS:
            raise ValueError("unsupported SVG element or attributes")
        if "data-region-id" in node.attrib:
            region = _rectangle(node, vw, vh)
            if region.region_id in regions:
                raise ValueError("duplicate SVG region ID")
            regions[region.region_id] = region
    return tuple(regions[key] for key in sorted(regions))
