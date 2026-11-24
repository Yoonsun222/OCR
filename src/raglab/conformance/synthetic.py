"""Exercise the common contracts using only synthetic data and local storage."""

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.contracts.evidence import (
    BBox,
    EvidenceRegion,
    EvidenceRegionV1,
    EvidenceRelation,
    EvidenceRole,
    ExtractionMethod,
    NativeLocator,
    Point,
    validate_artifact_match,
)
from raglab.deletion.registry import (
    DeletionCoordinator,
    DeletionStatus,
    Derivative,
    DerivativeKind,
    MemoryDerivativeRegistry,
    MemoryStorageBackend,
)
from raglab.indexing.generation import (
    GenerationState,
    IndexGeneration,
    IndexManifest,
    JsonGenerationCatalog,
)
from raglab.observability.events import (
    EventName,
    EventStage,
    JsonlEventWriter,
    OpaqueId,
    OpaqueIdKind,
    OpaqueIdRegistry,
    SafeEvent,
)
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore, QueryPin


# Synthetic fixture values are embedded so the installed package needs no test files.
_ARTIFACT_HASH = "a" * 64
_QUERY_STAGES = (
    EventStage.RETRIEVE,
    EventStage.SOURCE_LOAD,
    EventStage.MODEL,
    EventStage.SERIALIZE,
)


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    authorized_query: bool
    highlight_hash_verified: bool
    generation_active: bool
    revocation_blocked: bool
    deletion_verified: bool
    payload_free_log: bool

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


def _synthetic_evidence(pin: QueryPin, source_hash: str) -> EvidenceRegionV1:
    target = EvidenceRegion(
        region_id="ev_01:target",
        artifact_id="artifact_01",
        artifact_hash=_ARTIFACT_HASH,
        artifact_mime_type="image/png",
        renderer_name="synthetic",
        renderer_version="1.0",
        render_profile_hash="c" * 64,
        canvas_width_px=1000,
        canvas_height_px=800,
        dpi=144,
        page_rotation=0,
        page_box="crop",
        polygon=(Point(0.40, 0.20), Point(0.70, 0.20), Point(0.70, 0.60), Point(0.40, 0.60)),
        bbox=BBox(0.40, 0.20, 0.30, 0.40),
        transform_chain=((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),),
        role=EvidenceRole.TARGET,
    )
    label = replace(
        target,
        region_id="ev_01:label",
        polygon=(Point(0.10, 0.20), Point(0.30, 0.20), Point(0.30, 0.30), Point(0.10, 0.30)),
        bbox=BBox(0.10, 0.20, 0.20, 0.10),
        role=EvidenceRole.LABEL,
    )
    return EvidenceRegionV1(
        evidence_id="ev_01",
        source_hash=source_hash,
        document_version=pin.document_version,
        generation_id=pin.generation_id,
        native_locator=NativeLocator.page(1),
        role=EvidenceRole.TARGET,
        text="secret_01",
        regions=(label, target),
        relations=(EvidenceRelation(EvidenceRole.LABEL, label.region_id, target.region_id),),
        extraction_method=ExtractionMethod.SYNTHETIC,
        confidence=1.0,
        acl_snapshot_id=pin.query_snapshot_id,
        metadata={},
    )


def _authorization_changed(
    guard: AuthorizationGuard, pin: QueryPin, stage: EventStage, diagnostic_id: str
) -> bool:
    try:
        guard.check(pin, stage.value, diagnostic_id)
    except RaglabError as error:
        if error.code is not ErrorCode.AUTHORIZATION_CHANGED or error.stage != stage.value:
            raise
        return True
    return False


def _reserve_log_path(workspace: Path, run_id: OpaqueId) -> Path:
    path = workspace / "events.jsonl"
    try:
        path.touch(exist_ok=False)
    except FileExistsError:
        path = workspace / f"events-{run_id.value}.jsonl"
        path.touch(exist_ok=False)
    return path


def _log_matches_events(path: Path, events: tuple[SafeEvent, ...]) -> bool:
    with path.open(encoding="utf-8", newline="") as stream:
        for event in events:
            line = stream.readline()
            try:
                record = json.loads(line)
                timestamp = record.get("timestamp") if isinstance(record, dict) else None
                if not isinstance(timestamp, str):
                    return False
                parsed_timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                return False
            if parsed_timestamp.tzinfo is not UTC or timestamp != parsed_timestamp.isoformat():
                return False

            expected = {
                "event_name": event.event_name.value,
                "run_id": event.run_id.value,
                "query_id": event.query_id.value,
                "document_id": event.document_id.value,
                "generation_id": event.generation_id.value,
                "stage": event.stage.value,
                "metrics": dict(event.metrics),
                "timestamp": timestamp,
            }
            # Compare the serialized line to check types and reject duplicate fields.
            if line != json.dumps(expected, ensure_ascii=True, separators=(",", ":")) + "\n":
                return False
        return bool(events) and not stream.readline()


