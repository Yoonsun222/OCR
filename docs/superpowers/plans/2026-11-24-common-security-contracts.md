# Common Security Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the dependency-light common contracts that every document adapter must satisfy before any real company document may be processed.

**Architecture:** A Python 3.11 package exposes immutable dataclasses for evidence, authorization snapshots, index generations, derivatives, typed errors, and content-free events. JSON files use write-to-temp plus `os.replace` for crash-safe local persistence; service boundaries are `Protocol` interfaces so later storage engines can replace the in-memory and JSON reference implementations without changing Track A/B/C.

**Tech Stack:** Python 3.11, Python standard library (`dataclasses`, `enum`, `hashlib`, `json`, `pathlib`, `typing`, `uuid`), pytest 8.3.5 as the only development dependency.

## Global Constraints

- Primary runtime is a company-controlled Windows CPU workstation; Linux-only behavior is not accepted.
- No security document, extracted content, document name, crop, embedding, or external model is used in this plan; fixtures are synthetic and committed as text JSON.
- Network access is not required at build, test, or runtime after the approved wheelhouse is prepared.
- Authorization is fail-closed and checked at retrieval, source load, optional model execution, serialization, and highlight opening.
- A query pins principal, group hash, policy epoch, query snapshot, document version, and active generation; an epoch mismatch returns `AUTHORIZATION_CHANGED` and discards partial results.
- Only one validated immutable generation becomes `ACTIVE`; partial or mixed generations are never served.
- Evidence coordinates use rendered top-left normalized space, finite numbers, positive areas, tagged native locators, and an exact render artifact hash.
- Deletion is tombstone-first and `DELETED_VERIFIED` requires per-backend receipts plus negative lookups; retained backups remain `DELETION_PENDING_RETENTION`.
- Routine logs contain opaque IDs and metrics only; payload text, file names, questions, coordinates, and crops are forbidden.
- All behavior is developed test-first. Each task finishes with its focused test suite and a small commit.

---

## File Structure

```text
pyproject.toml                         package metadata and pytest configuration
src/raglab/__init__.py                public package version only
src/raglab/contracts/__init__.py      stable public contract exports
src/raglab/contracts/errors.py        typed safe errors and retry metadata
src/raglab/contracts/evidence.py      EvidenceRegionV1 geometry and locators
src/raglab/security/authorization.py  policy epochs, query pins, reauthorization
src/raglab/indexing/generation.py     immutable manifests and ACTIVE CAS catalog
src/raglab/deletion/registry.py       derivative lineage and deletion receipts
src/raglab/observability/events.py    payload-free structured event writer
src/raglab/conformance/synthetic.py   one complete synthetic contract scenario
tests/contracts/test_errors.py
tests/contracts/test_evidence.py
tests/security/test_authorization.py
tests/indexing/test_generation.py
tests/deletion/test_registry.py
tests/observability/test_events.py
tests/conformance/test_synthetic_flow.py
tests/fixtures/evidence/label_target.json
docs/contracts/common-security-contracts.md
```

### Task 1: Create the package and typed safe-error contract

**Files:**
- Create: `pyproject.toml`
- Create: `src/raglab/__init__.py`
- Create: `src/raglab/contracts/__init__.py`
- Create: `src/raglab/contracts/errors.py`
- Test: `tests/contracts/test_errors.py`

**Interfaces:**
- Consumes: no project code.
- Produces: `ErrorCode`, `RetryAdvice`, and `RaglabError.to_safe_dict() -> dict[str, object]` for every later task.

- [ ] **Step 1: Write the package metadata and failing error tests**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools==75.8.0"]
build-backend = "setuptools.build_meta"

[project]
name = "secure-multimodal-rag-lab"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = []

[project.optional-dependencies]
dev = ["pytest==8.3.5"]

[tool.pytest.ini_options]
addopts = "-q --strict-markers"
testpaths = ["tests"]
pythonpath = ["src"]
```

```python
# tests/contracts/test_errors.py
from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice


