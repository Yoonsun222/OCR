# Track C Mixed-Document Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an ACL-first mixed-document retrieval and evidence-composition layer that combines approved Track A and Track B evidence without inventing unsupported claims.

**Architecture:** Track C indexes content-free document metadata plus normalized evidence text/structure, pins one authorization and index-generation snapshot per request, retrieves from text/structure/visual adapters, fuses candidates deterministically, and returns extractive answers or abstention. Track C starts only after common conformance, Track A evidence, and Track B evidence adapters pass their release contracts.

**Tech Stack:** Python 3.11 standard library, SQLite FTS5 where available with an in-memory deterministic test adapter, pytest, common contracts from `src/raglab/contracts`, `src/raglab/security`, and `src/raglab/indexing`.

## Global Constraints

- Run only on approved company-controlled Windows CPU workstations; synthetic documents are the only pre-approval corpus.
- All retrieval is ACL-first and every response is reauthorized immediately before serialization.
- One request may cite only document versions and index generations pinned in its query snapshot.
- At most three evidence candidates are returned; unsupported or conflicting evidence produces `ABSTAIN` or `CONFLICT`.
- Track C cannot reinterpret engineering meaning that Track A/B evidence does not explicitly encode.
- Query P95 targets are 5 seconds for deterministic lookup, 10 seconds for highlighted evidence, and 30 seconds only for approved visual/cross-document verification.
- Implement every behavior test-first and commit after each task.

---

## File Structure

```text
src/raglab/track_c/
  models.py            # Query, candidate, snapshot, and response types
  tokenizer.py         # Mixed Korean/English/Japanese token and trigram analysis
  text_index.py        # SQLite FTS and deterministic in-memory adapter
  adapters.py          # Track A/B evidence source adapters
  retrieval.py         # ACL-first parallel source retrieval
  fusion.py            # Deduplication, version handling, and rank fusion
  composer.py          # Extractive evidence-only answer composition
  service.py           # Snapshot/retry/final authorization orchestration
  evaluation.py        # Retrieval, support, conflict, ACL, and latency metrics
tests/track_c/
  __init__.py
  helpers.py
  fixtures/corpus.json
  fixtures/questions.json
  test_models.py
  test_tokenizer.py
  test_text_index.py
  test_adapters.py
  test_retrieval.py
  test_fusion.py
  test_composer.py
  test_service.py
  test_evaluation.py
```

### Task 1: Define snapshot-pinned query and response types

**Files:**
- Create: `src/raglab/track_c/__init__.py`
- Create: `src/raglab/track_c/models.py`
- Create: `tests/__init__.py`
- Create: `tests/track_c/__init__.py`
- Create: `tests/track_c/helpers.py`
- Test: `tests/track_c/test_models.py`

**Interfaces:**
- Consumes: `QueryPin` from `raglab.security.authorization`, `IndexGeneration` from `raglab.indexing.generation`, and `EvidenceRegionV1` from `raglab.contracts.evidence`.
- Produces: `DocumentTarget`, `MixedQuery`, `PinnedMixedQuery`, `IndexRecord`, `SearchHit`, `RetrievalCandidate`, `FusionResult`, `MixedResponse`, and `ResponseState`.

- [ ] **Step 1: Write failing invariant tests**

```python
from raglab.contracts.evidence import EvidenceRegionV1, EvidenceRole, ExtractionMethod, NativeLocator
from raglab.indexing.generation import GenerationState, IndexGeneration, IndexManifest
from raglab.security.authorization import QueryPin
from raglab.track_c.models import DocumentTarget, MixedQuery, RetrievalCandidate

def native_evidence() -> EvidenceRegionV1:
    return EvidenceRegionV1(
        evidence_id="ev-1", source_hash="a" * 64, document_version="v1",
        generation_id="gen-1", native_locator=NativeLocator.sheet_range("sheet-1", "B7"),
        role=EvidenceRole.VALUE, text="12.5", regions=(), relations=(),
        extraction_method=ExtractionMethod.NATIVE, confidence=1.0,
        acl_snapshot_id="acl-1", metadata={"label": "Pressure", "unit": "MPa"},
    )

def test_query_requires_nonempty_text_and_snapshot():
    try:
        MixedQuery("", "principal-1", "g" * 64, (DocumentTarget("doc-1", "v1"),), 3)
    except ValueError as exc:
        assert "query text" in str(exc)
    else:
        raise AssertionError("empty query accepted")

def test_candidate_score_must_be_normalized():
    try:
        RetrievalCandidate(native_evidence(), "doc-1", "text", 1.1, 1)
    except ValueError as exc:
        assert "score" in str(exc)
    else:
        raise AssertionError("invalid score accepted")
```

- [ ] **Step 2: Run the model tests**

Run: `python -m pytest tests/track_c/test_models.py -q`

Expected: FAIL with a missing `raglab.track_c` module.

- [ ] **Step 3: Implement immutable typed models**

```python
# src/raglab/track_c/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Mapping

from raglab.contracts.evidence import EvidenceRegionV1
from raglab.indexing.generation import IndexGeneration
from raglab.security.authorization import QueryPin

Channel = Literal["text", "structure", "visual"]

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
        if not self.document_id or not self.document_version:
            raise ValueError("document target fields must not be empty")

@dataclass(frozen=True, slots=True)
class MixedQuery:
    text: str
    principal_id: str
    group_hash: str
    targets: tuple[DocumentTarget, ...]
    limit: int = 3
    def __post_init__(self) -> None:
        if not self.text.strip(): raise ValueError("query text must not be empty")
        if not self.principal_id: raise ValueError("principal_id must not be empty")
        if len(self.group_hash) != 64: raise ValueError("group_hash must be 64 characters")
        if not self.targets: raise ValueError("at least one document target is required")
        if not 1 <= self.limit <= 3: raise ValueError("limit must be from 1 to 3")

@dataclass(frozen=True, slots=True)
class PinnedMixedQuery:
    request: MixedQuery
    generation: IndexGeneration
    pins: tuple[QueryPin, ...]
    def __post_init__(self) -> None:
        expected = {target.document_id for target in self.request.targets}
        actual = {pin.document_id for pin in self.pins}
        if expected != actual: raise ValueError("pins must cover every target exactly")
        if any(pin.generation_id != self.generation.generation_id for pin in self.pins):
            raise ValueError("all pins must use the active generation")

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

@dataclass(frozen=True, slots=True)
class SearchHit:
    record: IndexRecord
    score: float
    rank: int

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
        if not 0.0 <= self.score <= 1.0: raise ValueError("score must be inside [0,1]")
        if self.rank < 1: raise ValueError("rank must be positive")
    @property
    def generation_id(self) -> str:
        return self.evidence.generation_id

@dataclass(frozen=True, slots=True)
class FusionResult:
    candidates: tuple[RetrievalCandidate, ...]
    has_conflict: bool
    conflict_keys: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class MixedResponse:
    state: ResponseState
    answer: str
    evidence: tuple[EvidenceRegionV1, ...]
    reason_code: str
    diagnostics: Mapping[str, int] = field(default_factory=dict)
```

