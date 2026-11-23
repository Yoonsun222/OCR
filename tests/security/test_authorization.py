from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore


def test_guard_requires_explicit_mode() -> None:
    with pytest.raises(TypeError):
        AuthorizationGuard(MemoryPolicyStore())


@pytest.mark.parametrize("blocker", [None, False])
def test_deletion_managed_guard_requires_a_callable_blocker(blocker: object) -> None:
    with pytest.raises(TypeError):
        AuthorizationGuard.deletion_managed(MemoryPolicyStore(), blocker)


def test_deletion_managed_mode_rejects_missing_blocker() -> None:
    with pytest.raises(TypeError, match="callable"):
        AuthorizationGuard(MemoryPolicyStore(), mode="deletion_managed")


def test_epoch_change_discards_result_at_every_boundary() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    guard = AuthorizationGuard.policy_only(store)
    pin = guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    stages = ("retrieve", "source_load", "model", "serialize", "highlight_open")
    for stage in stages:
        guard.check(pin, stage)

    store.revoke("doc_01")
    for stage in stages:
        diagnostic_id = f"diag_{stage}"
        with pytest.raises(RaglabError) as raised:
            guard.check(pin, stage, diagnostic_id=diagnostic_id)
        assert raised.value.to_safe_dict() == {
            "code": ErrorCode.AUTHORIZATION_CHANGED.value,
            "stage": stage,
            "observed": f"policy epoch {pin.policy_epoch + 1}",
            "expected": f"policy epoch {pin.policy_epoch}",
            "protected_state": "partial results discarded",
            "retry": RetryAdvice.ONCE_FROM_FRESH_SNAPSHOT.value,
            "next_action": "restart authorization once",
            "diagnostic_id": diagnostic_id,
        }


def test_cache_key_changes_with_each_pinned_dimension() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    guard = AuthorizationGuard.policy_only(store)
    first = guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    assert UUID(str(first.query_snapshot_id)).version == 4

    for field, value in (
        ("principal_id", "user_02"),
        ("group_hash", "h" * 64),
        ("policy_epoch", first.policy_epoch + 1),
        ("query_snapshot_id", uuid4()),
        ("document_id", "doc_02"),
        ("document_version", "doc-v2"),
        ("generation_id", "gen_02"),
    ):
        assert replace(first, **{field: value}).cache_key() != first.cache_key()

    repeated = guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    assert repeated.cache_key() != first.cache_key()
    store.revoke("doc_01")
    store.allow("user_01", "doc_01", group_hash="h" * 64)
    second = guard.pin("user_01", "h" * 64, "doc_01", "doc-v1", "gen_02")
    assert second.policy_epoch > first.policy_epoch
    assert second.cache_key() != first.cache_key()


def test_policy_store_failure_is_fail_closed() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    guard = AuthorizationGuard.policy_only(store)
    pin = guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    store.fail_reads = True

    with pytest.raises(RaglabError) as raised:
        guard.pin("user_01", "g" * 64, "doc_01", "doc-v1", "gen_01")
    assert raised.value.code is ErrorCode.POLICY_UNAVAILABLE
    assert raised.value.retry is RetryAdvice.SAFE
    assert raised.value.protected_state == "request failed closed"

    with pytest.raises(RaglabError) as raised:
        guard.check(pin, "source_load", diagnostic_id="diag_01")
    assert raised.value.code is ErrorCode.POLICY_UNAVAILABLE
    assert raised.value.retry is RetryAdvice.SAFE
    assert raised.value.protected_state == "request failed closed"
    assert raised.value.stage == "source_load"
    assert raised.value.diagnostic_id == "diag_01"


def test_denied_principal_group_pair_cannot_discover_document_existence() -> None:
    store = MemoryPolicyStore()
    store.allow("user_01", "doc_01", group_hash="g" * 64)
    store.allow("user_02", "doc_01", group_hash="h" * 64)
    guard = AuthorizationGuard.policy_only(store)
    errors = []

    for document_id in ("doc_01", "doc_02"):
        with pytest.raises(RaglabError) as raised:
            guard.pin(
                "user_01", "h" * 64, document_id, "doc-v1", "gen_01",
                diagnostic_id="diag_01",
            )
        assert raised.value.code is ErrorCode.POLICY_DENIED
        errors.append(raised.value.to_safe_dict())

    assert errors[0] == errors[1]