def test_safe_error_never_serializes_sensitive_context() -> None:
    error = RaglabError(
        code=ErrorCode.AUTHORIZATION_CHANGED,
        stage="serialize",
        observed="policy epoch 8",
        expected="policy epoch 7",
        protected_state="partial results discarded",
        retry=RetryAdvice.ONCE_FROM_FRESH_SNAPSHOT,
        next_action="restart authorization once",
        diagnostic_id="diag_01JTEST",
        sensitive_context={"file_name": "secret.pdf", "question": "part A?"},
    )
    safe = error.to_safe_dict()
    assert safe["code"] == "AUTHORIZATION_CHANGED"
    assert safe["retry"] == "ONCE_FROM_FRESH_SNAPSHOT"
    assert "secret.pdf" not in repr(safe)
    assert "part A?" not in repr(safe)


def test_unknown_error_fields_are_not_available() -> None:
    error = RaglabError.policy_denied("source_load", "diag_02JTEST")
    assert set(error.to_safe_dict()) == {
        "code", "stage", "observed", "expected", "protected_state",
        "retry", "next_action", "diagnostic_id",
    }
```

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run: `py -3.11 -m pytest tests/contracts/test_errors.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'raglab'`.

- [ ] **Step 3: Implement the immutable error types and safe serializer**

```python
# src/raglab/contracts/errors.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class ErrorCode(StrEnum):
    AUTHORIZATION_CHANGED = "AUTHORIZATION_CHANGED"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    POLICY_DENIED = "POLICY_DENIED"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    GENERATION_CONFLICT = "GENERATION_CONFLICT"
    DELETION_VERIFICATION_FAILED = "DELETION_VERIFICATION_FAILED"
    DEGRADED_RESOURCE_MODE = "DEGRADED_RESOURCE_MODE"


class RetryAdvice(StrEnum):
    NEVER = "NEVER"
    SAFE = "SAFE"
    ONCE_FROM_FRESH_SNAPSHOT = "ONCE_FROM_FRESH_SNAPSHOT"


@dataclass(frozen=True, slots=True)
class RaglabError(Exception):
    code: ErrorCode
    stage: str
    observed: str
    expected: str
    protected_state: str
    retry: RetryAdvice
    next_action: str
    diagnostic_id: str
    sensitive_context: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def policy_denied(cls, stage: str, diagnostic_id: str) -> "RaglabError":
        return cls(
            ErrorCode.POLICY_DENIED, stage, "access denied", "current authorization",
            "no source or result released", RetryAdvice.NEVER,
            "request access from the document owner", diagnostic_id,
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "stage": self.stage,
            "observed": self.observed,
            "expected": self.expected,
            "protected_state": self.protected_state,
            "retry": self.retry.value,
            "next_action": self.next_action,
            "diagnostic_id": self.diagnostic_id,
        }
```

Create `src/raglab/__init__.py` with `__version__ = "0.1.0"`; create both package `__init__.py` files and export the three public error types from `src/raglab/contracts/__init__.py`.

- [ ] **Step 4: Run the focused tests**

Run: `py -3.11 -m pytest tests/contracts/test_errors.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit the error contract**

```powershell
git add pyproject.toml src/raglab tests/contracts/test_errors.py
git commit -m "feat: add safe typed error contract"
```

### Task 2: Implement EvidenceRegionV1 and coordinate validation

**Files:**
- Create: `src/raglab/contracts/evidence.py`
- Modify: `src/raglab/contracts/__init__.py`
- Create: `tests/contracts/test_evidence.py`
- Create: `tests/fixtures/evidence/label_target.json`

**Interfaces:**
- Consumes: `RaglabError`, `ErrorCode`, `RetryAdvice`.
- Produces: `Point`, `BBox`, `EvidenceRegion`, `NativeLocator`, `EvidenceRegionV1`, `EvidenceRelation`, `EvidenceRole`, `ExtractionMethod`, `validate_artifact_match(evidence, region_id, actual_hash)`, and JSON-safe `to_dict()` methods.

- [ ] **Step 1: Add the synthetic label-target fixture and failing validation tests**

```json
{
  "source_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "artifact_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "label": {"bbox": [0.10, 0.20, 0.20, 0.10], "role": "label"},
  "target": {"polygon": [[0.40, 0.20], [0.70, 0.20], [0.70, 0.60], [0.40, 0.60]], "role": "target"}
}
```