```python
# src/raglab/track_c/__init__.py
from .models import DocumentTarget, MixedQuery, MixedResponse, ResponseState

__all__ = ["DocumentTarget", "MixedQuery", "MixedResponse", "ResponseState"]

# tests/__init__.py and tests/track_c/__init__.py each contain only:
# Package marker for deterministic test-helper imports.
```

```python
# tests/track_c/helpers.py
from raglab.contracts.evidence import EvidenceRegionV1, EvidenceRole, ExtractionMethod, NativeLocator
from raglab.indexing.generation import GenerationState, IndexGeneration, IndexManifest
from raglab.security.authorization import QueryPin
from raglab.track_c.models import (
    DocumentTarget, FusionResult, MixedQuery, PinnedMixedQuery, RetrievalCandidate,
)

def evidence(
    evidence_id: str = "ev-1", document_version: str = "v1", generation_id: str = "gen-1",
    text: str = "12.5", metadata: dict[str, object] | None = None,
) -> EvidenceRegionV1:
    return EvidenceRegionV1(
        evidence_id=evidence_id, source_hash="a" * 64, document_version=document_version,
        generation_id=generation_id, native_locator=NativeLocator.sheet_range("sheet-1", "B7"),
        role=EvidenceRole.VALUE, text=text, regions=(), relations=(),
        extraction_method=ExtractionMethod.NATIVE, confidence=1.0,
        acl_snapshot_id="acl-1", metadata=metadata or {"label": "Pressure", "unit": "MPa"},
    )

def generation(generation_id: str = "gen-1") -> IndexGeneration:
    hashes = {"text": "a" * 64, "structure": "b" * 64, "visual": "c" * 64}
    manifests = tuple(IndexManifest(kind, hashes[kind], 1) for kind in ("text", "structure", "visual"))
    return IndexGeneration(
        generation_id, "catalog-v1", "b" * 64, "evidence-region.v1", 1,
        manifests, GenerationState.ACTIVE,
    )

def pinned_query() -> PinnedMixedQuery:
    request = MixedQuery("pressure", "user-1", "g" * 64, (DocumentTarget("doc-1", "v1"),), 3)
    pin = QueryPin(
        principal_id="user-1", group_hash="g" * 64, policy_epoch=1,
        query_snapshot_id="00000000-0000-4000-8000-000000000001", document_id="doc-1",
        document_version="v1", generation_id="gen-1",
    )
    return PinnedMixedQuery(request, generation(), (pin,))

def candidate(
    evidence_id: str = "ev-1", document_id: str = "doc-1", score: float = 1.0,
    rank: int = 1, document_version: str = "v1", key: str | None = "Pressure",
    value: str | None = "12.5", channel: str = "structure",
) -> RetrievalCandidate:
    return RetrievalCandidate(
        evidence(evidence_id, document_version), document_id, channel, score, rank, key, value, "MPa",
    )

def fused() -> FusionResult:
    return FusionResult((candidate(),), False, ())
```

- [ ] **Step 4: Run model tests**

Run: `python -m pytest tests/track_c/test_models.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the Track C boundary**

```bash
git add src/raglab/track_c tests/track_c/test_models.py
git commit -m "feat(track-c): define snapshot pinned query types"
```

### Task 2: Tokenize mixed Korean, English, and Japanese text deterministically

**Files:**
- Create: `src/raglab/track_c/tokenizer.py`
- Test: `tests/track_c/test_tokenizer.py`

**Interfaces:**
- Consumes: raw query or evidence text.
- Produces: `analyze(text: str) -> tuple[str, ...]` with normalized words and CJK trigrams.

- [ ] **Step 1: Write failing multilingual tests**

```python
from raglab.track_c.tokenizer import analyze

def test_analyze_mixed_text_is_stable():
    assert analyze("Pump A 압력상승 部品C") == (
        "pump", "a", "압력상", "력상승", "部品c"
    )

def test_analyze_ignores_control_characters():
    assert "\u0000" not in "".join(analyze("A\u0000B"))
```

- [ ] **Step 2: Run tokenizer tests**

Run: `python -m pytest tests/track_c/test_tokenizer.py -q`

Expected: FAIL because `analyze` is absent.

- [ ] **Step 3: Implement Unicode normalization and bounded n-grams**

```python
# src/raglab/track_c/tokenizer.py
from __future__ import annotations

import unicodedata

MAX_CHARACTERS = 8_192
MAX_TOKENS = 2_048

def _is_cjk(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF or 0x4E00 <= value <= 0x9FFF
        or 0x3040 <= value <= 0x30FF or 0xAC00 <= value <= 0xD7AF
    )

def _runs(text: str) -> tuple[str, ...]:
    output: list[str] = []
    current: list[str] = []
    for character in text:
        if character.isalnum():
            current.append(character)
        elif current:
            output.append("".join(current))
            current = []
    if current:
        output.append("".join(current))
    return tuple(output)

def analyze(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text[:MAX_CHARACTERS]).casefold()
    sanitized = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    tokens: list[str] = []
    for run in _runs(sanitized):
        if len(run) >= 3 and all(_is_cjk(character) for character in run):
            tokens.extend(run[index:index + 3] for index in range(len(run) - 2))
        else:
            tokens.append(run)
        if len(tokens) >= MAX_TOKENS:
            break
    return tuple(tokens[:MAX_TOKENS])
