"""Convert ranked Track A candidates to common rendered evidence records."""

import hashlib
import json

from raglab.contracts.evidence import (
    BBox,
    EvidenceRegion,
    EvidenceRegionV1,
    EvidenceRelation,
    EvidenceRole,
    ExtractionMethod,
    Point,
)

from .models import ArtifactProvenance, GroundingCandidate
from .normalization import normalize_label


def _bbox(points: tuple[tuple[float, float], ...]) -> BBox:
    xs, ys = zip(*points)
    return BBox(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _identity(candidate: GroundingCandidate, provenance: ArtifactProvenance) -> str:
    """Hash structured location identity, including the OCR occurrence geometry."""
    payload = {
        "source_hash": provenance.source_hash,
        "document_version": provenance.document_version,
        "generation_id": provenance.generation_id,
        "artifact_id": provenance.artifact_id,
        "artifact_hash": provenance.artifact_hash,
        "native_locator": provenance.native_locator.to_dict(),
        "label": normalize_label(candidate.token.text),
        "language": candidate.token.language,
        "ocr_box": list(candidate.token.box),
        "target_id": candidate.target.region_id,
        "target_kind": candidate.target.kind,
        "target_polygon": [list(point) for point in candidate.target.polygon],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_to_evidence(
    candidate: GroundingCandidate, provenance: ArtifactProvenance
) -> EvidenceRegionV1:
    """Create paired label/target regions and a validated relation between them."""
    try:
        evidence_id = "ev_" + _identity(candidate, provenance)[:24]
        label_id, target_id = f"{evidence_id}:label", f"{evidence_id}:target"
        x, y, width, height = candidate.token.box
        label_polygon = (
            Point(x, y),
            Point(x + width, y),
            Point(x + width, y + height),
            Point(x, y + height),
        )
        target_polygon = tuple(Point(px, py) for px, py in candidate.target.polygon)
        common = {
            "artifact_id": provenance.artifact_id,
            "artifact_hash": provenance.artifact_hash,
            "artifact_mime_type": provenance.artifact_mime_type,
            "renderer_name": provenance.renderer_name,
            "renderer_version": provenance.renderer_version,
            "render_profile_hash": provenance.render_profile_hash,
            "canvas_width_px": provenance.canvas_width_px,
            "canvas_height_px": provenance.canvas_height_px,
            "dpi": provenance.dpi,
            "page_rotation": provenance.page_rotation,
            "page_box": provenance.page_box,
            "transform_chain": provenance.transform_chain,
        }
        regions = (
            EvidenceRegion(
                region_id=label_id,
                polygon=label_polygon,
                bbox=BBox(x, y, width, height),
                role=EvidenceRole.LABEL,
                **common,
            ),
            EvidenceRegion(
                region_id=target_id,
                polygon=target_polygon,
                bbox=_bbox(candidate.target.polygon),
                role=EvidenceRole.TARGET,
                **common,
            ),
        )
        return EvidenceRegionV1(
            evidence_id=evidence_id,
            source_hash=provenance.source_hash,
            document_version=provenance.document_version,
            generation_id=provenance.generation_id,
            native_locator=provenance.native_locator,
            role=EvidenceRole.TARGET,
            text=candidate.token.text,
            regions=regions,
            relations=(EvidenceRelation(EvidenceRole.LABEL, label_id, target_id),),
            extraction_method=ExtractionMethod.OCR,
            confidence=candidate.score,
            acl_snapshot_id=provenance.acl_snapshot_id,
            metadata={"target_region_id": candidate.target.region_id, "language": candidate.token.language},
        )
    except Exception:
        # Input-derived text, labels, and identifiers must not escape via exception messages.
        raise ValueError("candidate evidence conversion failed") from None
