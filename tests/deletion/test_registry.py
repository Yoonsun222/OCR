from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC
from threading import Event

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
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore


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
    registry.publish(derivative, backend)

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
    registry.publish(derivative, backup)
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
        registry.publish(derivative, backend)
    unrelated = replace(
        derivative, derivative_id="der_03", source_hash="t" * 64, object_key="opaque/der_03"
    )
    registry.publish(unrelated, backend)
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
    backend = MemoryStorageBackend("local")
    registry.tombstone(SOURCE_HASH)
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, backend.name, "opaque/object/01", None,
    )

    with pytest.raises(ValueError, match="tombstoned"):
        registry.register(derivative, backend)
    assert registry.derivatives_for(SOURCE_HASH) == ()


def test_missing_backend_preserves_tombstone_and_raises_typed_error() -> None:
    registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("missing")
    registry.register(Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, backend.name, "opaque/object/01", None,
    ), backend)

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
    registry.publish(Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.INDEX_POSTING, backend.name, "opaque/object/01", None,
    ), backend)

    with pytest.raises(RaglabError) as caught:
        DeletionCoordinator(registry, {backend.name: backend}).delete_document(SOURCE_HASH)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert registry.is_tombstoned(SOURCE_HASH)


@pytest.mark.parametrize("backend_fails", [False, True])
def test_tombstone_blocks_serving_before_backend_operations(backend_fails: bool) -> None:
    registry = MemoryDerivativeRegistry()
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    sources = {"doc_01": SOURCE_HASH}
    guard = AuthorizationGuard.deletion_managed(
        store, lambda document_id: registry.is_tombstoned(sources[document_id])
    )
    pin = guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    guard.check(pin, "serialize")

    class CheckingBackend(MemoryStorageBackend):
        def delete(self, object_key: str):
            with pytest.raises(RaglabError) as caught:
                guard.check(pin, "source_load")
            assert caught.value.code is ErrorCode.AUTHORIZATION_CHANGED
            if backend_fails:
                raise OSError("synthetic backend failure")
            return super().delete(object_key)

    backend = CheckingBackend("local")
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    registry.publish(derivative, backend)
    coordinator = DeletionCoordinator(registry, {backend.name: backend})

    if backend_fails:
        with pytest.raises(RaglabError) as caught:
            coordinator.delete_document(SOURCE_HASH)
        assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
        assert backend.exists(derivative.object_key)
    else:
        assert coordinator.delete_document(SOURCE_HASH).status is DeletionStatus.DELETED_VERIFIED

    with pytest.raises(RaglabError) as caught:
        guard.check(pin, "serialize")
    assert caught.value.code is ErrorCode.AUTHORIZATION_CHANGED
    with pytest.raises(RaglabError) as caught:
        guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    assert caught.value.code is ErrorCode.POLICY_DENIED


def test_unavailable_serving_blocker_fails_closed() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    original_guard = AuthorizationGuard.policy_only(store)
    pin = original_guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")

    def unavailable(_document_id: str) -> bool:
        raise OSError("synthetic tombstone lookup failure")

    guard = AuthorizationGuard.deletion_managed(store, unavailable)
    with pytest.raises(RaglabError) as caught:
        guard.check(pin, "serialize")
    assert caught.value.code is ErrorCode.POLICY_UNAVAILABLE
    with pytest.raises(RaglabError) as caught:
        guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    assert caught.value.code is ErrorCode.POLICY_UNAVAILABLE


def test_publication_winning_the_tombstone_race_is_deleted() -> None:
    writing = Event()
    deleting = Event()
    finish_write = Event()

    class SynchronizedRegistry(MemoryDerivativeRegistry):
        def tombstone(self, source_hash: str) -> None:
            deleting.set()
            super().tombstone(source_hash)

    registry = SynchronizedRegistry()

    class PausedBackend(MemoryStorageBackend):
        def _put(self, object_key: str) -> None:
            assert registry._lock.locked()
            writing.set()
            assert finish_write.wait(5)
            super()._put(object_key)

    backend = PausedBackend("local")
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    coordinator = DeletionCoordinator(registry, {backend.name: backend})

    with ThreadPoolExecutor(max_workers=2) as executor:
        publication = executor.submit(registry.publish, derivative, backend)
        try:
            assert writing.wait(5)
            deletion = executor.submit(coordinator.delete_document, SOURCE_HASH)
            assert deleting.wait(5)
        finally:
            finish_write.set()
        publication.result(timeout=5)
        receipt = deletion.result(timeout=5)

    assert receipt.expected_derivatives == ("der_01",)
    assert receipt.status is DeletionStatus.DELETED_VERIFIED
    assert not backend.exists(derivative.object_key)


def test_tombstone_winning_the_publication_race_prevents_storage_write() -> None:
    tombstoned = Event()
    finish_deletion = Event()

    class PausedRegistry(MemoryDerivativeRegistry):
        def derivatives_for(self, source_hash: str) -> tuple[Derivative, ...]:
            tombstoned.set()
            assert finish_deletion.wait(5)
            return super().derivatives_for(source_hash)

    registry = PausedRegistry()
    backend = MemoryStorageBackend("local")
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    coordinator = DeletionCoordinator(registry, {backend.name: backend})

    with ThreadPoolExecutor(max_workers=1) as executor:
        deletion = executor.submit(coordinator.delete_document, SOURCE_HASH)
        try:
            assert tombstoned.wait(5)
            with pytest.raises(ValueError, match="tombstoned"):
                registry.publish(derivative, backend)
        finally:
            finish_deletion.set()
        receipt = deletion.result(timeout=5)

    assert receipt.expected_derivatives == ()
    assert receipt.status is DeletionStatus.DELETED_VERIFIED
    with pytest.raises(ValueError, match="tombstoned"):
        registry.publish(derivative, backend)
    assert not backend.exists(derivative.object_key)