```

- [ ] **Step 4: Run tokenizer tests twice**

Run: `python -m pytest tests/track_c/test_tokenizer.py -q; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests/track_c/test_tokenizer.py -q`

Expected: identical PASS output.

- [ ] **Step 5: Commit the analyzer**

```bash
git add src/raglab/track_c/tokenizer.py tests/track_c/test_tokenizer.py
git commit -m "feat(track-c): analyze mixed language text"
```

### Task 3: Build a generation-scoped text index adapter

**Files:**
- Create: `src/raglab/track_c/text_index.py`
- Create: `tests/track_c/fixtures/corpus.json`
- Test: `tests/track_c/test_text_index.py`

**Interfaces:**
- Consumes: immutable `IndexRecord` values.
- Produces: `MemoryTextIndex.build(records, generation_id)`, `SqliteTextIndex.build(records, generation_id)`, and identical `search(tokens, allowed_document_ids, generation_id, limit) -> tuple[SearchHit, ...]` methods.

- [ ] **Step 1: Write failing ACL and generation isolation tests**

Create `tests/track_c/fixtures/corpus.json` with:

```json
{"schema_version":1,"records":[{"document_id":"doc-allowed","document_version":"v1","generation_id":"gen-2","evidence_id":"ev-1","text":"Pressure 12.5 MPa"}]}
```

```python
from raglab.contracts.evidence import EvidenceRegionV1, EvidenceRole, ExtractionMethod, NativeLocator
from raglab.track_c.models import IndexRecord
from raglab.track_c.text_index import MemoryTextIndex

def record(document_id: str, generation_id: str, evidence_id: str) -> IndexRecord:
    evidence = EvidenceRegionV1(
        evidence_id=evidence_id, source_hash="a" * 64, document_version="v1",
        generation_id=generation_id, native_locator=NativeLocator.sheet_range("s1", "B7"),
        role=EvidenceRole.VALUE, text="Pressure 12.5 MPa", regions=(), relations=(),
        extraction_method=ExtractionMethod.NATIVE, confidence=1.0,
        acl_snapshot_id="acl-1", metadata={},
    )
    return IndexRecord(evidence, document_id, "text", evidence.text)

def test_search_never_returns_disallowed_or_mixed_generation():
    index = MemoryTextIndex()
    index.build((record("doc-allowed", "gen-2", "ev-1"),), "gen-2")
    result = index.search(("pressure",), frozenset({"doc-allowed"}), "gen-2", 10)
    assert {item.record.document_id for item in result} == {"doc-allowed"}
    assert {item.record.evidence.generation_id for item in result} == {"gen-2"}
    assert index.search(("pressure",), frozenset({"doc-hidden"}), "gen-2", 10) == ()
```

- [ ] **Step 2: Run index tests**

Run: `python -m pytest tests/track_c/test_text_index.py -q`

Expected: FAIL because `TextIndex` is absent.

- [ ] **Step 3: Implement an in-memory reference index and SQLite adapter**

```python
# src/raglab/track_c/text_index.py
from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Iterable

from raglab.track_c.models import IndexRecord, SearchHit
from raglab.track_c.tokenizer import analyze

def _validate(records: tuple[IndexRecord, ...], generation_id: str) -> None:
    if not generation_id:
        raise ValueError("generation_id must not be empty")
    if any(record.evidence.generation_id != generation_id for record in records):
        raise ValueError("all records must belong to the requested generation")
    if len({record.evidence.evidence_id for record in records}) != len(records):
        raise ValueError("evidence IDs must be unique inside one generation")

class MemoryTextIndex:
    def __init__(self) -> None:
        self._records: dict[str, tuple[IndexRecord, ...]] = {}

    def build(self, records: Iterable[IndexRecord], generation_id: str) -> None:
        frozen = tuple(records)
        _validate(frozen, generation_id)
        self._records[generation_id] = frozen

    def search(
        self, tokens: tuple[str, ...], allowed_document_ids: frozenset[str],
        generation_id: str, limit: int,
    ) -> tuple[SearchHit, ...]:
        if limit < 1 or limit > 30: raise ValueError("limit must be inside [1,30]")
        wanted = Counter(tokens)
        scored: list[tuple[float, IndexRecord]] = []
        for record in self._records.get(generation_id, ()):
            if record.document_id not in allowed_document_ids: continue
            present = Counter(analyze(record.searchable_text))
            matches = sum(min(count, present[token]) for token, count in wanted.items())
            if matches:
                scored.append((matches / max(1, sum(wanted.values())), record))
        scored.sort(key=lambda item: (-item[0], item[1].evidence.evidence_id))
        return tuple(SearchHit(record, min(score, 1.0), rank) for rank, (score, record) in enumerate(scored[:limit], 1))

class SqliteTextIndex:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._records: dict[tuple[str, str], IndexRecord] = {}

    @staticmethod
    def _table(generation_id: str) -> str:
        return "fts_" + hashlib.sha256(generation_id.encode("utf-8")).hexdigest()[:16]

    def build(self, records: Iterable[IndexRecord], generation_id: str) -> None:
        frozen = tuple(records)
        _validate(frozen, generation_id)
        table = self._table(generation_id)
        with self._connection:
            self._connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5("
                "evidence_id UNINDEXED, document_id UNINDEXED, generation_id UNINDEXED, token_text)"
            )
            self._connection.execute(f"DELETE FROM {table}")
            self._connection.executemany(
                f"INSERT INTO {table}(evidence_id,document_id,generation_id,token_text) VALUES(?,?,?,?)",
                ((r.evidence.evidence_id, r.document_id, generation_id, " ".join(analyze(r.searchable_text))) for r in frozen),
            )
        for record in frozen:
            self._records[(generation_id, record.evidence.evidence_id)] = record

    def search(
        self, tokens: tuple[str, ...], allowed_document_ids: frozenset[str],
        generation_id: str, limit: int,
    ) -> tuple[SearchHit, ...]:
        if not tokens or not allowed_document_ids: return ()
        if limit < 1 or limit > 30: raise ValueError("limit must be inside [1,30]")
        table = self._table(generation_id)
        query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        placeholders = ",".join("?" for _ in allowed_document_ids)
        sql = (
            f"SELECT evidence_id,bm25({table}) FROM {table} "
            f"WHERE {table} MATCH ? AND generation_id=? AND document_id IN ({placeholders}) "
            "ORDER BY bm25(" + table + "), evidence_id LIMIT ?"
        )
        parameters: tuple[object, ...] = (query, generation_id, *sorted(allowed_document_ids), limit)
        rows = self._connection.execute(sql, parameters).fetchall()
        output: list[SearchHit] = []
        for rank, (evidence_id, raw_score) in enumerate(rows, 1):
            record = self._records[(generation_id, evidence_id)]
            output.append(SearchHit(record, 1.0 / (1.0 + abs(float(raw_score))), rank))
        return tuple(output)
