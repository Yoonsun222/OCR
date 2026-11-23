from dataclasses import FrozenInstanceError, replace
from datetime import UTC

import pytest

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.deletion.registry import (
    DeletionCoordinator,
    DeletionStatus,
    Derivative,
    DerivativeKind,
    MemoryDerivativeRegistry,
    MemoryStorageBackend,
)


SOURCE_HASH = "s" * 64


def test_delete_tombstones_before_backend_removal_and_verifies_absence() -> None:
    registry = MemoryDerivativeRegistry()

    class CheckingBackend(MemoryStorageBackend):
        def delete(self, object_key: str):
            assert registry.is_tombstoned(SOURCE_HASH)
            return super().delete(object_key)

    backend = CheckingBackend("local-index")
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, backend.name, "opaque/object/01", None,
    )
    registry.register(derivative)
    backend.put(derivative.object_key)

    receipt = DeletionCoordinator(registry, {backend.name: backend}).delete_document(SOURCE_HASH)

    assert registry.is_tombstoned(SOURCE_HASH)
    assert receipt.status is DeletionStatus.DELETED_VERIFIED
    assert receipt.deleted_count == 1
    assert receipt.negative_lookup_results == {"der_01": True}
    assert not backend.exists(derivative.object_key)


def test_retained_backup_never_reports_deleted_verified() -> None:
    registry = MemoryDerivativeRegistry()
    backup = MemoryStorageBackend("backup", retention_locked=True)
    derivative = Derivative(
        "der_02", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.BACKUP_REFERENCE, backup.name, "opaque/backup/02", None,
    )
    registry.register(derivative)
    backup.put(derivative.object_key)
    coordinator = DeletionCoordinator(registry, {backup.name: backup})

    receipt = coordinator.delete_document(SOURCE_HASH)
    retry = coordinator.delete_document(SOURCE_HASH)

    assert receipt.status is retry.status is DeletionStatus.DELETION_PENDING_RETENTION
    assert receipt.deleted_count == retry.deleted_count == 0
    assert receipt.negative_lookup_results == {"der_02": False}
    assert retry.deletion_job_id != receipt.deletion_job_id
    assert backup.exists(derivative.object_key)


def test_receipt_freezes_sorted_lineage_and_records_fresh_verification() -> None:
    events = []

    class CountingRegistry(MemoryDerivativeRegistry):
        def derivatives_for(self, source_hash: str):
            assert self.is_tombstoned(source_hash)
            events.append("enumerate")
            return super().derivatives_for(source_hash)

    class RecordingBackend(MemoryStorageBackend):
        def delete(self, object_key: str):
            events.append("delete")
            return super().delete(object_key)

        def exists(self, object_key: str) -> bool:
            events.append("lookup")
            return super().exists(object_key)

    registry = CountingRegistry()
    backend = RecordingBackend("local")
    for derivative_id, kind in [
        ("der_02", DerivativeKind.TEMPORARY),
        ("der_01", DerivativeKind.CACHE),
    ]:
        derivative = Derivative(
            derivative_id, SOURCE_HASH, "doc-v1", "gen_01",
            kind, backend.name, f"opaque/{derivative_id}", None,
        )
        registry.register(derivative)
        backend.put(derivative.object_key)
    unrelated = replace(
        derivative, derivative_id="der_03", source_hash="t" * 64, object_key="opaque/der_03"
    )
    registry.register(unrelated)
    backend.put(unrelated.object_key)
    coordinator = DeletionCoordinator(registry, {backend.name: backend})

    receipt = coordinator.delete_document(SOURCE_HASH)
    retry = coordinator.delete_document(SOURCE_HASH)

    assert events == ["enumerate", "delete", "delete", "lookup", "lookup"] * 2
    assert backend.exists(unrelated.object_key)
    assert receipt.source_hash == SOURCE_HASH
    assert receipt.expected_derivatives == retry.expected_derivatives == ("der_01", "der_02")
    assert receipt.deleted_count == retry.deleted_count == 2
    assert set(receipt.backend_operation_ids) == {"der_01", "der_02"}
    assert all(receipt.backend_operation_ids.values())
    assert receipt.negative_lookup_results == {"der_01": True, "der_02": True}
    assert receipt.key_destruction_result is None
    assert receipt.verified_at.tzinfo is UTC
    assert receipt.verifier_version == "deletion.v1"
    assert receipt.status is retry.status is DeletionStatus.DELETED_VERIFIED
    assert receipt.deletion_job_id != retry.deletion_job_id
    with pytest.raises(FrozenInstanceError):
        receipt.deleted_count = 0
    with pytest.raises(TypeError):
        receipt.negative_lookup_results["der_01"] = False


def test_tombstone_rejects_new_derivatives() -> None:
    registry = MemoryDerivativeRegistry()
    registry.tombstone(SOURCE_HASH)
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, "local", "opaque/object/01", None,
    )

    with pytest.raises(ValueError, match="tombstoned"):
        registry.register(derivative)
    assert registry.derivatives_for(SOURCE_HASH) == ()


def test_missing_backend_preserves_tombstone_and_raises_typed_error() -> None:
    registry = MemoryDerivativeRegistry()
    registry.register(Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, "missing", "opaque/object/01", None,
    ))

    with pytest.raises(RaglabError) as caught:
        DeletionCoordinator(registry, {}).delete_document(SOURCE_HASH)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert registry.is_tombstoned(SOURCE_HASH)
    assert "opaque/object/01" not in str(caught.value.to_safe_dict())


@pytest.mark.parametrize(
    ("retention_locked", "reported_absent", "key_remains"),
    [(False, True, True), (True, False, False), (False, False, True)],
)
def test_unverifiable_backend_results_raise_typed_error(
    retention_locked: bool, reported_absent: bool, key_remains: bool
) -> None:
    class UnverifiableBackend(MemoryStorageBackend):
        def delete(self, object_key: str):
            operation = super().delete(object_key)
            return replace(operation, object_absent=reported_absent)

        def exists(self, object_key: str) -> bool:
            return key_remains

    registry = MemoryDerivativeRegistry()
    backend = UnverifiableBackend("local", retention_locked=retention_locked)
    registry.register(Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, backend.name, "opaque/object/01", None,
    ))
    backend.put("opaque/object/01")

    with pytest.raises(RaglabError) as caught:
        DeletionCoordinator(registry, {backend.name: backend}).delete_document(SOURCE_HASH)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert registry.is_tombstoned(SOURCE_HASH)
