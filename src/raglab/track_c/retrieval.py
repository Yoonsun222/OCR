"""Coordinate channel searches over the exact authorized query snapshot."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import BoundedSemaphore
from time import monotonic
from typing import Protocol

from raglab.contracts.evidence import EvidenceRegionV1
from raglab.track_c.models import (
    Channel,
    DocumentTarget,
    IndexRecord,
    PinnedMixedQuery,
    RetrievalCandidate,
    SearchHit,
)
from raglab.track_c.tokenizer import analyze

_MAX_ADAPTERS = 3
_CHANNEL_LIMIT = 30
_MAX_DEADLINE_SECONDS = 60
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
    versions: dict[str, str],
    generation_id: str,
    targets: tuple[DocumentTarget, ...],
    deadline: float,
) -> tuple[RetrievalCandidate, ...]:
    try:
        if monotonic() >= deadline:
            return ()
        channel = adapter.channel
        if (
            type(channel) is not str
            or channel not in ("text", "structure", "visual")
            or monotonic() >= deadline
        ):
            return ()
        hits = adapter.search(
            tokens, frozenset(versions), generation_id, _CHANNEL_LIMIT, document_targets=targets,
        )
        if monotonic() >= deadline or type(hits) is not tuple:
            return ()

        candidates: list[RetrievalCandidate] = []
        for hit in hits[:_CHANNEL_LIMIT]:
            if type(hit) is not SearchHit or type(hit.record) is not IndexRecord:
                continue
            record = hit.record
            evidence = record.evidence
            if type(evidence) is not EvidenceRegionV1:
                continue
            if (
                type(record.document_id) is not str
                or type(record.channel) is not str
                or type(evidence.evidence_id) is not str
                or type(evidence.document_version) is not str
                or type(evidence.generation_id) is not str
                or versions.get(record.document_id) != evidence.document_version
                or evidence.generation_id != generation_id
                or record.channel != channel
            ):
                continue
            candidates.append(RetrievalCandidate(
                evidence, record.document_id, channel, hit.score, hit.rank,
                record.structured_key, record.structured_value, record.unit,
            ))
        return tuple(candidates) if monotonic() < deadline else ()
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
    """Return candidates from searches completed within a budget of up to 60 seconds."""
    if type(deadline_seconds) not in (int, float) or not 0 < deadline_seconds <= _MAX_DEADLINE_SECONDS:
        raise ValueError(f"deadline_seconds must be in (0, {_MAX_DEADLINE_SECONDS}]")
    if len(adapters) > _MAX_ADAPTERS:
        raise ValueError("at most three retrieval adapters are allowed")
    deadline = monotonic() + deadline_seconds
    tokens = analyze(query.request.text)
    if not tokens:
        return ()
    targets = tuple(DocumentTarget(pin.document_id, pin.document_version) for pin in query.pins)
    versions = {target.document_id: target.document_version for target in targets}
    generation_id = query.generation.generation_id

    futures: list[Future[tuple[RetrievalCandidate, ...]]] = []
    for adapter in adapters:
        if monotonic() >= deadline or not _SLOTS.acquire(blocking=False):
            break
        try:
            future = _POOL.submit(_search, adapter, tokens, versions, generation_id, targets, deadline)
        except RuntimeError:
            _SLOTS.release()
            break
        futures.append(future)

    done, pending = wait(futures, timeout=max(0.0, deadline - monotonic()))
    # Platform waits can return a fraction early. Keep waiting until the same deadline.
    while pending and (remaining := deadline - monotonic()) > 0:
        completed, pending = wait(pending, timeout=remaining)
        done.update(completed)
    candidates = (candidate for future in futures if future in done for candidate in future.result())
    return tuple(sorted(candidates, key=lambda item: (item.channel, item.rank, item.evidence.evidence_id)))