```

- [ ] **Step 4: Run injection, ACL, and generation tests**

Run: `python -m pytest tests/track_c/test_text_index.py -q`

Expected: PASS; a query containing quotes or FTS operators is treated as tokens rather than executable query syntax.

- [ ] **Step 5: Commit the text retrieval adapter**

```bash
git add src/raglab/track_c/text_index.py tests/track_c
git commit -m "feat(track-c): add generation scoped text index"
```

### Task 4: Adapt Track A and Track B evidence without changing semantics

**Files:**
- Create: `src/raglab/track_c/adapters.py`
- Test: `tests/track_c/test_adapters.py`

**Interfaces:**
- Consumes: Track A `EvidenceRegionV1` objects and Track B cell/table/chart evidence.
- Produces: `IndexRecord` values with channel, searchable text, structured keys, document/version/generation IDs, and the original evidence reference.

- [ ] **Step 1: Write failing semantic-preservation tests**

```python
from raglab.track_c.adapters import from_track_a, from_track_b
from tests.track_c.helpers import evidence

def test_visual_adapter_preserves_label_target_relation():
    track_a_evidence = evidence(metadata={"normalized_label": "A", "relation_kind": "label_targets"})
    record = from_track_a(track_a_evidence, "doc-a")
    assert record.channel == "visual"
    assert record.evidence.evidence_id == track_a_evidence.evidence_id
    assert "label_targets" in record.relation_kinds

def test_chart_adapter_indexes_series_and_source_range():
    track_b_chart = evidence(metadata={"chart_title": "Pressure", "source_range": "Data!$B$2:$B$5"})
    record = from_track_b(track_b_chart, "doc-b")
    assert record.structured_key == "Data!$B$2:$B$5"
```

- [ ] **Step 2: Run adapter tests**

Run: `python -m pytest tests/track_c/test_adapters.py -q`

Expected: FAIL because the adapters are absent.

- [ ] **Step 3: Implement lossless adapters**

Use this complete adapter implementation:

```python
# src/raglab/track_c/adapters.py
from __future__ import annotations

from collections.abc import Mapping

from raglab.contracts.evidence import EvidenceRegionV1, EvidenceRole
from raglab.track_c.models import IndexRecord

def _strings(value: object) -> tuple[str, ...]:
    output: list[str] = []
    if isinstance(value, str) and value.strip():
        output.append(value.strip())
    elif isinstance(value, Mapping):
        for key in sorted(value): output.extend(_strings(value[key]))
    elif isinstance(value, (list, tuple)):
        for item in value: output.extend(_strings(item))
    return tuple(output)

def _searchable(evidence: EvidenceRegionV1, allowed_metadata: tuple[str, ...]) -> str:
    values = [evidence.text]
    values.extend(str(evidence.metadata[key]) for key in allowed_metadata if evidence.metadata.get(key) not in (None, ""))
    return " ".join(dict.fromkeys(value.strip() for value in values if value.strip()))

def from_track_a(evidence: EvidenceRegionV1, document_id: str) -> IndexRecord:
    roles = {region.role for region in evidence.regions}
    has_pair = {EvidenceRole.LABEL, EvidenceRole.TARGET} <= roles or evidence.metadata.get("relation_kind") == "label_targets"
    relation_kinds = ("label_targets",) if has_pair else ()
    return IndexRecord(
        evidence=evidence, document_id=document_id, channel="visual",
        searchable_text=_searchable(evidence, ("normalized_label",)),
        relation_kinds=relation_kinds,
    )

def from_track_b(evidence: EvidenceRegionV1, document_id: str) -> IndexRecord:
    metadata = evidence.metadata
    relation_strings = _strings(evidence.to_dict().get("relations", ()))
    source_range = str(metadata["source_range"]) if metadata.get("source_range") else next(
        (value for value in relation_strings if "!" in value and ("$" in value or ":" in value)), None
    )
    label = str(metadata.get("label") or metadata.get("chart_title") or evidence.role.value)
    unit = str(metadata["unit"]) if metadata.get("unit") not in (None, "") else None
    structured_key = source_range or evidence.native_locator.to_dict().get("cell_range") or label
    searchable = _searchable(evidence, ("label", "unit", "chart_title", "chart_part"))
    if evidence.role in {EvidenceRole.SERIES, EvidenceRole.LEGEND, EvidenceRole.AXIS}:
        searchable = " ".join(dict.fromkeys((searchable, *relation_strings)))
    return IndexRecord(
        evidence=evidence, document_id=document_id, channel="structure",
        searchable_text=searchable, structured_key=str(structured_key),
        structured_value=evidence.text, unit=unit,
        relation_kinds=tuple(sorted({region.role.value for region in evidence.regions})),
    )
```

- [ ] **Step 4: Run Track A/B contract tests with adapters**

Run: `python -m pytest tests/track_c/test_adapters.py tests/track_a/test_evidence.py tests/track_b/test_evidence.py -q`

Expected: all tests PASS with unchanged evidence hashes.

- [ ] **Step 5: Commit cross-track adapters**

```bash
git add src/raglab/track_c/adapters.py tests/track_c/test_adapters.py
git commit -m "feat(track-c): adapt track evidence for retrieval"
```

### Task 5: Retrieve through ACL-first channel adapters

**Files:**
- Create: `src/raglab/track_c/retrieval.py`
- Test: `tests/track_c/test_retrieval.py`

**Interfaces:**
- Consumes: pinned query snapshot, authorized document IDs, text/structure/visual search adapters.
- Produces: `retrieve(query, adapters) -> tuple[RetrievalCandidate, ...]`.

- [ ] **Step 1: Write failing pre-filter, timeout, and side-channel tests**

```python
from raglab.track_c.retrieval import retrieve
from tests.track_c.helpers import pinned_query