@pytest.mark.parametrize("source_hash", [SOURCE_HASH, "t" * 64])
def test_object_key_cannot_be_rebound_to_another_derivative(source_hash: str) -> None:
    registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("local")
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    registry.register(derivative, backend)
    conflicting = replace(derivative, derivative_id="der_02", source_hash=source_hash)

    with pytest.raises(RaglabError) as caught:
        registry.register(conflicting, backend)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert conflicting.object_key not in str(caught.value.to_safe_dict())
    assert registry.derivatives_for(SOURCE_HASH) == (derivative,)


def test_mixed_deletion_receipt_identifies_which_derivative_remains() -> None:
    registry = MemoryDerivativeRegistry()
    local = MemoryStorageBackend("local")
    backup = MemoryStorageBackend("backup", retention_locked=True)
    for derivative_id, kind, backend in [
        ("der_01", DerivativeKind.ORIGINAL_COPY, local),
        ("der_02", DerivativeKind.BACKUP_REFERENCE, backup),
    ]:
        derivative = Derivative(
            derivative_id, SOURCE_HASH, "doc-v1", "gen_01",
            kind, backend.name, f"opaque/{derivative_id}",
        )
        registry.publish(derivative, backend)

    receipt = DeletionCoordinator(
        registry, {local.name: local, backup.name: backup}
    ).delete_document(SOURCE_HASH)

    assert receipt.status is DeletionStatus.DELETION_PENDING_RETENTION
    assert receipt.deleted_count == 1
    assert receipt.expected_derivatives == ("der_01", "der_02")
    assert receipt.negative_lookup_results == {"der_01": True, "der_02": False}
    assert set(receipt.backend_operation_ids) == {"der_01", "der_02"}


def test_same_name_backend_cannot_verify_another_instances_publication() -> None:
    registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("local")
    same_name = MemoryStorageBackend(backend.name)
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    registry.publish(derivative, backend)

    with pytest.raises(RaglabError) as caught:
        DeletionCoordinator(registry, {same_name.name: same_name}).delete_document(SOURCE_HASH)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert registry.is_tombstoned(SOURCE_HASH)
    assert backend.exists(derivative.object_key)
    receipt = DeletionCoordinator(registry, {backend.name: backend}).delete_document(SOURCE_HASH)
    assert receipt.status is DeletionStatus.DELETED_VERIFIED
    assert not backend.exists(derivative.object_key)


def test_same_name_backend_cannot_publish_into_a_second_store() -> None:
    registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("local")
    same_name = MemoryStorageBackend(backend.name)
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    registry.publish(derivative, backend)
    second = replace(derivative, derivative_id="der_02", object_key="opaque/object/02")

    with pytest.raises(RaglabError) as caught:
        registry.publish(second, same_name)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert registry.derivatives_for(SOURCE_HASH) == (derivative,)
    assert not same_name.exists(second.object_key)


def test_existing_lineage_requires_actual_backend_instance() -> None:
    registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("local")
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    with pytest.raises(TypeError):
        registry.register(derivative)

    registry.register(derivative, backend)
    same_name = MemoryStorageBackend(backend.name)
    with pytest.raises(RaglabError) as caught:
        DeletionCoordinator(registry, {same_name.name: same_name}).delete_document(SOURCE_HASH)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert derivative.object_key not in str(caught.value.to_safe_dict())


@pytest.mark.parametrize("operation", ["register", "publish"])
def test_second_registry_cannot_recreate_object_after_verified_deletion(operation: str) -> None:
    registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("local")
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    registry.publish(derivative, backend)
    coordinator = DeletionCoordinator(registry, {backend.name: backend})
    assert coordinator.delete_document(SOURCE_HASH).status is DeletionStatus.DELETED_VERIFIED

    second_registry = MemoryDerivativeRegistry()
    with pytest.raises(RaglabError) as caught:
        getattr(second_registry, operation)(derivative, backend)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert derivative.object_key not in str(caught.value.to_safe_dict())
    assert second_registry.derivatives_for(SOURCE_HASH) == ()
    assert not backend.exists(derivative.object_key)


@pytest.mark.parametrize("has_lineage", [False, True])
def test_second_registry_cannot_verify_deletion_against_foreign_backend(has_lineage: bool) -> None:
    registry = MemoryDerivativeRegistry()
    second_registry = MemoryDerivativeRegistry()
    backend = MemoryStorageBackend("local")
    second_backend = MemoryStorageBackend(backend.name)
    derivative = Derivative(
        "der_01", SOURCE_HASH, "doc-v1", "gen_01",
        DerivativeKind.ORIGINAL_COPY, backend.name, "opaque/object/01",
    )
    registry.publish(derivative, backend)
    if has_lineage:
        second_registry.register(derivative, second_backend)

    with pytest.raises(RaglabError) as caught:
        DeletionCoordinator(second_registry, {backend.name: backend}).delete_document(SOURCE_HASH)

    assert caught.value.code is ErrorCode.DELETION_VERIFICATION_FAILED
    assert backend.exists(derivative.object_key)
