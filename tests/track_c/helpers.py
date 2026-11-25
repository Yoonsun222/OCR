from raglab.contracts.evidence import EvidenceRegionV1, EvidenceRole, ExtractionMethod, NativeLocator
from raglab.indexing.generation import GenerationState, IndexGeneration, IndexManifest
from raglab.security.authorization import QueryPin
from raglab.track_c.models import (
    Channel,
    DocumentTarget,
    FusionResult,
    MixedQuery,
    PinnedMixedQuery,
    RetrievalCandidate,
)


def evidence(
    evidence_id: str = "ev-1",
    document_version: str = "v1",
    generation_id: str = "gen-1",
    text: str = "12.5",
    metadata: dict[str, object] | None = None,
) -> EvidenceRegionV1:
    return EvidenceRegionV1(
        evidence_id=evidence_id,
        source_hash="a" * 64,
        document_version=document_version,
        generation_id=generation_id,
        native_locator=NativeLocator.sheet_range("sheet-1", "B7"),
        role=EvidenceRole.VALUE,
        text=text,
        regions=(),
        relations=(),
        extraction_method=ExtractionMethod.NATIVE,
        confidence=1.0,
        acl_snapshot_id="acl-1",
        metadata={"label": "Pressure", "unit": "MPa"} if metadata is None else metadata,
    )


def generation(generation_id: str = "gen-1") -> IndexGeneration:
    manifests = (
        IndexManifest("text", "a" * 64, 1),
        IndexManifest("structure", "b" * 64, 1),
        IndexManifest("visual", "c" * 64, 1),
    )
    return IndexGeneration(
        generation_id, "catalog-v1", "b" * 64, "evidence-region.v1", 1,
        manifests, GenerationState.ACTIVE,
    )


def pinned_query() -> PinnedMixedQuery:
    request = MixedQuery("pressure", "user-1", "g" * 64, (DocumentTarget("doc-1", "v1"),))
    pin = QueryPin(
        principal_id="user-1",
        group_hash="g" * 64,
        policy_epoch=1,
        query_snapshot_id="00000000-0000-4000-8000-000000000001",
        document_id="doc-1",
        document_version="v1",
        generation_id="gen-1",
    )
    return PinnedMixedQuery(request, generation(), (pin,))


def candidate(
    evidence_id: str = "ev-1",
    document_id: str = "doc-1",
    score: float = 1.0,
    rank: int = 1,
    document_version: str = "v1",
    key: str | None = "Pressure",
    value: str | None = "12.5",
    channel: Channel = "structure",
) -> RetrievalCandidate:
    return RetrievalCandidate(
        evidence(evidence_id, document_version), document_id, channel, score, rank, key, value, "MPa",
    )


def fused() -> FusionResult:
    return FusionResult((candidate(),), False)