```python
# tests/contracts/test_evidence.py
import math
import pytest

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.contracts.evidence import (
    BBox, EvidenceRegion, EvidenceRegionV1, EvidenceRelation, EvidenceRole,
    ExtractionMethod, NativeLocator, Point, validate_artifact_match,
)


def valid_evidence() -> EvidenceRegionV1:
    return EvidenceRegionV1(
        evidence_id="ev_01", source_hash="b" * 64, document_version="doc-v1",
        generation_id="gen_01", native_locator=NativeLocator.page(1),
        role=EvidenceRole.TARGET, text="A-12", extraction_method=ExtractionMethod.SYNTHETIC,
        confidence=1.0, acl_snapshot_id="acl-01", metadata={},
        regions=(EvidenceRegion(
            region_id="ev_01:target", artifact_id="artifact_01", artifact_hash="a" * 64,
            artifact_mime_type="image/png", renderer_name="synthetic",
            renderer_version="1.0", render_profile_hash="c" * 64,
            canvas_width_px=1000, canvas_height_px=800, dpi=144, page_rotation=0,
            page_box="crop", polygon=(Point(0.4, 0.2), Point(0.7, 0.2),
            Point(0.7, 0.6), Point(0.4, 0.6)), bbox=BBox(0.4, 0.2, 0.3, 0.4),
            transform_chain=((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),),
            role=EvidenceRole.TARGET,
        ),), relations=(EvidenceRelation(EvidenceRole.TARGET, "ev_01:target", None),),
    )


def test_valid_evidence_round_trips_to_json_safe_dict() -> None:
    data = valid_evidence().to_dict()
    assert data["regions"][0]["coordinate_space"] == "rendered_top_left_normalized"
    assert data["native_locator"] == {"kind": "page", "page_number": 1}


def test_native_evidence_may_omit_rendered_regions() -> None:
    item = EvidenceRegionV1(
        evidence_id="cell-01", source_hash="b" * 64, document_version="doc-v1",
        generation_id="gen_01", native_locator=NativeLocator.sheet_range("sheet-1", "B2"),
        role=EvidenceRole.VALUE, text="12.5", regions=(), relations=(),
        extraction_method=ExtractionMethod.NATIVE, confidence=1.0,
        acl_snapshot_id="acl-01", metadata={"unit": "MPa"},
    )
    assert item.regions == ()


@pytest.mark.parametrize("value", [-0.01, 1.01, math.inf, math.nan])
def test_point_rejects_out_of_range_or_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError):
        Point(value, 0.5)


def test_bbox_rejects_zero_area() -> None:
    with pytest.raises(ValueError):
        BBox(0.1, 0.2, 0.0, 0.4)


def test_highlight_rejects_wrong_artifact_hash() -> None:
    with pytest.raises(RaglabError) as raised:
        validate_artifact_match(valid_evidence(), "ev_01:target", "d" * 64, "diag_hash")
    assert raised.value.code is ErrorCode.ARTIFACT_HASH_MISMATCH
```

- [ ] **Step 2: Run the focused test and confirm missing evidence types**

Run: `py -3.11 -m pytest tests/contracts/test_evidence.py -v`

Expected: FAIL during collection because `raglab.contracts.evidence` does not exist.

- [ ] **Step 3: Implement immutable geometry, tagged locators, and evidence validation**

Implement `src/raglab/contracts/evidence.py` with frozen, slotted dataclasses. `Point.__post_init__` requires `math.isfinite(value)` and `0.0 <= value <= 1.0`. `BBox.__post_init__` requires positive width/height and `x + width <= 1.0`, `y + height <= 1.0`. `NativeLocator` exposes only these constructors:

```python
@classmethod
def page(cls, page_number: int) -> "NativeLocator": ...
@classmethod
def slide(cls, slide_number: int) -> "NativeLocator": ...
@classmethod
def sheet_range(cls, sheet_id: str, cell_range: str) -> "NativeLocator": ...
@classmethod
def sheet_object(cls, sheet_id: str, object_id: str) -> "NativeLocator": ...
@classmethod
def object(cls, object_id: str) -> "NativeLocator": ...
```

