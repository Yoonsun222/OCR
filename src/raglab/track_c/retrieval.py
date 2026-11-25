"""Coordinate channel searches over the exact authorized query snapshot."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import BoundedSemaphore
from time import monotonic
from typing import Protocol

from raglab.track_c.models import (
    Channel,
    DocumentTarget,
    PinnedMixedQuery,
    RetrievalCandidate,
    SearchHit,
)
from raglab.track_c.tokenizer import analyze

_MAX_ADAPTERS = 3
_CHANNEL_LIMIT = 30
_POOL = ThreadPoolExecutor(max_workers=_MAX_ADAPTERS, thread_name_prefix="track-c")
_SLOTS = BoundedSemaphore(_MAX_ADAPTERS)


class SearchAdapter(Protocol):
    @property
    def channel(self) -> Channel: ...

    def search(
        self,
        tokens: tuple[str, ...],
        allowed_document_ids: frozenset[str],
        generation_id: str,
        limit: int,
        *,
        document_targets: tuple[DocumentTarget, ...],
    ) -> tuple[SearchHit, ...]:
        """Apply the exact document and generation scope before reading or ranking hits."""
        ...


def _search(
    adapter: SearchAdapter,
    tokens: tuple[str, ...],
    allowed: frozenset[str],
    generation_id: str,
    targets: tuple[DocumentTarget, ...],
    deadline: float,
) -> tuple[SearchHit, ...]:
    try:
        if monotonic() >= deadline:
            return ()
        hits = adapter.search(tokens, allowed, generation_id, _CHANNEL_LIMIT, document_targets=targets)
        if monotonic() >= deadline or not isinstance(hits, tuple):
            return ()
        return hits[:_CHANNEL_LIMIT]
    except Exception:
        # Channel exceptions may include backend data and stay inside the worker.
        return ()
    finally:
        # A timed-out search keeps its slot until its worker actually exits.
        _SLOTS.release()


def retrieve(
    query: PinnedMixedQuery,
    adapters: tuple[SearchAdapter, ...],
    deadline_seconds: float = 2.5,
) -> tuple[RetrievalCandidate, ...]:
    if len(adapters) > _MAX_ADAPTERS:
        raise ValueError("at most three retrieval adapters are allowed")
    deadline = monotonic() + deadline_seconds
    tokens = analyze(query.request.text)
    if not tokens:
        return ()
    targets = tuple(DocumentTarget(pin.document_id, pin.document_version) for pin in query.pins)
    versions = {target.document_id: target.document_version for target in targets}
    allowed = frozenset(versions)
    generation_id = query.generation.generation_id

    futures: dict[Future[tuple[SearchHit, ...]], Channel] = {}
    for adapter in adapters:
        channel = adapter.channel
        if channel not in ("text", "structure", "visual"):
            continue
        if monotonic() >= deadline or not _SLOTS.acquire(blocking=False):
            break
        try:
            future = _POOL.submit(_search, adapter, tokens, allowed, generation_id, targets, deadline)
        except RuntimeError:
            _SLOTS.release()
            break
        futures[future] = channel

    done, _ = wait(futures, timeout=max(0.0, deadline - monotonic()))
    candidates: list[RetrievalCandidate] = []
    for future, channel in futures.items():
        if future not in done:
            continue
        for hit in future.result():
            if not isinstance(hit, SearchHit):
                continue
            record = hit.record
            evidence = record.evidence
            if (
                versions.get(record.document_id) != evidence.document_version
                or evidence.generation_id != generation_id
                or record.channel != channel
            ):
                continue
            candidates.append(RetrievalCandidate(
                evidence, record.document_id, channel, hit.score, hit.rank,
                record.structured_key, record.structured_value, record.unit,
            ))
    return tuple(sorted(candidates, key=lambda item: (item.channel, item.rank, item.evidence.evidence_id)))
