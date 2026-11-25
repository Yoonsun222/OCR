"""Immutable models for snapshot-pinned mixed-document retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, TypeVar

from raglab.contracts.evidence import EvidenceRegionV1
from raglab.indexing.generation import GenerationState, IndexGeneration
from raglab.security.authorization import QueryPin

Channel = Literal["text", "structure", "visual"]
_CHANNELS = ("text", "structure", "visual")
_MAX_EVIDENCE = 3
_T = TypeVar("_T")


def _nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _typed_tuple(value: object, item_type: type[_T], name: str) -> tuple[_T, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{name} must be a sequence of {item_type.__name__}")
    items = tuple(value)
    if any(not isinstance(item, item_type) for item in items):
        raise ValueError(f"{name} must contain only {item_type.__name__} values")
    return items


def _validate_ranked_score(score: float, rank: int) -> None:
    if type(score) not in (int, float) or not 0.0 <= score <= 1.0:
        raise ValueError("score must be a number inside [0, 1]")
    if type(rank) is not int or rank < 1:
        raise ValueError("rank must be a positive integer")


def _validate_evidence_fields(
    evidence: EvidenceRegionV1,
    document_id: str,
    channel: Channel,
    structured_key: str | None,
    structured_value: str | None,
    unit: str | None,
) -> None:
    if not isinstance(evidence, EvidenceRegionV1):
        raise ValueError("evidence must be an EvidenceRegionV1")
    _nonempty_string(document_id, "document_id")
    if not isinstance(channel, str) or channel not in _CHANNELS:
        raise ValueError("channel must be text, structure, or visual")
    for name, value in (
        ("structured_key", structured_key), ("structured_value", structured_value), ("unit", unit),
    ):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be a string or None")


class ResponseState(StrEnum):
    VERIFIED = "VERIFIED"
    ABSTAIN = "ABSTAIN"
    NO_EVIDENCE = "NO_EVIDENCE"
    CONFLICT = "CONFLICT"
    AUTHORIZATION_CHANGED = "AUTHORIZATION_CHANGED"


@dataclass(frozen=True, slots=True)
class DocumentTarget:
    document_id: str
    document_version: str

    def __post_init__(self) -> None:
        _nonempty_string(self.document_id, "document_id")
        _nonempty_string(self.document_version, "document_version")


@dataclass(frozen=True, slots=True)
class MixedQuery:
    text: str
    principal_id: str
    group_hash: str
    targets: tuple[DocumentTarget, ...]
    limit: int = _MAX_EVIDENCE

    def __post_init__(self) -> None:
        _nonempty_string(self.text, "query text")
        _nonempty_string(self.principal_id, "principal_id")
        _nonempty_string(self.group_hash, "group_hash")
        if len(self.group_hash) != 64:
            raise ValueError("group_hash must be 64 characters")
        targets = _typed_tuple(self.targets, DocumentTarget, "targets")
        if not targets:
            raise ValueError("at least one document target is required")
        if len({target.document_id for target in targets}) != len(targets):
            raise ValueError("document target IDs must be unique")
        if type(self.limit) is not int or not 1 <= self.limit <= _MAX_EVIDENCE:
            raise ValueError(f"limit must be an integer from 1 to {_MAX_EVIDENCE}")
        object.__setattr__(self, "targets", targets)


@dataclass(frozen=True, slots=True)
class PinnedMixedQuery:
    request: MixedQuery
    generation: IndexGeneration
    pins: tuple[QueryPin, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, MixedQuery):
            raise ValueError("request must be a MixedQuery")
        if not isinstance(self.generation, IndexGeneration) or (
            self.generation.state is not GenerationState.ACTIVE
        ):
            raise ValueError("generation must be an active IndexGeneration")

        pins = _typed_tuple(self.pins, QueryPin, "pins")
        versions = {target.document_id: target.document_version for target in self.request.targets}
        for pin in pins:
            _nonempty_string(pin.document_id, "pin document_id")
            _nonempty_string(pin.document_version, "pin document_version")
            _nonempty_string(pin.generation_id, "pin generation_id")
            _nonempty_string(pin.principal_id, "pin principal_id")
            _nonempty_string(pin.group_hash, "pin group_hash")
            _nonempty_string(pin.query_snapshot_id, "pin query snapshot ID")
            if type(pin.policy_epoch) is not int or pin.policy_epoch < 0:
                raise ValueError("pin policy epoch must be a non-negative integer")
            expected_version = versions.pop(pin.document_id, None)
            if expected_version is None:
                raise ValueError("pins must cover every document target exactly once")
            if pin.document_version != expected_version:
                raise ValueError("pin document version must match the target")
            if pin.generation_id != self.generation.generation_id:
                raise ValueError("pin generation must match the active generation")
            if pin.principal_id != self.request.principal_id:
                raise ValueError("pin principal must match the request")
            if pin.group_hash != self.request.group_hash:
                raise ValueError("pin group hash must match the request")
        if versions:
            raise ValueError("pins must cover every document target exactly once")
        object.__setattr__(self, "pins", pins)


@dataclass(frozen=True, slots=True)
class IndexRecord:
    evidence: EvidenceRegionV1
    document_id: str
    channel: Channel
    searchable_text: str
    structured_key: str | None = None
    structured_value: str | None = None
    unit: str | None = None
    relation_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_evidence_fields(
            self.evidence, self.document_id, self.channel,
            self.structured_key, self.structured_value, self.unit,
        )
        if not isinstance(self.searchable_text, str):
            raise ValueError("searchable_text must be a string")
        kinds = _typed_tuple(self.relation_kinds, str, "relation_kinds")
        object.__setattr__(self, "relation_kinds", kinds)


@dataclass(frozen=True, slots=True)
class SearchHit:
    record: IndexRecord
    score: float
    rank: int

    def __post_init__(self) -> None:
        if not isinstance(self.record, IndexRecord):
            raise ValueError("record must be an IndexRecord")
        _validate_ranked_score(self.score, self.rank)


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    evidence: EvidenceRegionV1
    document_id: str
    channel: Channel
    score: float
    rank: int
    structured_key: str | None = None
    structured_value: str | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        _validate_evidence_fields(
            self.evidence, self.document_id, self.channel,
            self.structured_key, self.structured_value, self.unit,
        )
        _validate_ranked_score(self.score, self.rank)

    @property
    def generation_id(self) -> str:
        return self.evidence.generation_id


@dataclass(frozen=True, slots=True)
class FusionResult:
    candidates: tuple[RetrievalCandidate, ...]
    has_conflict: bool
    conflict_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        candidates = _typed_tuple(self.candidates, RetrievalCandidate, "candidates")
        if type(self.has_conflict) is not bool:
            raise ValueError("has_conflict must be a boolean")
        keys = _typed_tuple(self.conflict_keys, str, "conflict_keys")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "conflict_keys", keys)


@dataclass(frozen=True, slots=True)
class MixedResponse:
    state: ResponseState
    answer: str
    evidence: tuple[EvidenceRegionV1, ...]
    reason_code: str
    diagnostics: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.state, ResponseState):
            raise ValueError("state must be a ResponseState")
        if not isinstance(self.answer, str):
            raise ValueError("answer must be a string")
        evidence = _typed_tuple(self.evidence, EvidenceRegionV1, "evidence")
        if len(evidence) > _MAX_EVIDENCE:
            raise ValueError(f"evidence must contain at most {_MAX_EVIDENCE} items")
        _nonempty_string(self.reason_code, "reason_code")
        if not isinstance(self.diagnostics, Mapping):
            raise ValueError("diagnostics must be a mapping")
        diagnostics = dict(self.diagnostics)
        if any(not isinstance(key, str) or type(value) is not int for key, value in diagnostics.items()):
            raise ValueError("diagnostics must map string keys to integer values")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "diagnostics", MappingProxyType(diagnostics))