Define `EvidenceRole(StrEnum)` with `LABEL`, `TARGET`, `LEGEND`, `SERIES`, `AXIS`, `VALUE`, and `CONTEXT`; define `ExtractionMethod(StrEnum)` with `NATIVE`, `OCR`, `SYNTHETIC`, and `VISUAL_MODEL`. `EvidenceRegion` owns artifact/render/coordinate fields and requires 64-character lowercase hexadecimal artifact and render-profile hashes, positive canvas dimensions and DPI, rotation in `{0, 90, 180, 270}`, at least three polygon points, and one or more nine-value transform matrices. `EvidenceRegionV1` is the envelope: it owns source/document/generation/native locator, top-level role/text, `regions`, typed `relations`, extraction method, confidence, ACL snapshot ID, and JSON-safe metadata. Native evidence may use `regions=()`; every rendered region requires its exact artifact. Confidence is in `[0,1]`, relation targets must refer to a region or explicit native formula/range, and schema version is exactly `evidence-region.v1`. Every `to_dict()` emits only JSON primitives.

Implement the artifact check exactly as:

```python
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
```

Export the public evidence types from `src/raglab/contracts/__init__.py`.

- [ ] **Step 4: Run the EvidenceRegionV1 tests**

Run: `py -3.11 -m pytest tests/contracts/test_evidence.py -v`

Expected: `8 passed`.

- [ ] **Step 5: Commit the evidence contract**

```powershell
git add src/raglab/contracts tests/contracts/test_evidence.py tests/fixtures/evidence/label_target.json
git commit -m "feat: add evidence region v1 contract"
```

### Task 3: Close authorization TOCTOU with policy epochs

**Files:**
- Create: `src/raglab/security/__init__.py`
- Create: `src/raglab/security/authorization.py`
- Test: `tests/security/test_authorization.py`

**Interfaces:**
- Consumes: typed errors from Task 1.
- Produces: `PolicySnapshot`, `QueryPin`, `CacheKey`, `MemoryPolicyStore`, `AuthorizationGuard.pin()`, and `AuthorizationGuard.check(pin, stage)`.

- [ ] **Step 1: Write failing tests for five authorization boundaries and cache isolation**

```python
# tests/security/test_authorization.py
import pytest

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore


def test_epoch_change_discards_result_at_every_boundary() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    guard = AuthorizationGuard(store)
    pin = guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    store.revoke("doc_01")
    for stage in ("retrieve", "source_load", "model", "serialize", "highlight_open"):
        with pytest.raises(RaglabError) as raised:
            guard.check(pin, stage, diagnostic_id=f"diag_{stage}")
        assert raised.value.code is ErrorCode.AUTHORIZATION_CHANGED
        assert raised.value.protected_state == "partial results discarded"


def test_cache_key_changes_with_principal_group_epoch_and_generation() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    guard = AuthorizationGuard(store)
    first = guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    store.revoke("doc_01")
    store.allow("user_01", "doc_01", group_hash="h" * 64)
    second = guard.pin("user_01", "h" * 64, "doc_01", "doc-v1", "gen_02")
    assert first.cache_key() != second.cache_key()


def test_policy_store_failure_is_fail_closed() -> None:
    store = MemoryPolicyStore()
    store.fail_reads = True
    with pytest.raises(RaglabError) as raised:
        AuthorizationGuard(store).pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    assert raised.value.code is ErrorCode.POLICY_UNAVAILABLE
```

- [ ] **Step 2: Run authorization tests and confirm the missing-module failure**

Run: `py -3.11 -m pytest tests/security/test_authorization.py -v`

Expected: FAIL during collection because `raglab.security.authorization` does not exist.

- [ ] **Step 3: Implement pinning, monotonic epochs, and fail-closed checks**

`PolicySnapshot` contains `document_id`, `policy_epoch`, `allowed_principals`, and `group_hashes`. `QueryPin` contains `principal_id`, `group_hash`, `policy_epoch`, a UUID4 `query_snapshot_id`, `document_id`, `document_version`, and `generation_id`. Its `cache_key()` returns a `CacheKey` containing every one of those security-sensitive dimensions.

`MemoryPolicyStore.revoke(document_id)` must increment the epoch before clearing grants. `AuthorizationGuard.check()` must retrieve the current snapshot and raise this exact error on any epoch or grant change:

```python
raise RaglabError(
    ErrorCode.AUTHORIZATION_CHANGED, stage,
    f"policy epoch {current.policy_epoch}", f"policy epoch {pin.policy_epoch}",
    "partial results discarded", RetryAdvice.ONCE_FROM_FRESH_SNAPSHOT,
    "restart authorization once", diagnostic_id,
)
```