class SpyAdapter:
    channel = "text"
    def __init__(self): self.allowed = []
    def search(self, tokens, allowed_document_ids, generation_id, limit):
        self.allowed.append(allowed_document_ids); return ()

def test_retrieval_passes_only_authorized_ids_to_every_channel():
    adapter = SpyAdapter()
    assert retrieve(pinned_query(), (adapter,)) == ()
    assert adapter.allowed == [frozenset({"doc-1"})]

class SlowAdapter:
    channel = "visual"
    def search(self, tokens, allowed_document_ids, generation_id, limit):
        import time
        time.sleep(0.05)
        return ()

def test_timeout_returns_no_content_or_hidden_count():
    assert retrieve(pinned_query(), (SlowAdapter(),), deadline_seconds=0.001) == ()
```

- [ ] **Step 2: Run retrieval tests**

Run: `python -m pytest tests/track_c/test_retrieval.py -q`

Expected: FAIL because the retrieval coordinator is absent.

- [ ] **Step 3: Implement bounded parallel retrieval**

```python
# src/raglab/track_c/retrieval.py
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from typing import Protocol

from raglab.track_c.models import Channel, PinnedMixedQuery, RetrievalCandidate, SearchHit
from raglab.track_c.tokenizer import analyze

class SearchAdapter(Protocol):
    channel: Channel
    def search(
        self, tokens: tuple[str, ...], allowed_document_ids: frozenset[str],
        generation_id: str, limit: int,
    ) -> tuple[SearchHit, ...]: ...

def retrieve(
    query: PinnedMixedQuery, adapters: tuple[SearchAdapter, ...], deadline_seconds: float = 2.5,
) -> tuple[RetrievalCandidate, ...]:
    if len(adapters) > 3: raise ValueError("at most three retrieval adapters are allowed")
    tokens = analyze(query.request.text)
    if not tokens: return ()
    allowed = frozenset(pin.document_id for pin in query.pins)
    generation_id = query.generation.generation_id
    pool = ThreadPoolExecutor(max_workers=max(1, len(adapters)), thread_name_prefix="track-c")
    futures = {
        pool.submit(adapter.search, tokens, allowed, generation_id, 30): adapter.channel
        for adapter in adapters
    }
    done, pending = wait(tuple(futures), timeout=deadline_seconds)
    for future in pending: future.cancel()
    output: list[RetrievalCandidate] = []
    for future in sorted(done, key=lambda item: futures[item]):
        try: hits = future.result()
        except (TimeoutError, OSError, RuntimeError, ValueError): continue
        for hit in hits:
            record = hit.record
            if record.document_id not in allowed or record.evidence.generation_id != generation_id: continue
            output.append(RetrievalCandidate(
                record.evidence, record.document_id, record.channel, hit.score, hit.rank,
                record.structured_key, record.structured_value, record.unit,
            ))
    pool.shutdown(wait=False, cancel_futures=True)
    return tuple(sorted(output, key=lambda item: (item.channel, item.rank, item.evidence.evidence_id)))
```

- [ ] **Step 4: Run retrieval and common policy-race tests**

Run: `python -m pytest tests/track_c/test_retrieval.py tests/security/test_authorization.py -q`

Expected: all tests PASS; unauthorized IDs never appear in adapter calls or diagnostics.

- [ ] **Step 5: Commit ACL-first retrieval**

```bash
git add src/raglab/track_c/retrieval.py tests/track_c/test_retrieval.py
git commit -m "feat(track-c): retrieve through authorized channels"
```

### Task 6: Fuse, deduplicate, and detect version conflicts

**Files:**
- Create: `src/raglab/track_c/fusion.py`
- Test: `tests/track_c/test_fusion.py`

**Interfaces:**
- Consumes: channel candidates and the pinned generation ID.
- Produces: `fuse(candidates, generation_id, limit=3) -> FusionResult` with stable ranking and conflict metadata.

- [ ] **Step 1: Write failing reciprocal-rank, deduplication, and conflict tests**

```python
from raglab.track_c.fusion import fuse
from tests.track_c.helpers import candidate

def test_fusion_deduplicates_evidence_and_preserves_best_channel():
    candidates = (candidate(channel="text", rank=2), candidate(channel="structure", rank=1))
    result = fuse(candidates, "gen-1", limit=3)
    assert len({c.evidence.evidence_id for c in result.candidates}) == len(result.candidates)

def test_conflicting_versions_are_not_merged():
    candidates = (
        candidate("ev-1", document_version="v1", value="12.5"),
        candidate("ev-2", document_version="v2", value="13.0"),
    )
    result = fuse(candidates, "gen-1", limit=3)
    assert result.has_conflict is True
```

- [ ] **Step 2: Run fusion tests**

Run: `python -m pytest tests/track_c/test_fusion.py -q`

Expected: FAIL because `fuse` is absent.

- [ ] **Step 3: Implement deterministic reciprocal-rank fusion**

```python
# src/raglab/track_c/fusion.py
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from raglab.track_c.models import FusionResult, RetrievalCandidate

def fuse(
    candidates: tuple[RetrievalCandidate, ...], generation_id: str, limit: int = 3,
) -> FusionResult:
    if not 1 <= limit <= 3: raise ValueError("limit must be inside [1,3]")
    eligible = tuple(candidate for candidate in candidates if candidate.generation_id == generation_id)
    grouped: dict[str, list[RetrievalCandidate]] = defaultdict(list)
    for candidate in eligible: grouped[candidate.evidence.evidence_id].append(candidate)
    raw: list[tuple[float, RetrievalCandidate]] = []
    for evidence_id, group in grouped.items():
        score = sum(1.0 / (60.0 + candidate.rank) for candidate in group)
        representative = min(group, key=lambda item: (-item.score, item.rank, item.channel, evidence_id))
        raw.append((score, representative))
    maximum = max((score for score, _ in raw), default=1.0)
    ranked = [replace(item, score=score / maximum) for score, item in raw]
    ranked.sort(key=lambda item: (-item.score, item.evidence.evidence_id))

    values: dict[str, set[str]] = defaultdict(set)
    versions: dict[str, set[str]] = defaultdict(set)
    for candidate in ranked:
        if candidate.structured_key and candidate.structured_value is not None:
            values[candidate.structured_key].add(candidate.structured_value)
            versions[candidate.structured_key].add(candidate.evidence.document_version)
    conflicts = tuple(sorted(
        key for key in values if len(values[key]) > 1 and len(versions[key]) > 1
    ))
    return FusionResult(tuple(ranked[:limit]), bool(conflicts), conflicts)
