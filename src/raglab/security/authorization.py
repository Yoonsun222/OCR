from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    document_id: str
    policy_epoch: int
    allowed_principals: frozenset[str]
    group_hashes: Mapping[str, frozenset[str]]

    def allows(self, principal_id: str, group_hash: str) -> bool:
        return principal_id in self.allowed_principals and group_hash in self.group_hashes.get(
            principal_id, ()
        )


@dataclass(frozen=True, slots=True)
class CacheKey:
    principal_id: str
    group_hash: str
    policy_epoch: int
    query_snapshot_id: str
    document_id: str
    document_version: str
    generation_id: str


@dataclass(frozen=True, slots=True)
class QueryPin:
    principal_id: str
    group_hash: str
    policy_epoch: int
    query_snapshot_id: str
    document_id: str
    document_version: str
    generation_id: str

    def cache_key(self) -> CacheKey:
        return CacheKey(
            principal_id=self.principal_id,
            group_hash=self.group_hash,
            policy_epoch=self.policy_epoch,
            query_snapshot_id=self.query_snapshot_id,
            document_id=self.document_id,
            document_version=self.document_version,
            generation_id=self.generation_id,
        )


class MemoryPolicyStore:
    def __init__(self) -> None:
        self.fail_reads = False
        self._epochs: dict[str, int] = {}
        self._grants: dict[str, dict[str, set[str]]] = {}
        self._lock = Lock()

    def allow(self, principal_id: str, document_id: str, *, group_hash: str) -> None:
        with self._lock:
            group_hashes = self._grants.setdefault(document_id, {}).setdefault(principal_id, set())
            if group_hash not in group_hashes:
                self._epochs[document_id] = self._epochs.get(document_id, 0) + 1
                group_hashes.add(group_hash)

    def revoke(self, document_id: str) -> None:
        with self._lock:
            self._epochs[document_id] = self._epochs.get(document_id, 0) + 1
            self._grants.pop(document_id, None)

    def read(self, document_id: str) -> PolicySnapshot:
        with self._lock:
            if self.fail_reads:
                raise OSError("policy store unavailable")
            grants = self._grants.get(document_id, {})
            group_hashes = {principal: frozenset(hashes) for principal, hashes in grants.items()}
            return PolicySnapshot(
                document_id=document_id,
                policy_epoch=self._epochs.get(document_id, 0),
                allowed_principals=frozenset(grants),
                group_hashes=MappingProxyType(group_hashes),
            )


class AuthorizationGuard:
    """Check policy and any injected document serving block at each request boundary."""

    def __init__(
        self,
        store: MemoryPolicyStore,
        *,
        mode: Literal["policy_only", "deletion_managed"],
        serving_blocked: Callable[[str], bool] | None = None,
    ) -> None:
        if mode == "deletion_managed":
            if not callable(serving_blocked):
                raise TypeError("deletion-managed authorization requires a callable serving blocker")
        elif mode != "policy_only" or serving_blocked is not None:
            raise TypeError("policy-only authorization requires policy_only mode without a blocker")
        self._store = store
        self._serving_blocked = serving_blocked

    @classmethod
    def policy_only(cls, store: MemoryPolicyStore) -> "AuthorizationGuard":
        return cls(store, mode="policy_only")

    @classmethod
    def deletion_managed(
        cls, store: MemoryPolicyStore, serving_blocked: Callable[[str], bool]
    ) -> "AuthorizationGuard":
        return cls(store, mode="deletion_managed", serving_blocked=serving_blocked)

    def pin(
        self,
        principal_id: str,
        group_hash: str,
        document_id: str,
        document_version: str,
        generation_id: str,
        diagnostic_id: str | None = None,
    ) -> QueryPin:
        current, blocked = self._read(document_id, "pin", diagnostic_id)
        if blocked or not current.allows(principal_id, group_hash):
            raise RaglabError.policy_denied("pin", diagnostic_id or uuid4().hex)
        return QueryPin(
            principal_id=principal_id,
            group_hash=group_hash,
            policy_epoch=current.policy_epoch,
            query_snapshot_id=str(uuid4()),
            document_id=document_id,
            document_version=document_version,
            generation_id=generation_id,
        )

    def check(self, pin: QueryPin, stage: str, diagnostic_id: str | None = None) -> None:
        current, blocked = self._read(pin.document_id, stage, diagnostic_id)
        if blocked or current.policy_epoch != pin.policy_epoch or not current.allows(
            pin.principal_id, pin.group_hash
        ):
            raise RaglabError(
                ErrorCode.AUTHORIZATION_CHANGED, stage,
                "document serving blocked" if blocked else f"policy epoch {current.policy_epoch}",
                f"policy epoch {pin.policy_epoch}",
                "partial results discarded", RetryAdvice.ONCE_FROM_FRESH_SNAPSHOT,
                "restart authorization once", diagnostic_id or uuid4().hex,
            )

    def _read(
        self, document_id: str, stage: str, diagnostic_id: str | None
    ) -> tuple[PolicySnapshot, bool]:
        try:
            current = self._store.read(document_id)
            blocked = (
                self._serving_blocked(document_id) if self._serving_blocked is not None else False
            )
            if type(blocked) is not bool:
                raise ValueError("invalid serving decision")
            return current, blocked
        except Exception:
            raise RaglabError(
                code=ErrorCode.POLICY_UNAVAILABLE,
                stage=stage,
                observed="policy store unavailable",
                expected="current policy snapshot",
                protected_state="request failed closed",
                retry=RetryAdvice.SAFE,
                next_action="retry when policy is available",
                diagnostic_id=diagnostic_id or uuid4().hex,
            ) from None
