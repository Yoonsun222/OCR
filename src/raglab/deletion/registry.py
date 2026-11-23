from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from uuid import uuid4

from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice


class DerivativeKind(StrEnum):
    ORIGINAL_COPY = "ORIGINAL_COPY"
    RENDER = "RENDER"
    CROP = "CROP"
    NATIVE_EXTRACTION = "NATIVE_EXTRACTION"
    OCR_EXTRACTION = "OCR_EXTRACTION"
    EMBEDDING = "EMBEDDING"
    INDEX_POSTING = "INDEX_POSTING"
    CACHE = "CACHE"
    TEMPORARY = "TEMPORARY"
    ANNOTATION_EXPORT = "ANNOTATION_EXPORT"
    HISTORY = "HISTORY"
    LOG_REFERENCE = "LOG_REFERENCE"
    BACKUP_REFERENCE = "BACKUP_REFERENCE"


class DeletionStatus(StrEnum):
    DELETED_VERIFIED = "DELETED_VERIFIED"
    DELETION_PENDING_RETENTION = "DELETION_PENDING_RETENTION"


@dataclass(frozen=True, slots=True)
class Derivative:
    derivative_id: str
    source_hash: str
    document_version: str
    generation_id: str
    kind: DerivativeKind
    backend_name: str
    object_key: str
    parent_derivative_id: str | None = None


@dataclass(frozen=True, slots=True)
class BackendDeletionReceipt:
    operation_id: str
    object_absent: bool


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    deletion_job_id: str
    source_hash: str
    expected_derivatives: tuple[str, ...]
    deleted_count: int
    backend_operation_ids: Mapping[str, str]
    negative_lookup_results: Mapping[str, bool]
    key_destruction_result: bool | None
    verified_at: datetime
    verifier_version: str
    status: DeletionStatus


class MemoryDerivativeRegistry:
    def __init__(self) -> None:
        self._derivatives: dict[str, Derivative] = {}
        self._tombstones: set[str] = set()
        self._lock = Lock()

    def register(self, derivative: Derivative) -> None:
        with self._lock:
            if derivative.source_hash in self._tombstones:
                raise ValueError("cannot register a derivative for a tombstoned source")
            existing = self._derivatives.get(derivative.derivative_id)
            if existing is not None and existing != derivative:
                raise ValueError("derivative identifier is already bound")
            self._derivatives[derivative.derivative_id] = derivative

    def tombstone(self, source_hash: str) -> None:
        with self._lock:
            self._tombstones.add(source_hash)

    def is_tombstoned(self, source_hash: str) -> bool:
        with self._lock:
            return source_hash in self._tombstones

    def derivatives_for(self, source_hash: str) -> tuple[Derivative, ...]:
        with self._lock:
            return tuple(
                derivative for derivative in self._derivatives.values()
                if derivative.source_hash == source_hash
            )


class MemoryStorageBackend:
    def __init__(self, name: str, retention_locked: bool = False) -> None:
        self.name = name
        self.retention_locked = retention_locked
        self._object_keys: set[str] = set()
        self._lock = Lock()

    def put(self, object_key: str) -> None:
        with self._lock:
            self._object_keys.add(object_key)

    def delete(self, object_key: str) -> BackendDeletionReceipt:
        with self._lock:
            if not self.retention_locked:
                self._object_keys.discard(object_key)
            return BackendDeletionReceipt(uuid4().hex, object_key not in self._object_keys)

    def exists(self, object_key: str) -> bool:
        with self._lock:
            return object_key in self._object_keys


class DeletionCoordinator:
    def __init__(
        self, registry: MemoryDerivativeRegistry, backends: Mapping[str, MemoryStorageBackend]
    ) -> None:
        self._registry = registry
        self._backends = dict(backends)

    def delete_document(self, source_hash: str) -> DeletionReceipt:
        self._registry.tombstone(source_hash)
        derivatives = sorted(
            self._registry.derivatives_for(source_hash), key=lambda derivative: derivative.derivative_id
        )
        expected_derivatives = tuple(derivative.derivative_id for derivative in derivatives)
        deletion_job_id = uuid4().hex
        operations: dict[str, BackendDeletionReceipt] = {}

        for derivative in derivatives:
            backend = self._backends.get(derivative.backend_name)
            if backend is None:
                raise _verification_failed(deletion_job_id, "required storage backend unavailable")
            try:
                operation = backend.delete(derivative.object_key)
            except Exception:
                raise _verification_failed(deletion_job_id, "storage deletion failed") from None
            operations[derivative.derivative_id] = operation

        negative_lookups: dict[str, bool] = {}
        for derivative in derivatives:
            backend = self._backends[derivative.backend_name]
            operation = operations[derivative.derivative_id]
            try:
                remains = backend.exists(derivative.object_key)
            except Exception:
                raise _verification_failed(deletion_job_id, "storage lookup failed") from None
            if (
                not isinstance(operation, BackendDeletionReceipt)
                or not isinstance(operation.operation_id, str)
                or not operation.operation_id.strip()
                or type(remains) is not bool
                or operation.object_absent is not (not remains)
            ):
                raise _verification_failed(deletion_job_id, "invalid or contradictory backend result")
            if remains and not backend.retention_locked:
                raise _verification_failed(deletion_job_id, "derivative remains outside retention")
            negative_lookups[derivative.derivative_id] = not remains

        deleted_count = sum(negative_lookups.values())
        status = (
            DeletionStatus.DELETED_VERIFIED
            if deleted_count == len(expected_derivatives)
            else DeletionStatus.DELETION_PENDING_RETENTION
        )
        return DeletionReceipt(
            deletion_job_id=deletion_job_id,
            source_hash=source_hash,
            expected_derivatives=expected_derivatives,
            deleted_count=deleted_count,
            backend_operation_ids=MappingProxyType({
                derivative_id: operation.operation_id for derivative_id, operation in operations.items()
            }),
            negative_lookup_results=MappingProxyType(negative_lookups),
            key_destruction_result=None,
            verified_at=datetime.now(UTC),
            verifier_version="deletion.v1",
            status=status,
        )


def _verification_failed(deletion_job_id: str, observed: str) -> RaglabError:
    return RaglabError(
        code=ErrorCode.DELETION_VERIFICATION_FAILED,
        stage="verify_deletion",
        observed=observed,
        expected="each derivative verified absent or accounted for by retention",
        protected_state="source tombstoned",
        retry=RetryAdvice.SAFE,
        next_action="inspect the configured storage backends before retrying",
        diagnostic_id=deletion_job_id,
    )
