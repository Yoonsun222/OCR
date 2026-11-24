# Common security contracts

This reference records the implemented public contracts used across the RAG lab. The common evidence and error surface is `raglab.contracts`; authorization, generation, deletion, and observability remain separately imported from `raglab.security`, `raglab.indexing.generation`, `raglab.deletion.registry`, and `raglab.observability`.

## Stable import surfaces

`raglab.contracts.__all__` is exactly: `BBox`, `ErrorCode`, `EvidenceRegion`, `EvidenceRegionV1`, `EvidenceRelation`, `EvidenceRole`, `ExtractionMethod`, `NativeLocator`, `Point`, `RaglabError`, `RetryAdvice`, and `validate_artifact_match`. The latter three evidence enums and record types are part of the reviewed API and must not be dropped to follow an outdated narrower list.

The other reviewed public APIs are:

- `raglab.security`: `AuthorizationGuard`, `CacheKey`, `MemoryPolicyStore`, `PolicySnapshot`, `QueryPin`.
- `raglab.indexing.generation`: `GenerationState`, `IndexManifest`, `IndexGeneration`, `JsonGenerationCatalog`.
- `raglab.deletion.registry`: `DerivativeKind`, `DeletionStatus`, `Derivative`, `BackendDeletionReceipt`, `DeletionReceipt`, `MemoryDerivativeRegistry`, `MemoryStorageBackend`, `DeletionCoordinator`.
- `raglab.observability`: `EventName`, `EventStage`, `JsonlEventWriter`, `OpaqueId`, `OpaqueIdKind`, `OpaqueIdRegistry`, `SafeEvent`.

## EvidenceRegionV1

The frozen envelope has schema version `evidence-region.v1` and fields `evidence_id`, `source_hash`, `document_version`, `generation_id`, `native_locator`, `role`, `text`, `regions`, `relations`, `extraction_method`, `confidence`, `acl_snapshot_id`, and JSON-only `metadata`, plus its fixed `schema_version`. Identifiers and hashes are nonempty; hashes belonging to source/render artifacts are 64 lowercase hexadecimal SHA-256 strings. Confidence is finite and in `[0,1]`. Native extraction can have no rendered region; OCR, synthetic, and visual-model evidence requires at least one.

`EvidenceRole` values are `label`, `target`, `legend`, `series`, `axis`, `value`, and `context`. `ExtractionMethod` values are `native`, `ocr`, `synthetic`, and `visual_model`. `EvidenceRelation` ties a typed role to an existing rendered region or, for native evidence, a supported cell/range reference or formula. `to_dict()` returns JSON primitives, with metadata recursively frozen in memory and copied to JSON values on serialization.

`EvidenceRegion` carries `region_id`, `artifact_id`, `artifact_hash`, `artifact_mime_type`, `renderer_name`, `renderer_version`, `render_profile_hash`, `canvas_width_px`, `canvas_height_px`, `dpi`, `page_rotation`, `page_box`, `polygon`, `bbox`, `transform_chain`, `role`, and fixed `coordinate_space`. Coordinates use `rendered_top_left_normalized`: finite x/y values from 0 through 1, origin at the rendered canvas's top left, x increasing right and y increasing down. Bounding boxes have positive width/height and remain within the canvas. Polygons have at least three normalized points. Canvas dimensions and DPI are positive; rotation is 0, 90, 180, or 270 degrees. Every transform matrix has nine finite values, and the chain is nonempty. Artifact and render-profile hashes are exact SHA-256 values. `validate_artifact_match` blocks highlight opening with `ARTIFACT_HASH_MISMATCH` if the region is absent or the supplied artifact hash differs.

`NativeLocator` is tagged and can only be constructed with the `page`, `slide`, `sheet_range`, `sheet_object`, and `object` factories. Page and slide numbers are positive integers. Sheet and object locators include their required nonempty IDs and ranges.

## Authorization boundaries and operating modes

Every pinned query is rechecked at five release boundaries: retrieval, source load, optional model execution, result serialization, and highlight opening. A check reads current policy and any configured serving block. Policy read failures or invalid serving decisions fail closed as `POLICY_UNAVAILABLE`, preserving `request failed closed`. A revoked grant, changed epoch, or deletion serving block raises `AUTHORIZATION_CHANGED`; partial results are discarded, and retry advice is `ONCE_FROM_FRESH_SNAPSHOT`.