If the store read raises, convert it to `POLICY_UNAVAILABLE`, `RetryAdvice.SAFE`, and `protected_state="request failed closed"`. Do not expose whether a denied document exists.

- [ ] **Step 4: Run authorization tests**

Run: `py -3.11 -m pytest tests/security/test_authorization.py -v`

Expected: `3 passed`.

- [ ] **Step 5: Commit authorization pinning**

```powershell
git add src/raglab/security tests/security/test_authorization.py
git commit -m "feat: enforce policy epochs across query stages"
```

### Task 4: Build immutable index generations and ACTIVE compare-and-swap

**Files:**
- Create: `src/raglab/indexing/__init__.py`
- Create: `src/raglab/indexing/generation.py`
- Test: `tests/indexing/test_generation.py`

**Interfaces:**
- Consumes: typed generation-conflict errors.
- Produces: `IndexManifest`, `IndexGeneration`, `GenerationState`, and `JsonGenerationCatalog.promote(candidate, expected_active_id)`.

- [ ] **Step 1: Write failing tests for complete manifests, stale CAS, and crash remnants**

```python
# tests/indexing/test_generation.py
from pathlib import Path
import pytest

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.indexing.generation import (
    GenerationState, IndexGeneration, IndexManifest, JsonGenerationCatalog,
)


def candidate(generation_id: str) -> IndexGeneration:
    manifests = tuple(
        IndexManifest(kind=kind, sha256=(kind[0] * 64), row_count=3)
        for kind in ("text", "structure", "visual")
    )
    return IndexGeneration(generation_id, "doc-v1", "s" * 64,
        "evidence-region.v1", 4, manifests, GenerationState.VALIDATED)


def test_promote_requires_all_three_validated_manifests(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    active = catalog.promote(candidate("gen_01"), expected_active_id=None)
    assert active.generation_id == "gen_01"
    assert catalog.read_active().generation_id == "gen_01"


def test_stale_compare_and_swap_preserves_current_active(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    catalog.promote(candidate("gen_01"), expected_active_id=None)
    with pytest.raises(RaglabError) as raised:
        catalog.promote(candidate("gen_02"), expected_active_id=None)
    assert raised.value.code is ErrorCode.GENERATION_CONFLICT
    assert catalog.read_active().generation_id == "gen_01"


def test_recover_removes_temp_catalog_but_keeps_active(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    catalog.promote(candidate("gen_01"), expected_active_id=None)
    catalog.temp_path.write_text("partial", encoding="utf-8")
    catalog.recover()
    assert not catalog.temp_path.exists()
    assert catalog.read_active().generation_id == "gen_01"
```

- [ ] **Step 2: Run tests and confirm missing generation implementation**

Run: `py -3.11 -m pytest tests/indexing/test_generation.py -v`

Expected: FAIL during collection because `raglab.indexing.generation` does not exist.

- [ ] **Step 3: Implement immutable manifests and atomic catalog promotion**

`IndexGeneration.__post_init__` must require exactly one manifest for each of `text`, `structure`, and `visual`; all SHA-256 values are lowercase hexadecimal; all row counts are non-negative; only `VALIDATED` may be promoted. `JsonGenerationCatalog` exposes:

```python
class JsonGenerationCatalog:
    def __init__(self, path: Path): ...
    @property
    def temp_path(self) -> Path: ...
    def read_active(self) -> IndexGeneration | None: ...
    def promote(
        self, candidate: IndexGeneration, expected_active_id: str | None
    ) -> IndexGeneration: ...
    def recover(self) -> None: ...
```

Inside `promote`, read the current active ID, compare it to `expected_active_id`, serialize a copy of the candidate with state `ACTIVE`, flush and `os.fsync()` the temporary file, then call `os.replace(temp_path, path)`. On mismatch raise `GENERATION_CONFLICT` and leave both the catalog and candidate unchanged. `recover()` deletes only the known sibling temporary path and never scans or deletes a broad directory.

- [ ] **Step 4: Run generation and crash-recovery tests**

Run: `py -3.11 -m pytest tests/indexing/test_generation.py -v`

Expected: `3 passed`.

- [ ] **Step 5: Commit immutable generation promotion**

```powershell
git add src/raglab/indexing tests/indexing/test_generation.py
git commit -m "feat: add crash-safe index generation catalog"
```