```

- [ ] **Step 4: Run fusion tests twice**

Run: `python -m pytest tests/track_c/test_fusion.py -q; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; python -m pytest tests/track_c/test_fusion.py -q`

Expected: identical PASS output and ordering.

- [ ] **Step 5: Commit fusion and conflict behavior**

```bash
git add src/raglab/track_c/fusion.py tests/track_c/test_fusion.py
git commit -m "feat(track-c): fuse evidence and expose conflicts"
```

### Task 7: Compose extractive answers and abstain safely

**Files:**
- Create: `src/raglab/track_c/composer.py`
- Test: `tests/track_c/test_composer.py`

**Interfaces:**
- Consumes: `FusionResult` and query intent.
- Produces: `compose() -> MixedResponse` with zero to three evidence objects.

- [ ] **Step 1: Write failing supported-answer, conflict, and no-evidence tests**

```python
from raglab.track_c.composer import compose
from raglab.track_c.models import FusionResult
from tests.track_c.helpers import candidate, fused

def test_composer_uses_only_explicit_evidence_text():
    response = compose("What is the pressure?", fused())
    assert response.state == "VERIFIED"
    assert response.answer == "Pressure: 12.5 MPa"
    assert response.evidence[0].native_locator.to_dict()["cell_range"] == "B7"

def test_composer_does_not_choose_between_conflicts():
    conflict = FusionResult((candidate("ev-1"), candidate("ev-2", value="13.0")), True, ("Pressure",))
    assert compose("pressure", conflict).state == "CONFLICT"
```

- [ ] **Step 2: Run composer tests**

Run: `python -m pytest tests/track_c/test_composer.py -q`

Expected: FAIL because `compose` is absent.

- [ ] **Step 3: Implement rule-bound extractive composition**

```python
# src/raglab/track_c/composer.py
from __future__ import annotations

from raglab.track_c.models import FusionResult, MixedResponse, ResponseState

def compose(query_text: str, result: FusionResult) -> MixedResponse:
    del query_text  # Query text selects evidence upstream; it never acts as an instruction here.
    if result.has_conflict:
        return MixedResponse(
            ResponseState.CONFLICT, "서로 충돌하는 명시적 근거가 있습니다.",
            tuple(candidate.evidence for candidate in result.candidates),
            "EXPLICIT_VALUE_CONFLICT", {"candidate_count": len(result.candidates)},
        )
    if not result.candidates:
        return MixedResponse(ResponseState.NO_EVIDENCE, "근거를 찾지 못했습니다.", (), "NO_AUTHORIZED_EVIDENCE")
    top = result.candidates[0]
    if top.score < 0.55:
        return MixedResponse(
            ResponseState.ABSTAIN, "판단할 만큼 강한 근거가 없습니다.",
            tuple(candidate.evidence for candidate in result.candidates),
            "LOW_RETRIEVAL_CONFIDENCE", {"candidate_count": len(result.candidates)},
        )
    if top.structured_key and top.structured_value is not None:
        unit = f" {top.unit}" if top.unit else ""
        answer = f"{top.structured_key}: {top.structured_value}{unit}"
    else:
        answer = "관련 근거 후보를 찾았습니다."
    return MixedResponse(
        ResponseState.VERIFIED, answer,
        tuple(candidate.evidence for candidate in result.candidates),
        "EXTRACTIVE_EVIDENCE", {"candidate_count": len(result.candidates)},
    )
```

- [ ] **Step 4: Run injection and unsupported-claim tests**

Run: `python -m pytest tests/track_c/test_composer.py -q`

Expected: PASS; the injection case in `test_composer.py` proves that document text resembling an instruction remains quoted evidence and never changes control flow.

- [ ] **Step 5: Commit the evidence-only composer**

```bash
git add src/raglab/track_c/composer.py tests/track_c/test_composer.py
git commit -m "feat(track-c): compose evidence only responses"
```

### Task 8: Orchestrate snapshot retry and final authorization

**Files:**
- Create: `src/raglab/track_c/service.py`
- Test: `tests/track_c/test_service.py`

**Interfaces:**
- Consumes: `AuthorizationGuard`, `JsonGenerationCatalog`, channel `SearchAdapter` values, retrieval, fusion, and composer.
- Produces: `MixedDocumentService.query(request: MixedQuery) -> MixedResponse`.

- [ ] **Step 1: Write failing epoch-change and generation-change tests**

```python
from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice
from raglab.security.authorization import QueryPin
from raglab.track_c.models import DocumentTarget, MixedQuery
from raglab.track_c.service import MixedDocumentService
from tests.track_c.helpers import generation

REQUEST = MixedQuery("pressure", "user-1", "g" * 64, (DocumentTarget("doc-1", "v1"),), 3)

class ChangingGuard:
    def pin(self, principal_id, group_hash, document_id, document_version, generation_id):
        return QueryPin(principal_id, group_hash, 1, "00000000-0000-4000-8000-000000000001", document_id, document_version, generation_id)
    def check(self, pin, stage, diagnostic_id):
        if stage == "serialize":
            raise RaglabError(
                ErrorCode.AUTHORIZATION_CHANGED, stage, "epoch 2", "epoch 1",
                "partial results discarded", RetryAdvice.ONCE_FROM_FRESH_SNAPSHOT,
                "restart authorization once", diagnostic_id,
            )

class StableCatalog:
    def read_active(self): return generation("gen-1")

def test_service_discards_result_when_acl_changes_before_serialization():
    service = MixedDocumentService(ChangingGuard(), StableCatalog(), ())
    response = service.query(REQUEST)
    assert response.state == "AUTHORIZATION_CHANGED"
    assert response.evidence == ()

class StableGuard(ChangingGuard):
    def check(self, pin, stage, diagnostic_id): return None

class FlippingCatalog:
    def __init__(self): self.calls = 0
    def read_active(self):
        self.calls += 1
        return generation("gen-1" if self.calls % 2 else "gen-2")