`AuthorizationGuard.policy_only(store)` enforces current grants and policy epochs. It does not claim to enforce deletion state. `AuthorizationGuard.deletion_managed(store, serving_blocked)` requires an explicit callable serving gate and checks it at pin and each boundary. The gate must represent the authoritative deletion state for the document. The constructor rejects ambiguous mode/blocker combinations.

`QueryPin` binds principal ID, group hash, policy epoch, UUID query snapshot ID, document ID, document version, and generation ID. Its cache key contains all seven values, preventing cross-principal, group, policy, snapshot, document-version, or generation reuse. The reference `MemoryPolicyStore` increments a document epoch on grant changes and revocation.

## Index generation contract

Each immutable generation binds its ID, document version, source hash, schema version, ACL projection version, state, and exactly one manifest of each kind: `text`, `structure`, and `visual`. Each manifest has a lowercase 64-character SHA-256 and nonnegative integer row count. Only a `VALIDATED` candidate can be promoted. Promotion compares the caller's `expected_active_id` with the current active ID under a lock; mismatch raises `GENERATION_CONFLICT` and leaves the catalog unchanged. The persisted catalog has at most one `ACTIVE` generation, is written through a flushed/fsynced temporary JSON file plus atomic replacement, and retains a fingerprint for every generation ID ever activated. An ID already in that history cannot be rebound to different content; exact retry of the current active generation is idempotent. `recover()` removes an abandoned temporary file.

## Derivatives and deletion

`DerivativeKind` covers `ORIGINAL_COPY`, `RENDER`, `CROP`, `NATIVE_EXTRACTION`, `OCR_EXTRACTION`, `EMBEDDING`, `INDEX_POSTING`, `CACHE`, `TEMPORARY`, `ANNOTATION_EXPORT`, `HISTORY`, `LOG_REFERENCE`, and `BACKUP_REFERENCE`. Every derivative records its ID, source hash, document version, generation, kind, backend name, object key, and optional parent derivative ID. Registry publication records lineage and writes the object while holding the registry lock; tombstoned sources cannot receive new lineage. A storage backend instance is bound to one registry, and the same backend authority must be supplied for verification.

Deletion order is tombstone first, then enumerate the complete registered derivative lineage, delete each derivative through its recorded backend, and verify backend receipts against negative lookups. Missing, foreign, mismatched, or failing backends do not produce a success receipt. `DELETED_VERIFIED` means all expected derivatives have successful deletion receipts and negative lookups. `DELETION_PENDING_RETENTION` means at least one retained derivative remains under a retention lock; `deleted_count` counts only derivatives verified absent. Backups must be registered as derivatives to be covered by this guarantee. A tombstone remains even when verification fails.

## Events and safe errors

Routine events use `SafeEvent` and the sole event name `stage_finished`. Stages are `authorize`, `delete`, `generate`, `index`, `retrieve`, `source_load`, `model`, `serialize`, and `highlight_open`. IDs are registry-issued random opaque IDs for run, query, document, and generation. The only metric keys are `duration_ms`, `candidate_count`, `denied_count`, `retry_count`, `working_set_bytes`, `queue_ms`, `deleted_count`, and `coordinate_validation_failures`; values are booleans or finite numbers. Payload text, prompts/questions, document names, coordinates, crops, embeddings, and arbitrary fields are prohibited. `JsonlEventWriter` appends compact JSONL records and serializes writes with a process-local thread lock; it does not provide cross-process locking, so separate processes require separate files.

`ErrorCode` values are `AUTHORIZATION_CHANGED`, `POLICY_UNAVAILABLE`, `POLICY_DENIED`, `ARTIFACT_HASH_MISMATCH`, `INVALID_EVIDENCE`, `GENERATION_CONFLICT`, `DELETION_VERIFICATION_FAILED`, and `DEGRADED_RESOURCE_MODE`. `RetryAdvice` is `NEVER`, `SAFE`, or `ONCE_FROM_FRESH_SNAPSHOT`. Safe serialization contains exactly `code`, `stage`, `observed`, `expected`, `protected_state`, `retry`, `next_action`, and `diagnostic_id`; `sensitive_context` is excluded.

## Compatibility and conformance

Additive optional fields require a minor schema version. Removing or renaming fields, or changing their meaning, requires a new major contract and migration tests. The evidence v1 field set and the export lists above are frozen for this contract revision.

Run the public surface check with `py -3.11 -m pytest tests/conformance/test_public_contract.py -v` (1 test). Run the complete conformance suite with `py -3.11 -m pytest tests/conformance -v` (10 tests: 1 public-surface test and 9 synthetic-flow tests). The earlier plan's `21 passed` estimate is stale. The full repository suite currently contains 165 tests.