### Task 5: Register derivatives and produce verifiable deletion receipts

**Files:**
- Create: `src/raglab/deletion/__init__.py`
- Create: `src/raglab/deletion/registry.py`
- Test: `tests/deletion/test_registry.py`

**Interfaces:**
- Consumes: typed deletion errors.
- Produces: `Derivative`, `DerivativeKind`, `DeletionStatus`, `MemoryDerivativeRegistry`, `MemoryStorageBackend`, and `DeletionCoordinator.delete_document()`.

- [ ] **Step 1: Write failing tombstone-first and backup-retention tests**

```python
# tests/deletion/test_registry.py
from raglab.deletion.registry import (
    DeletionCoordinator, DeletionStatus, Derivative, DerivativeKind,
    MemoryDerivativeRegistry, MemoryStorageBackend,
)


def test_delete_tombstones_before_backend_removal_and_verifies_absence() -> None:
    registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("local-index")
    derivative = Derivative("der_01", "s" * 64, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, "local-index", "opaque/object/01", None)
    registry.register(derivative)
    backend.put("opaque/object/01")
    receipt = DeletionCoordinator(registry, {backend.name: backend}).delete_document("s" * 64)
    assert registry.is_tombstoned("s" * 64)
    assert receipt.status is DeletionStatus.DELETED_VERIFIED
    assert receipt.deleted_count == 1
    assert receipt.negative_lookup_results == {"der_01": True}


def test_retained_backup_never_reports_deleted_verified() -> None:
    registry = MemoryDerivativeRegistry()
    backup = MemoryStorageBackend("backup", retention_locked=True)
    registry.register(Derivative("der_02", "s" * 64, "doc-v1", "gen_01",
        DerivativeKind.BACKUP_REFERENCE, "backup", "opaque/backup/02", None))
    backup.put("opaque/backup/02")
    receipt = DeletionCoordinator(registry, {backup.name: backup}).delete_document("s" * 64)
    assert receipt.status is DeletionStatus.DELETION_PENDING_RETENTION
```

- [ ] **Step 2: Run tests and confirm missing deletion registry**

Run: `py -3.11 -m pytest tests/deletion/test_registry.py -v`

Expected: FAIL during collection because `raglab.deletion.registry` does not exist.

- [ ] **Step 3: Implement lineage, tombstones, backend receipts, and negative lookup**

Define all required `DerivativeKind` values: `ORIGINAL_COPY`, `RENDER`, `CROP`, `NATIVE_EXTRACTION`, `OCR_EXTRACTION`, `EMBEDDING`, `INDEX_POSTING`, `CACHE`, `TEMPORARY`, `ANNOTATION_EXPORT`, `HISTORY`, `LOG_REFERENCE`, and `BACKUP_REFERENCE`.

`DeletionCoordinator.delete_document(source_hash)` must execute this order:

1. `registry.tombstone(source_hash)`.
2. Enumerate the registry once and freeze `expected_derivatives` as sorted IDs.
3. Ask each named backend to delete its opaque object key.
4. Ask the backend whether the key remains; record a negative lookup per derivative.
5. Return `DELETED_VERIFIED` only when every expected derivative is absent.
6. Return `DELETION_PENDING_RETENTION` if every remaining derivative belongs to a retention-locked backend.
7. Raise `DELETION_VERIFICATION_FAILED` for missing backends, contradictory receipts, or any other remaining derivative.

The returned frozen `DeletionReceipt` includes `deletion_job_id`, source hash, expected derivative IDs, deleted count, backend operation IDs, negative lookup results, optional key-destruction result, UTC verification timestamp, verifier version `deletion.v1`, and status. Calling the method again must be idempotent and produce a new receipt with the same terminal status.

- [ ] **Step 4: Run deletion tests**

Run: `py -3.11 -m pytest tests/deletion/test_registry.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit deletion receipts**

```powershell
git add src/raglab/deletion tests/deletion/test_registry.py
git commit -m "feat: verify derivative deletion with receipts"
```

### Task 6: Add payload-free structured observability

**Files:**
- Create: `src/raglab/observability/__init__.py`
- Create: `src/raglab/observability/events.py`
- Test: `tests/observability/test_events.py`

**Interfaces:**
- Consumes: opaque run/query/document/generation IDs from earlier contracts.
- Produces: `EventName`, `SafeEvent`, and `JsonlEventWriter.write(event)`.

- [ ] **Step 1: Write failing allowlist and newline-safe JSONL tests**

```python
# tests/observability/test_events.py
from pathlib import Path
import json
import pytest

