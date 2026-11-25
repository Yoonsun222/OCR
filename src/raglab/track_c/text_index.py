"""Generation-scoped lexical retrieval over authorized document versions."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable
from threading import RLock
from typing import Literal

from raglab.track_c.models import DocumentTarget, IndexRecord, SearchHit
from raglab.track_c.tokenizer import analyze


def _validate_generation(generation_id: str) -> None:
    if type(generation_id) is not str or not generation_id.strip():
        raise ValueError("generation_id must be a non-empty string")


def _validated_records(
    records: Iterable[IndexRecord], generation_id: str,
) -> dict[str, IndexRecord]:
    _validate_generation(generation_id)
    indexed: dict[str, IndexRecord] = {}
    versions: dict[str, str] = {}
    for record in records:
        if not isinstance(record, IndexRecord):
            raise ValueError("records must be IndexRecord values")
        evidence = record.evidence
        if evidence.generation_id != generation_id:
            raise ValueError("all records must belong to the requested generation")
        if evidence.evidence_id in indexed:
            raise ValueError("evidence IDs must be unique inside one generation")
        if versions.setdefault(record.document_id, evidence.document_version) != evidence.document_version:
            raise ValueError("each document must have a single version inside one generation")
        indexed[evidence.evidence_id] = record
    return indexed


def _search_scope(
    tokens: tuple[str, ...],
    allowed_document_ids: frozenset[str],
    generation_id: str,
    limit: int,
    document_targets: tuple[DocumentTarget, ...] | None,
) -> tuple[tuple[str, str | None], ...]:
    _validate_generation(generation_id)
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError("limit must be an integer inside [1, 30]")
    if type(tokens) is not tuple or any(type(token) is not str for token in tokens):
        raise ValueError("tokens must be a tuple of strings")
    if type(allowed_document_ids) is not frozenset or any(
        type(document_id) is not str or not document_id.strip() for document_id in allowed_document_ids
    ):
        raise ValueError("allowed_document_ids must be a frozenset of non-empty strings")
    if document_targets is None:
        return tuple((document_id, None) for document_id in sorted(allowed_document_ids))
    if type(document_targets) is not tuple or any(
        type(target) is not DocumentTarget for target in document_targets
    ):
        raise ValueError("document_targets must be a tuple of DocumentTarget values")
    if len({target.document_id for target in document_targets}) != len(document_targets):
        raise ValueError("document_targets must have unique document IDs")
    return tuple(
        (target.document_id, target.document_version)
        for target in document_targets
        if target.document_id in allowed_document_ids
    )


def _rank(
    candidates: Iterable[IndexRecord], tokens: tuple[str, ...], limit: int,
) -> tuple[SearchHit, ...]:
    wanted = Counter(tokens)
    scored: list[tuple[int, IndexRecord]] = []
    for record in candidates:
        present = Counter(analyze(record.searchable_text))
        matches = sum(min(count, present[token]) for token, count in wanted.items())
        if matches:
            scored.append((matches, record))
    scored.sort(key=lambda item: (-item[0], item[1].evidence.evidence_id))
    return tuple(
        SearchHit(record, matches / len(tokens), rank)
        for rank, (matches, record) in enumerate(scored[:limit], start=1)
    )


class MemoryTextIndex:
    """Reference index keyed by generation and document ID."""

    channel: Literal["text"] = "text"

    def __init__(self) -> None:
        self._generations: dict[str, dict[str, tuple[IndexRecord, ...]]] = {}
        self._lock = RLock()

    def build(self, records: Iterable[IndexRecord], generation_id: str) -> None:
        documents: dict[str, list[IndexRecord]] = {}
        for record in _validated_records(records, generation_id).values():
            documents.setdefault(record.document_id, []).append(record)
        generation = {document_id: tuple(items) for document_id, items in documents.items()}
        with self._lock:
            self._generations[generation_id] = generation

    def search(
        self,
        tokens: tuple[str, ...],
        allowed_document_ids: frozenset[str],
        generation_id: str,
        limit: int,
        *,
        document_targets: tuple[DocumentTarget, ...] | None = None,
    ) -> tuple[SearchHit, ...]:
        scope = _search_scope(tokens, allowed_document_ids, generation_id, limit, document_targets)
        if not tokens or not scope:
            return ()
        with self._lock:
            documents = self._generations.get(generation_id, {})
        candidates = (
            record
            for document_id, version in scope
            for record in documents.get(document_id, ())
            if version is None or version == record.evidence.document_version
        )
        return _rank(candidates, tokens, limit)


class SqliteTextIndex:
    """In-memory SQLite scope index with the same exact scoring as the reference index."""

    channel: Literal["text"] = "text"

    def __init__(self) -> None:
        self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._connection.execute(
            "CREATE TABLE text_records ("
            "generation_id TEXT NOT NULL, document_id TEXT NOT NULL, document_version TEXT NOT NULL, "
            "evidence_id TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, document_id, document_version, evidence_id)"
            ") WITHOUT ROWID"
        )
        self._records: dict[str, dict[str, IndexRecord]] = {}
        self._lock = RLock()

    def build(self, records: Iterable[IndexRecord], generation_id: str) -> None:
        indexed = _validated_records(records, generation_id)
        with self._lock:
            with self._connection:
                self._connection.execute("DELETE FROM text_records WHERE generation_id = ?", (generation_id,))
                self._connection.executemany(
                    "INSERT INTO text_records VALUES (?, ?, ?, ?)",
                    (
                        (generation_id, record.document_id, record.evidence.document_version, evidence_id)
                        for evidence_id, record in indexed.items()
                    ),
                )
            self._records[generation_id] = indexed

    def search(
        self,
        tokens: tuple[str, ...],
        allowed_document_ids: frozenset[str],
        generation_id: str,
        limit: int,
        *,
        document_targets: tuple[DocumentTarget, ...] | None = None,
    ) -> tuple[SearchHit, ...]:
        scope = _search_scope(tokens, allowed_document_ids, generation_id, limit, document_targets)
        if not tokens or not scope:
            return ()
        select = "SELECT evidence_id FROM text_records WHERE generation_id = ? AND document_id = ?"
        candidates = []
        with self._lock:
            indexed = self._records.get(generation_id, {})
            for document_id, version in scope:
                if version is None:
                    rows = self._connection.execute(select, (generation_id, document_id))
                else:
                    rows = self._connection.execute(
                        select + " AND document_version = ?", (generation_id, document_id, version),
                    )
                candidates.extend(indexed[evidence_id] for (evidence_id,) in rows)
        return _rank(candidates, tokens, limit)
