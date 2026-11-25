"""Immutable inputs and outputs for explicit-label grounding."""

import math
import re
from dataclasses import dataclass
from typing import Literal

from raglab.contracts.evidence import BBox, EvidenceRegionV1, NativeLocator
from raglab.security.authorization import QueryPin

Box = tuple[float, float, float, float]
Point = tuple[float, float]
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _finite(value: object) -> bool:
    return type(value) is int or type(value) is float and math.isfinite(value)


def _unit(value: object) -> bool:
    return _finite(value) and 0 <= value <= 1


def _require_text(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("required text must be nonempty")


def validate_box(box: Box) -> None:
    try:
        BBox(*box)
    except (TypeError, ValueError):
        raise ValueError("normalized box must have positive area inside [0,1]") from None


@dataclass(frozen=True, slots=True)
class OcrToken:
    text: str
    language: str
    box: Box
    confidence: float

    def __post_init__(self) -> None:
        try:
            box = tuple(self.box)
        except TypeError:
            raise ValueError("normalized box must have four coordinates") from None
        validate_box(box)
        _require_text(self.text, self.language)
        if not _unit(self.confidence):
            raise ValueError("OCR confidence must be finite and between 0 and 1")
        object.__setattr__(self, "box", box)


@dataclass(frozen=True, slots=True)
class TargetRegion:
    region_id: str
    polygon: tuple[Point, ...]
    kind: str

    def __post_init__(self) -> None:
        _require_text(self.region_id, self.kind)
        error = "target polygon must have positive area inside [0,1]"
        try:
            polygon = tuple(tuple(point) for point in self.polygon)
        except TypeError:
            raise ValueError(error) from None
        if len(polygon) < 3 or any(
            len(point) != 2 or not all(_unit(value) for value in point) for point in polygon
        ):
            raise ValueError(error)
        area = math.fsum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1])
        )
        if not area:
            raise ValueError(error)
        object.__setattr__(self, "polygon", polygon)


@dataclass(frozen=True, slots=True)
class PartQuery:
    raw: str
    label: str

    def __post_init__(self) -> None:
        _require_text(self.raw, self.label)


@dataclass(frozen=True, slots=True)
class GroundingCandidate:
    token: OcrToken
    target: TargetRegion
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.token, OcrToken) or not isinstance(self.target, TargetRegion):
            raise ValueError("candidate requires an OCR token and a target region")
        if not _unit(self.score):
            raise ValueError("candidate score must be finite and between 0 and 1")


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    source_hash: str
    document_version: str
    generation_id: str
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
    transform_chain: tuple[tuple[float, ...], ...]
    native_locator: NativeLocator
    acl_snapshot_id: str

    def __post_init__(self) -> None:
        _require_text(
            self.document_version, self.generation_id, self.artifact_id,
            self.artifact_mime_type, self.renderer_name, self.renderer_version,
            self.page_box, self.acl_snapshot_id,
        )
        if any(
            not isinstance(value, str) or not _SHA256.fullmatch(value)
            for value in (self.source_hash, self.artifact_hash, self.render_profile_hash)
        ):
            raise ValueError("provenance hashes must be lowercase SHA-256 digests")
        if any(
            type(size) is not int or size <= 0
            for size in (self.canvas_width_px, self.canvas_height_px)
        ):
            raise ValueError("canvas dimensions must be positive integers")
        if not _finite(self.dpi) or self.dpi <= 0:
            raise ValueError("DPI must be finite and positive")
        if type(self.page_rotation) is not int or self.page_rotation not in (0, 90, 180, 270):
            raise ValueError("page rotation must be 0, 90, 180 or 270 degrees")
        if not isinstance(self.native_locator, NativeLocator):
            raise ValueError("provenance requires a native locator")
        error = "transform chain must contain finite nine-value matrices"
        try:
            chain = tuple(tuple(matrix) for matrix in self.transform_chain)
        except TypeError:
            raise ValueError(error) from None
        if not chain or any(len(matrix) != 9 or not all(map(_finite, matrix)) for matrix in chain):
            raise ValueError(error)
        object.__setattr__(self, "transform_chain", chain)


@dataclass(frozen=True, slots=True)
class GroundingRequest:
    question: str
    artifact_path: str
    provenance: ArtifactProvenance
    pin: QueryPin

    def __post_init__(self) -> None:
        _require_text(self.question, self.artifact_path)
        if not isinstance(self.provenance, ArtifactProvenance) or not isinstance(self.pin, QueryPin):
            raise ValueError("grounding request requires provenance and a query pin")


@dataclass(frozen=True, slots=True)
class GroundingResponse:
    state: Literal["VERIFIED", "PART_ABSENT", "ABSTAIN"]
    evidence: tuple[EvidenceRegionV1, ...]
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.reason)
        if self.state not in ("VERIFIED", "PART_ABSENT", "ABSTAIN"):
            raise ValueError("unsupported grounding response state")
        try:
            evidence = tuple(self.evidence)
        except TypeError:
            raise ValueError("expected evidence records") from None
        if any(not isinstance(item, EvidenceRegionV1) for item in evidence):
            raise ValueError("expected evidence records")
        if self.state == "VERIFIED":
            if not 1 <= len(evidence) <= 3:
                raise ValueError("verified responses require one to three evidence records")
        elif evidence:
            raise ValueError("absent and abstaining responses cannot include evidence")
        object.__setattr__(self, "evidence", evidence)