from raglab.observability.events import EventName, JsonlEventWriter, SafeEvent


def test_event_rejects_payload_fields() -> None:
    with pytest.raises(ValueError, match="forbidden event field"):
        SafeEvent(EventName.STAGE_FINISHED, "run_01", "query_01", "doc_opaque",
            "gen_01", "stage", {"file_name": "secret.pdf"})


def test_writer_emits_one_json_object_without_content(tmp_path: Path) -> None:
    writer = JsonlEventWriter(tmp_path / "events.jsonl")
    writer.write(SafeEvent(EventName.STAGE_FINISHED, "run_01", "query_01",
        "doc_opaque", "gen_01", "retrieve", {"duration_ms": 42, "candidate_count": 3}))
    record = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))
    assert record["metrics"] == {"candidate_count": 3, "duration_ms": 42}
```

- [ ] **Step 2: Run tests and confirm missing observability implementation**

Run: `py -3.11 -m pytest tests/observability/test_events.py -v`

Expected: FAIL during collection because `raglab.observability.events` does not exist.

- [ ] **Step 3: Implement a strict metric allowlist and atomic line writes**

Permit only these metric keys: `duration_ms`, `candidate_count`, `denied_count`, `retry_count`, `working_set_bytes`, `queue_ms`, `deleted_count`, and `coordinate_validation_failures`. Reject keys containing or equal to `text`, `question`, `answer`, `file`, `file_name`, `path`, `coordinate`, `crop`, or `embedding`. Require numeric or boolean values, sort metric keys, add a UTC timestamp, and serialize with `ensure_ascii=True`.

`JsonlEventWriter.write` opens with `encoding="utf-8", newline="\n"`, writes exactly one compact JSON object plus one newline, flushes, and calls `os.fsync()`. It creates the parent directory only when the configured event path has a non-current parent.

- [ ] **Step 4: Run observability tests**

Run: `py -3.11 -m pytest tests/observability/test_events.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit payload-free events**

```powershell
git add src/raglab/observability tests/observability/test_events.py
git commit -m "feat: add payload-free structured events"
```

### Task 7: Prove the contracts together with one synthetic conformance flow

**Files:**
- Create: `src/raglab/conformance/__init__.py`
- Create: `src/raglab/conformance/synthetic.py`
- Test: `tests/conformance/test_synthetic_flow.py`

**Interfaces:**
- Consumes: `AuthorizationGuard`, `EvidenceRegionV1`, `JsonGenerationCatalog`, `DeletionCoordinator`, and `JsonlEventWriter`.
- Produces: `run_synthetic_conformance(workspace: Path) -> ConformanceReport` with booleans `authorized_query`, `highlight_hash_verified`, `generation_active`, `revocation_blocked`, `deletion_verified`, and `payload_free_log`.

- [ ] **Step 1: Write the end-to-end failing test**