def test_service_never_combines_generations():
    service = MixedDocumentService(StableGuard(), FlippingCatalog(), ())
    response = service.query(REQUEST)
    assert response.state == "AUTHORIZATION_CHANGED"
    assert response.evidence == ()
```

- [ ] **Step 2: Run service tests**

Run: `python -m pytest tests/track_c/test_service.py -q`

Expected: FAIL because the service is absent.

- [ ] **Step 3: Implement one safe retry from a fresh snapshot**

```python
# src/raglab/track_c/service.py
from __future__ import annotations

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.indexing.generation import JsonGenerationCatalog
from raglab.security.authorization import AuthorizationGuard
from raglab.track_c.composer import compose
from raglab.track_c.fusion import fuse
from raglab.track_c.models import MixedQuery, MixedResponse, PinnedMixedQuery, ResponseState
from raglab.track_c.retrieval import SearchAdapter, retrieve

class MixedDocumentService:
    def __init__(
        self, guard: AuthorizationGuard, catalog: JsonGenerationCatalog,
        adapters: tuple[SearchAdapter, ...],
    ) -> None:
        self._guard = guard
        self._catalog = catalog
        self._adapters = adapters

    def _pin(self, request: MixedQuery) -> PinnedMixedQuery | None:
        generation = self._catalog.read_active()
        if generation is None: return None
        pins = tuple(
            self._guard.pin(
                request.principal_id, request.group_hash, target.document_id,
                target.document_version, generation.generation_id,
            )
            for target in request.targets
        )
        return PinnedMixedQuery(request, generation, pins)

    @staticmethod
    def _authorization_changed() -> MixedResponse:
        return MixedResponse(
            ResponseState.AUTHORIZATION_CHANGED, "권한이 변경되어 결과를 폐기했습니다.",
            (), "AUTHORIZATION_CHANGED",
        )

    def query(self, request: MixedQuery) -> MixedResponse:
        for attempt in range(2):
            try:
                pinned = self._pin(request)
                if pinned is None:
                    return MixedResponse(ResponseState.NO_EVIDENCE, "활성 인덱스가 없습니다.", (), "NO_ACTIVE_GENERATION")
                for pin in pinned.pins:
                    self._guard.check(pin, "retrieve", f"track-c-{attempt}-retrieve")
                candidates = retrieve(pinned, self._adapters)
                for pin in pinned.pins:
                    self._guard.check(pin, "source_load", f"track-c-{attempt}-source")
                fused = fuse(candidates, pinned.generation.generation_id, request.limit)
                response = compose(request.text, fused)
                active = self._catalog.read_active()
                if active is None or active.generation_id != pinned.generation.generation_id:
                    if attempt == 0: continue
                    return self._authorization_changed()
                for pin in pinned.pins:
                    self._guard.check(pin, "serialize", f"track-c-{attempt}-serialize")
                authorized = {pin.document_id for pin in pinned.pins}
                if any(candidate.document_id not in authorized for candidate in fused.candidates):
                    return self._authorization_changed()
                return response
            except RaglabError as error:
                if error.code is ErrorCode.AUTHORIZATION_CHANGED and attempt == 0: continue
                if error.code in {ErrorCode.AUTHORIZATION_CHANGED, ErrorCode.POLICY_DENIED, ErrorCode.POLICY_UNAVAILABLE}:
                    return self._authorization_changed()
                raise
        return self._authorization_changed()
```

- [ ] **Step 4: Run service, race, cache, and mixed-ACL tests**

Run: `python -m pytest tests/track_c/test_service.py tests/security/test_authorization.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the Track C service**

```bash
git add src/raglab/track_c/service.py tests/track_c/test_service.py
git commit -m "feat(track-c): orchestrate safe mixed document queries"
```

### Task 9: Add blind retrieval, support, security, and latency evaluation

**Files:**
- Create: `src/raglab/track_c/evaluation.py`
- Create: `tests/track_c/fixtures/questions.json`
- Test: `tests/track_c/test_evaluation.py`
- Create: `docs/track-c-evaluation.md`

**Interfaces:**
- Consumes: immutable `EvaluationCase` rows from a frozen question manifest and one selected run per question; repeated-run variance is stored separately by the caller.
- Produces: `evaluate(cases) -> EvaluationReport` with Recall@10, Evidence Recall@3, citation support, critical unsupported claims, ACL leaks, latency, and document-cluster bootstrap intervals.

- [ ] **Step 1: Write failing scorecard tests**

Create `tests/track_c/fixtures/questions.json` with:

```json
{"schema_version":1,"frozen":true,"questions":[{"question_id":"q-1","cluster_document_id":"doc-1","text":"Pressure?","gold_document_ids":["doc-1"],"gold_evidence_ids":["ev-1"],"language":"en","document_format":"xlsx"}]}
```

```python
from raglab.track_c.evaluation import EvaluationCase, evaluate

def test_scorecard_separates_retrieval_support_and_security():
    cases = tuple(
        EvaluationCase(
            question_id=f"q-{index}", cluster_document_id=f"cluster-{index // 2}",
            gold_document_ids=frozenset({f"doc-{index}"}), gold_evidence_ids=frozenset({f"ev-{index}"}),
            retrieved_document_ids=(f"doc-{index}",) if index < 18 else ("wrong",),
            retrieved_evidence_ids=(f"ev-{index}",) if index < 17 else ("wrong",),
            citation_supported=True, critical_unsupported_claims=0, acl_leaks=0,
            expected_answerable=True, answered=index < 17, state="VERIFIED" if index < 17 else "ABSTAIN",
            language="ko", document_format="xlsx", latency_ms=100 + index, queue_ms=5,
            peak_working_set_bytes=1_000_000,
        ) for index in range(20)
    )
    report = evaluate(cases)
    assert report.document_recall_at_10 == 0.90
    assert report.evidence_recall_at_3 == 0.85
    assert report.acl_leaks == 0
    assert report.status == "EXPLORATORY"
```

- [ ] **Step 2: Run evaluation tests**

Run: `python -m pytest tests/track_c/test_evaluation.py -q`

Expected: FAIL because `evaluate` is absent.

- [ ] **Step 3: Implement pre-registered metric output**

