"""Immutable evidence records tied to native locations and render artifacts."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from .errors import ErrorCode, RaglabError, RetryAdvice


_SHA256 = re.compile(r"[0-9a-f]{64}")
_CELL = r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]*"
_SHEET = r"(?:'(?:[^']|'')+'|[\w.]+)!"
_A1_REFERENCE = rf"(?:{_SHEET})?{_CELL}(?::{_CELL})?"
_NATIVE_RANGE = re.compile(_A1_REFERENCE)
_REFERENCE_ARGUMENTS = rf"{_A1_REFERENCE}(?:[ \t]*,[ \t]*{_A1_REFERENCE})*"
_NATIVE_FORMULA = re.compile(
    rf"=(?:{_A1_REFERENCE}|[A-Za-z_][A-Za-z0-9_.]*\([ \t]*{_REFERENCE_ARGUMENTS}[ \t]*\))"
)


def _is_finite_number(value: object) -> bool:
    return type(value) is int or isinstance(value, float) and math.isfinite(value)


def _is_normalized(value: object) -> bool:
    return _is_finite_number(value) and 0.0 <= value <= 1.0


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _require_sha256(name: str, value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 64-character lowercase hexadecimal hash")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("metadata object keys must be strings")
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, str):
        return str(value)
    if value is None or isinstance(value, bool) or _is_finite_number(value):
        return value
    raise ValueError("metadata must contain only JSON values with finite numbers")


def _copy_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy_json(item) for item in value]
    return value


class EvidenceRole(StrEnum):
    LABEL = "label"
    TARGET = "target"
    LEGEND = "legend"
    SERIES = "series"
    AXIS = "axis"
    VALUE = "value"
    CONTEXT = "context"


class ExtractionMethod(StrEnum):
    NATIVE = "native"
    OCR = "ocr"
    SYNTHETIC = "synthetic"
    VISUAL_MODEL = "visual_model"


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __post_init__(self) -> None:
        if not _is_normalized(self.x) or not _is_normalized(self.y):
            raise ValueError("point coordinates must be finite and normalized")

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True, slots=True)
class BBox:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        Point(self.x, self.y)
        if not all(
            _is_normalized(size) and size > 0 for size in (self.width, self.height)
        ):
            raise ValueError("bounding box width and height must be positive")
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            raise ValueError("bounding box must fit within the normalized canvas")

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


_LOCATOR_FIELDS = {
    "page": ("page_number",),
    "slide": ("slide_number",),
    "sheet_range": ("sheet_id", "cell_range"),
    "sheet_object": ("sheet_id", "object_id"),
    "object": ("object_id",),
}
_LOCATOR_FIELD_NAMES = frozenset(name for names in _LOCATOR_FIELDS.values() for name in names)
_LOCATOR_FACTORY = object()


@dataclass(frozen=True, slots=True)
class NativeLocator:
    kind: str
    _factory: InitVar[object] = field(default=None, kw_only=True)
    page_number: int | None = field(default=None, kw_only=True)
    slide_number: int | None = field(default=None, kw_only=True)
    sheet_id: str | None = field(default=None, kw_only=True)
    cell_range: str | None = field(default=None, kw_only=True)
    object_id: str | None = field(default=None, kw_only=True)

    def __post_init__(self, _factory: object) -> None:
        if _factory is not _LOCATOR_FACTORY:
            raise TypeError("use a NativeLocator factory")
        names = _LOCATOR_FIELDS.get(self.kind)
        if names is None:
            raise ValueError("invalid native locator kind")
        present = {name for name in _LOCATOR_FIELD_NAMES if getattr(self, name) is not None}
        if present != set(names):
            raise ValueError("native locator fields must match its kind")
        for name in names:
            value = getattr(self, name)
            if name in ("page_number", "slide_number"):
                if type(value) is not int or value < 1:
                    raise ValueError(f"{name} must be a positive integer")
            else:
                _require_text(name, value)

    @classmethod
    def page(cls, page_number: int) -> NativeLocator:
        return cls("page", page_number=page_number, _factory=_LOCATOR_FACTORY)

    @classmethod
    def slide(cls, slide_number: int) -> NativeLocator:
        return cls("slide", slide_number=slide_number, _factory=_LOCATOR_FACTORY)

    @classmethod
    def sheet_range(cls, sheet_id: str, cell_range: str) -> NativeLocator:
        return cls("sheet_range", sheet_id=sheet_id, cell_range=cell_range, _factory=_LOCATOR_FACTORY)

    @classmethod
    def sheet_object(cls, sheet_id: str, object_id: str) -> NativeLocator:
        return cls("sheet_object", sheet_id=sheet_id, object_id=object_id, _factory=_LOCATOR_FACTORY)

    @classmethod
    def object(cls, object_id: str) -> NativeLocator:
        return cls("object", object_id=object_id, _factory=_LOCATOR_FACTORY)

    def to_dict(self) -> dict[str, str | int]:
        locator_fields = {name: getattr(self, name) for name in _LOCATOR_FIELDS[self.kind]}
        return {"kind": self.kind, **locator_fields}


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    """A single target or a link from one rendered region to another target.

    With a native target, the third field holds an optional observation.
    """

    role: EvidenceRole
    target: str
    related: str | None = None
    _points_to_region: bool | None = field(default=None, kw_only=True, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.role, EvidenceRole):
            raise ValueError("relation role must be an EvidenceRole")
        _require_text("relation target", self.target)
        if self.related is not None and not isinstance(self.related, str):
            raise ValueError("related relation value must be a string")

    @property
    def target_region_id(self) -> str | None:
        # The envelope can confirm that a region ID resembling a cell is a real region.
        points_to_region = self._points_to_region
        if points_to_region is None:
            points_to_region = not _is_native_target(self.target)
        if not points_to_region:
            return None
        return self.target if self.related is None else self.related

    def to_dict(self) -> dict[str, str | None]:
        return {"role": self.role.value, "target": self.target, "related": self.related}


def _is_native_target(target: str) -> bool:
    # Accept direct A1 references and formulas consisting of a reference
    # or a function call with one or more reference arguments.
    return (
        _NATIVE_RANGE.fullmatch(target) is not None
        or _NATIVE_FORMULA.fullmatch(target) is not None
    )


@dataclass(frozen=True, slots=True)
class EvidenceRegion:
    region_id: str
    artifact_id: str
    artifact_hash: str
    artifact_mime_type: str
    renderer_name: str
    renderer_version: str
    render_profile_hash: str
    canvas_width_px: int
    canvas_height_px: int
    dpi: float
    page_rotation: int
    page_box: str
    polygon: tuple[Point, ...]
    bbox: BBox
    transform_chain: tuple[tuple[float, ...], ...]
    role: EvidenceRole
    coordinate_space: str = field(init=False, default="rendered_top_left_normalized")

    def __post_init__(self) -> None:
        for name in (
            "region_id", "artifact_id", "artifact_mime_type", "renderer_name",
            "renderer_version", "page_box",
        ):
            _require_text(name, getattr(self, name))
        _require_sha256("artifact_hash", self.artifact_hash)
        _require_sha256("render_profile_hash", self.render_profile_hash)
        if any(
            type(size) is not int or size < 1
            for size in (self.canvas_width_px, self.canvas_height_px)
        ):
            raise ValueError("canvas dimensions must be positive integers")
        if not _is_finite_number(self.dpi) or self.dpi <= 0:
            raise ValueError("DPI must be finite and positive")
        if type(self.page_rotation) is not int or self.page_rotation not in (0, 90, 180, 270):
            raise ValueError("page rotation must be 0, 90, 180 or 270 degrees")

        polygon = tuple(self.polygon)
        if len(polygon) < 3 or any(not isinstance(point, Point) for point in polygon):
            raise ValueError("polygon must have at least three normalized points")
        if not isinstance(self.bbox, BBox):
            raise ValueError("bbox must be a BBox")
        transform_chain = tuple(tuple(matrix) for matrix in self.transform_chain)
        if not transform_chain or any(
            len(matrix) != 9 or not all(_is_finite_number(value) for value in matrix)
            for matrix in transform_chain
        ):
            raise ValueError("transform chain must contain finite nine-value matrices")
        if not isinstance(self.role, EvidenceRole):
            raise ValueError("region role must be an EvidenceRole")
        object.__setattr__(self, "polygon", polygon)
        object.__setattr__(self, "transform_chain", transform_chain)

    def to_dict(self) -> dict[str, object]:
        return {
            "region_id": self.region_id,
            "artifact_id": self.artifact_id,
            "artifact_hash": self.artifact_hash,
            "artifact_mime_type": self.artifact_mime_type,
            "renderer_name": self.renderer_name,
            "renderer_version": self.renderer_version,
            "render_profile_hash": self.render_profile_hash,
            "canvas_width_px": self.canvas_width_px,
            "canvas_height_px": self.canvas_height_px,
            "dpi": self.dpi,
            "page_rotation": self.page_rotation,
            "page_box": self.page_box,
            "coordinate_space": self.coordinate_space,
            "polygon": [point.to_dict() for point in self.polygon],
            "bbox": self.bbox.to_dict(),
            "transform_chain": [list(matrix) for matrix in self.transform_chain],
            "role": self.role.value,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRegionV1:
    evidence_id: str
    source_hash: str
    document_version: str
    generation_id: str
    native_locator: NativeLocator
    role: EvidenceRole
    text: str | None
    regions: tuple[EvidenceRegion, ...]
    relations: tuple[EvidenceRelation, ...]
    extraction_method: ExtractionMethod
    confidence: float
    acl_snapshot_id: str
    metadata: Mapping[str, object]
    schema_version: str = field(init=False, default="evidence-region.v1")

    def __post_init__(self) -> None:
        for name in ("evidence_id", "source_hash", "document_version", "generation_id", "acl_snapshot_id"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.native_locator, NativeLocator):
            raise ValueError("native_locator must be a NativeLocator")
        if not isinstance(self.role, EvidenceRole):
            raise ValueError("evidence role must be an EvidenceRole")
        if self.text is not None and not isinstance(self.text, str):
            raise ValueError("evidence text must be a string or None")
        if not isinstance(self.extraction_method, ExtractionMethod):
            raise ValueError("extraction_method must be an ExtractionMethod")
        if not _is_normalized(self.confidence):
            raise ValueError("confidence must be finite and between 0 and 1")

        regions = tuple(self.regions)
        if not regions and self.extraction_method is not ExtractionMethod.NATIVE:
            raise ValueError("non-native evidence requires at least one rendered region")
        if any(not isinstance(region, EvidenceRegion) for region in regions):
            raise ValueError("regions must contain EvidenceRegion records")
        region_ids = {region.region_id for region in regions}
        if len(region_ids) != len(regions):
            raise ValueError("region IDs must be unique within an evidence record")

        relations = []
        for relation in self.relations:
            if not isinstance(relation, EvidenceRelation):
                raise ValueError("relations must contain EvidenceRelation records")
            points_to_region = relation.target in region_ids
            if points_to_region:
                if relation.related is not None and relation.related not in region_ids:
                    raise ValueError("rendered relation destination must be an existing region")
            elif self.extraction_method is not ExtractionMethod.NATIVE or not _is_native_target(
                relation.target
            ):
                raise ValueError("relation must target an existing region or a supported native reference")
            if points_to_region != (relation.target_region_id is not None):
                relation = replace(relation, _points_to_region=points_to_region)
            relations.append(relation)

        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a JSON object")
        object.__setattr__(self, "regions", regions)
        object.__setattr__(self, "relations", tuple(relations))
        object.__setattr__(self, "metadata", _freeze_json(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "source_hash": self.source_hash,
            "document_version": self.document_version,
            "generation_id": self.generation_id,
            "native_locator": self.native_locator.to_dict(),
            "role": self.role.value,
            "text": self.text,
            "regions": [region.to_dict() for region in self.regions],
            "relations": [relation.to_dict() for relation in self.relations],
            "extraction_method": self.extraction_method.value,
            "confidence": self.confidence,
            "acl_snapshot_id": self.acl_snapshot_id,
            "metadata": _copy_json(self.metadata),
            "schema_version": self.schema_version,
        }


def validate_artifact_match(
    evidence: EvidenceRegionV1, region_id: str, actual_hash: str, diagnostic_id: str
) -> None:
    region = next((item for item in evidence.regions if item.region_id == region_id), None)
    if region is None or region.artifact_hash != actual_hash:
        raise RaglabError(
            ErrorCode.ARTIFACT_HASH_MISMATCH, "highlight_open", actual_hash,
            region.artifact_hash if region else "missing region", "highlight blocked", RetryAdvice.NEVER,
            "open the render artifact recorded by this evidence", diagnostic_id,
        )