def run_synthetic_conformance(workspace: Path) -> ConformanceReport:
    """Run in a process-specific workspace and create a new log on each invocation."""

    principal_id = "user_01"
    group_hash = "d" * 64
    document_id = "doc_01"
    source_hashes = {document_id: "b" * 64}
    source_hash = source_hashes[document_id]
    generation_id = "gen_01"
    diagnostic_id = "diag_01"
    registry = MemoryDerivativeRegistry()
    policy = MemoryPolicyStore()

    def serving_blocked(requested_document_id: str) -> bool:
        return registry.is_tombstoned(source_hashes[requested_document_id])

    guard = AuthorizationGuard.deletion_managed(policy, serving_blocked)
    policy.allow(principal_id, document_id, group_hash=group_hash)
    pin = guard.pin(principal_id, group_hash, document_id, "doc-v1", generation_id, diagnostic_id)

    evidence = _synthetic_evidence(pin, source_hash)
    for region in evidence.regions:
        validate_artifact_match(evidence, region.region_id, _ARTIFACT_HASH, diagnostic_id)

    workspace.mkdir(parents=True, exist_ok=True)
    catalog = JsonGenerationCatalog(workspace / "generation.json")
    candidate = IndexGeneration(
        generation_id=pin.generation_id,
        document_version=pin.document_version,
        source_hash=evidence.source_hash,
        schema_version=evidence.schema_version,
        acl_projection_version=pin.policy_epoch,
        manifests=tuple(
            IndexManifest(kind, sha256(f"{kind}:{evidence.evidence_id}".encode()).hexdigest(), 1)
            for kind in ("text", "structure", "visual")
        ),
        state=GenerationState.VALIDATED,
    )
    # A retry can validate the same generation through the catalog's immutable ID check.
    expected_active_id = pin.generation_id if catalog.read_active() is not None else None
    active = catalog.promote(candidate, expected_active_id=expected_active_id)
    generation_active = active.state is GenerationState.ACTIVE and catalog.read_active() == active

    for stage in _QUERY_STAGES:
        guard.check(pin, stage.value, diagnostic_id)

    policy.revoke(document_id)
    revocation_blocked = _authorization_changed(guard, pin, EventStage.HIGHLIGHT_OPEN, diagnostic_id)

    # Establish a valid policy snapshot so the next denial proves the tombstone gate.
    policy.allow(principal_id, document_id, group_hash=group_hash)
    deletion_pin = guard.pin(
        principal_id, group_hash, document_id, pin.document_version, pin.generation_id, diagnostic_id
    )

    render_backend = MemoryStorageBackend("render")
    index_backend = MemoryStorageBackend("index")
    render = Derivative(
        "derivative_01", source_hash, pin.document_version, pin.generation_id,
        DerivativeKind.RENDER, render_backend.name, "object_01",
    )
    index_posting = Derivative(
        "derivative_02", source_hash, pin.document_version, pin.generation_id,
        DerivativeKind.INDEX_POSTING, index_backend.name, "object_02", render.derivative_id,
    )
    registry.publish(render, render_backend)
    registry.publish(index_posting, index_backend)
    receipt = DeletionCoordinator(
        registry, {render_backend.name: render_backend, index_backend.name: index_backend}
    ).delete_document(source_hash)
    tombstone_blocked = _authorization_changed(
        guard, deletion_pin, EventStage.RETRIEVE, diagnostic_id
    )
    deletion_verified = (
        receipt.status is DeletionStatus.DELETED_VERIFIED
        and receipt.deleted_count == 2
        and tombstone_blocked
    )

    identifiers = OpaqueIdRegistry()
    run_id = identifiers.map(OpaqueIdKind.RUN, "run_01")
    query_id = identifiers.map(OpaqueIdKind.QUERY, pin.query_snapshot_id)
    log_document_id = identifiers.map(OpaqueIdKind.DOCUMENT, pin.document_id)
    log_generation_id = identifiers.map(OpaqueIdKind.GENERATION, active.generation_id)
    writer = JsonlEventWriter(_reserve_log_path(workspace, run_id))
    stage_metrics = (
        (EventStage.RETRIEVE, {"candidate_count": 1}),
        (EventStage.SOURCE_LOAD, {"denied_count": 0}),
        (EventStage.MODEL, {"denied_count": 0}),
        (EventStage.SERIALIZE, {"denied_count": 0}),
        (EventStage.HIGHLIGHT_OPEN, {"denied_count": int(revocation_blocked)}),
        (EventStage.DELETE, {"deleted_count": receipt.deleted_count}),
    )
    events = tuple(
        SafeEvent(
            EventName.STAGE_FINISHED, run_id, query_id, log_document_id, log_generation_id,
            stage, metrics,
        )
        for stage, metrics in stage_metrics
    )
    for event in events:
        writer.write(event)

    return ConformanceReport(
        authorized_query=pin.policy_epoch == 1,
        highlight_hash_verified=True,
        generation_active=generation_active,
        revocation_blocked=revocation_blocked,
        deletion_verified=deletion_verified,
        payload_free_log=_log_matches_events(writer.path, events),
    )
