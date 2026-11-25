"""Authorize deterministic grounding over an approved immutable artifact."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice
from raglab.security.authorization import AuthorizationGuard, QueryPin

from .evidence import candidate_to_evidence
from .matcher import matching_tokens, parse_part_query
from .models import ArtifactProvenance, GroundingRequest, GroundingResponse, OcrToken, TargetRegion
from .ranker import rank_candidates

_MINIMUM_MARGIN = .05


@dataclass(frozen=True, slots=True)
class ApprovedArtifact:
    document_id: str
    artifact_path: str
    provenance: ArtifactProvenance
    data: bytes


class ArtifactResolver(Protocol):
    """Resolve from trusted storage using the pin and artifact ID as lookup keys.

    Supply canonical, query-bound provenance with the stored artifact bytes.
    The resolver is injected by the application and owns the document/version/
    generation binding. The request path is never passed to this resolver.
    """

    def __call__(self, pin: QueryPin, artifact_id: str, /) -> ApprovedArtifact | None: ...


class ArtifactOcrProvider(Protocol):
    def read(self, data: bytes, /) -> tuple[OcrToken, ...]: ...


class ArtifactGeometryProvider(Protocol):
    def read(self, data: bytes, /) -> tuple[TargetRegion, ...]: ...


class GroundingService:
    def __init__(
        self,
        guard: AuthorizationGuard,
        ocr: ArtifactOcrProvider,
        geometry: ArtifactGeometryProvider,
        minimum_score: float = .55,
        *,
        resolver: ArtifactResolver,
    ) -> None:
        if type(minimum_score) not in (int, float) or not 0 <= minimum_score <= 1:
            raise ValueError("minimum score must be between zero and one")
        self._guard = guard
        self._ocr = ocr
        self._geometry = geometry
        self._minimum_score = minimum_score
        self._resolver = resolver

    def query(self, request: GroundingRequest) -> GroundingResponse:
        pin = request.pin
        self._guard.check(pin, "retrieve", diagnostic_id="track-a-retrieve")
        query = parse_part_query(request.question)
        artifact = self._resolve(request)

        self._guard.check(pin, "source_load", diagnostic_id="track-a-ocr")
        tokens = matching_tokens(query, self._ocr.read(artifact.data))
        if not tokens:
            return self._release(pin, GroundingResponse("PART_ABSENT", (), "no exact visible label matched"))
        if len({token.box for token in tokens}) > 1:
            return self._release(pin, GroundingResponse("ABSTAIN", (), "multiple visible label occurrences"))

        token = min(tokens, key=lambda item: (-item.confidence, item.text, item.language))
        self._guard.check(pin, "source_load", diagnostic_id="track-a-geometry")
        regions = tuple(dict.fromkeys(self._geometry.read(artifact.data)))
        ranked = rank_candidates((token,), regions, 2)
        # The ranker can award .65 for OCR alone. Require a spatial contribution.
        spatial_baseline = .65 * token.confidence
        if not ranked or ranked[0].score < self._minimum_score or ranked[0].score <= spatial_baseline:
            return self._release(pin, GroundingResponse("ABSTAIN", (), "no target met spatial threshold"))
        # Close alternatives are ambiguous, even when only the leader meets the total score threshold.
        if len(ranked) == 2 and ranked[1].score > spatial_baseline and (
            ranked[1].score + _MINIMUM_MARGIN >= ranked[0].score
        ):
            return self._release(pin, GroundingResponse("ABSTAIN", (), "competing targets have similar scores"))

        self._guard.check(pin, "model", diagnostic_id="track-a-evidence")
        item = candidate_to_evidence(ranked[0], artifact.provenance)
        self._guard.check(pin, "highlight_open", diagnostic_id=item.evidence_id)
        return self._release(pin, GroundingResponse("VERIFIED", (item,), "explicit label and target matched"))

    def _resolve(self, request: GroundingRequest) -> ApprovedArtifact:
        pin, provenance = request.pin, request.provenance
        if (
            provenance.document_version != pin.document_version
            or provenance.generation_id != pin.generation_id
            or provenance.acl_snapshot_id != pin.query_snapshot_id
        ):
            raise RaglabError.policy_denied("retrieve", "track-a-artifact-binding")

        self._guard.check(pin, "source_load", diagnostic_id="track-a-resolve")
        try:
            approved = self._resolver(pin, provenance.artifact_id)
        except Exception:
            raise RaglabError(
                ErrorCode.POLICY_UNAVAILABLE, "source_load", "artifact binding unavailable",
                "approved artifact binding", "no source or result released", RetryAdvice.SAFE,
                "retry when the artifact resolver is available", "track-a-resolve",
            ) from None
        if (
            type(approved) is not ApprovedArtifact
            or approved.document_id != pin.document_id
            or approved.artifact_path != request.artifact_path
            or approved.provenance != provenance
        ):
            raise RaglabError.policy_denied("source_load", "track-a-artifact-binding")
        if type(approved.data) is not bytes or sha256(approved.data).hexdigest() != provenance.artifact_hash:
            raise RaglabError(
                ErrorCode.ARTIFACT_HASH_MISMATCH, "source_load", "artifact bytes failed verification",
                "immutable bytes matching approved artifact hash", "no source or result released",
                RetryAdvice.NEVER, "verify the approved artifact", "track-a-artifact-integrity",
            )
        return approved

    def _release(self, pin: QueryPin, response: GroundingResponse) -> GroundingResponse:
        self._guard.check(pin, "serialize", diagnostic_id="track-a-serialize")
        return response