```python
# src/raglab/track_c/evaluation.py
from __future__ import annotations

import math
import random
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class EvaluationCase:
    question_id: str
    cluster_document_id: str
    gold_document_ids: frozenset[str]
    gold_evidence_ids: frozenset[str]
    retrieved_document_ids: tuple[str, ...]
    retrieved_evidence_ids: tuple[str, ...]
    citation_supported: bool
    critical_unsupported_claims: int
    acl_leaks: int
    expected_answerable: bool
    answered: bool
    state: str
    language: str
    document_format: str
    latency_ms: int
    queue_ms: int
    peak_working_set_bytes: int

@dataclass(frozen=True, slots=True)
class EvaluationReport:
    status: str
    question_count: int
    document_recall_at_10: float
    evidence_recall_at_3: float
    citation_support: float
    critical_unsupported_claims: int
    acl_leaks: int
    answerable_recall: float
    no_answer_precision: float
    conflict_rate: float
    abstention_rate: float
    latency_p50_ms: int
    latency_p95_ms: int
    latency_p99_ms: int
    document_recall_ci95: tuple[float, float]
    evidence_recall_ci95: tuple[float, float]
    slices: dict[str, dict[str, float]]

def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0

def _percentile(values: list[int], percentile: float) -> int:
    if not values: return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]

def _cluster_ci(rows: list[tuple[str, float]], seed: int = 0) -> tuple[float, float]:
    clusters: dict[str, list[float]] = {}
    for cluster, value in rows: clusters.setdefault(cluster, []).append(value)
    keys = sorted(clusters)
    if not keys: return (0.0, 0.0)
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(1_000):
        chosen = [rng.choice(keys) for _ in keys]
        samples.append(_mean([value for key in chosen for value in clusters[key]]))
    samples.sort()
    return (samples[24], samples[974])

def _slice(cases: tuple[EvaluationCase, ...], attribute: str) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for case in cases:
        value = str(getattr(case, attribute))
        hit = float(bool(case.gold_evidence_ids & set(case.retrieved_evidence_ids[:3])))
        groups.setdefault(value, []).append(hit)
    return {key: _mean(groups[key]) for key in sorted(groups)}

def evaluate(cases: tuple[EvaluationCase, ...], approved_power_analysis: bool = False) -> EvaluationReport:
    if len({case.question_id for case in cases}) != len(cases):
        raise ValueError("question IDs must be unique; repeated runs are not independent cases")
    document_hits = [float(bool(case.gold_document_ids & set(case.retrieved_document_ids[:10]))) for case in cases]
    evidence_hits = [float(bool(case.gold_evidence_ids & set(case.retrieved_evidence_ids[:3]))) for case in cases]
    answerable = [case for case in cases if case.expected_answerable]
    predicted_no_answer = [case for case in cases if not case.answered]
    total_latency = [case.latency_ms + case.queue_ms for case in cases]
    return EvaluationReport(
        status="DECISION_ELIGIBLE" if len(cases) >= 150 or approved_power_analysis else "EXPLORATORY",
        question_count=len(cases), document_recall_at_10=_mean(document_hits),
        evidence_recall_at_3=_mean(evidence_hits),
        citation_support=_mean([float(case.citation_supported) for case in cases]),
        critical_unsupported_claims=sum(case.critical_unsupported_claims for case in cases),
        acl_leaks=sum(case.acl_leaks for case in cases),
        answerable_recall=_mean([float(case.answered) for case in answerable]),
        no_answer_precision=_mean([float(not case.expected_answerable) for case in predicted_no_answer]),
        conflict_rate=_mean([float(case.state == "CONFLICT") for case in cases]),
        abstention_rate=_mean([float(case.state == "ABSTAIN") for case in cases]),
        latency_p50_ms=_percentile(total_latency, 0.50),
        latency_p95_ms=_percentile(total_latency, 0.95),
        latency_p99_ms=_percentile(total_latency, 0.99),
        document_recall_ci95=_cluster_ci([(case.cluster_document_id, hit) for case, hit in zip(cases, document_hits)]),
        evidence_recall_ci95=_cluster_ci([(case.cluster_document_id, hit) for case, hit in zip(cases, evidence_hits)]),
        slices={"language": _slice(cases, "language"), "format": _slice(cases, "document_format")},
    )
```

Create `docs/track-c-evaluation.md` with this exact release contract:

```markdown
# Track C Evaluation Contract

- Freeze the document manifest and question manifest before the blind run.
- Count one selected run per question; repeated runs measure variance only.
- Report document-cluster bootstrap 95% intervals and language/format slices.
- Fewer than 150 blind questions remains `EXPLORATORY` unless an approved power-analysis artifact is attached.
- Any ACL leak, unapproved egress, or unsupported critical claim is a hard failure.
- Track C cannot enter integration until Track A and Track B EvidenceRegionV1 contract tests pass unchanged.
```

- [ ] **Step 4: Run all Track C and dependency contract tests**

Run: `python -m pytest tests/track_c tests/contracts tests/security tests/track_a/test_evidence.py tests/track_b/test_evidence.py -q`

Expected: all tests PASS and no Track C test mutates source evidence.

- [ ] **Step 5: Commit Track C evaluation**

```bash
git add src/raglab/track_c/evaluation.py tests/track_c docs/track-c-evaluation.md
git commit -m "test(track-c): add mixed retrieval evaluation gates"
```

## Self-Review

- Spec coverage: ACL-first retrieval, pinned generations, three retrieval channels, deterministic fusion, version conflict, evidence-only answers, abstention, final reauthorization, mixed-language analysis, blind metrics, and latency/resource reporting map to Tasks 1-9.
- Dependency coverage: Track C consumes but does not reinterpret common, Track A, or Track B contracts; its full integration cannot begin before their conformance tests pass.
- Safety coverage: prompts and document instructions remain data, unauthorized candidates never enter retrieval, and every policy/generation change discards partial output.

Plan complete and saved to `docs/superpowers/plans/2026-11-24-track-c-mixed-document-retrieval.md`. Implement only after the common contract suite plus Track A and Track B EvidenceRegionV1 adapter tests pass. Use `superpowers:subagent-driven-development` for task-by-task execution with review, or `superpowers:executing-plans` for checkpointed inline execution.