```python
# tests/conformance/test_synthetic_flow.py
from pathlib import Path

from raglab.conformance.synthetic import run_synthetic_conformance


def test_synthetic_flow_closes_every_common_gate(tmp_path: Path) -> None:
    report = run_synthetic_conformance(tmp_path)
    assert report.to_dict() == {
        "authorized_query": True,
        "highlight_hash_verified": True,
        "generation_active": True,
        "revocation_blocked": True,
        "deletion_verified": True,
        "payload_free_log": True,
    }
    assert "secret" not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the conformance test and confirm the missing orchestrator**

Run: `py -3.11 -m pytest tests/conformance/test_synthetic_flow.py -v`

Expected: FAIL during collection because `raglab.conformance.synthetic` does not exist.

- [ ] **Step 3: Implement the deterministic synthetic scenario**

`run_synthetic_conformance` must perform this exact sequence with opaque IDs and the Task 2 fixture values:

1. Grant `user_01` access and pin epoch 1.
2. Create and validate the label and target evidence against the fixture artifact hash.
3. Build three one-row manifests and promote generation `gen_01`.
4. Check authorization at `retrieve`, `source_load`, `model`, and `serialize`.
5. Revoke the document and assert that `highlight_open` raises `AUTHORIZATION_CHANGED` without recording content.
6. Register one render and one index derivative, delete both, and require `DELETED_VERIFIED`.
7. Write content-free stage metrics and scan the final JSONL keys against the allowlist.
8. Return the frozen `ConformanceReport`; catch only the expected authorization-change error.

Do not read a real file, call a model, open a socket, or infer any environment-specific secret.

- [ ] **Step 4: Run all common contract tests**

Run: `py -3.11 -m pytest tests/contracts tests/security tests/indexing tests/deletion tests/observability tests/conformance -v`

Expected: `21 passed` and no warnings containing document content.

- [ ] **Step 5: Commit the synthetic conformance flow**

```powershell
git add src/raglab/conformance tests/conformance
git commit -m "test: add common security conformance flow"
```

### Task 8: Publish the frozen common contract reference

**Files:**
- Create: `docs/contracts/common-security-contracts.md`
- Modify: `src/raglab/contracts/__init__.py`
- Test: `tests/conformance/test_public_contract.py`

**Interfaces:**
- Consumes: every public type and method from Tasks 1-7.
- Produces: one stable import surface for Track A/B/C and a human-readable freeze checklist.

- [ ] **Step 1: Write the public-surface test**

```python
# tests/conformance/test_public_contract.py
import raglab.contracts as contracts


def test_common_contract_exports_are_frozen() -> None:
    assert set(contracts.__all__) == {
        "BBox", "ErrorCode", "EvidenceRegion", "EvidenceRegionV1",
        "EvidenceRelation", "EvidenceRole", "ExtractionMethod", "NativeLocator",
        "Point", "RaglabError", "RetryAdvice", "validate_artifact_match",
    }
```

- [ ] **Step 2: Run the public-surface test and confirm it fails on missing exports**

Run: `py -3.11 -m pytest tests/conformance/test_public_contract.py -v`

Expected: FAIL because `__all__` is absent or incomplete.

- [ ] **Step 3: Freeze exports and write the contract reference**

Set `src/raglab/contracts/__init__.py::__all__` to the exact set asserted above. Write `docs/contracts/common-security-contracts.md` with:

- the five authorization check boundaries and fail-closed behavior;
- every EvidenceRegionV1 field, coordinate convention, hash requirement, and locator kind;
- the three required index manifests and ACTIVE CAS rule;
- derivative kinds, tombstone order, and terminal deletion-state meanings;
- routine event allowlist and prohibited payload classes;
- error codes, retry meanings, and stable safe-serialization fields;
- a compatibility rule: additive optional fields require a minor schema version; removed, renamed, or semantically changed fields require a new major contract and migration tests;
- the exact conformance command and expected `21 passed` result.

- [ ] **Step 4: Run the full suite and inspect collection count**

Run: `py -3.11 -m pytest -v`

Expected: `22 passed`, zero failed, zero errors.

Run: `py -3.11 -m pytest --collect-only -q`

Expected: the final line reports `22 tests collected`.

- [ ] **Step 5: Commit the frozen contract reference**

```powershell
git add src/raglab/contracts/__init__.py docs/contracts/common-security-contracts.md tests/conformance/test_public_contract.py
git commit -m "docs: freeze common security contracts"
```

## Self-Review Results

- Spec coverage: EvidenceRegionV1, five authorization boundaries, epoch-pinned cache keys, fail-closed policy reads, immutable three-index generations, crash-safe ACTIVE promotion, derivative lineage, deletion receipts, safe errors, payload-free events, and a synthetic conformance flow each map to a task.
- Deliberate deferral: Windows bundle verification and `doctor` belong to the separate Windows offline-toolkit plan; adapter-specific PDF/PPT/DOCX/XLSX coordinate goldens belong to Track plans; statistical evaluation belongs to the integration plan.
- Placeholder scan: every code-changing step includes exact content, commands, and expected results; no deferred implementation marker remains.
- Type consistency: the public evidence and error names are defined in Tasks 1-2 and reused unchanged; authorization, generation, deletion, and event services stay behind their stated interfaces.
- Security-document boundary: all fixtures use synthetic hashes, opaque IDs, and generated geometry; no input path for a company document is added.
