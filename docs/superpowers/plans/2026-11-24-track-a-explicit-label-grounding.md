# Track A Explicit Label Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find an explicitly printed Korean, English, or Japanese part label in a drawing and return at most three reproducible label-and-target evidence candidates on Windows CPU.

**Architecture:** Provider protocols supply recorded OCR tokens and synthetic SVG target regions. Pure deterministic normalization, matching, and spatial ranking feed the common `EvidenceRegionV1` envelope, whose rendered geometry lives in nested `EvidenceRegion` values; `GroundingService` uses the common `QueryPin` and `AuthorizationGuard` at every authorization boundary.

**Tech Stack:** Python 3.11 standard library, pytest 8.3.5, UTF-8 Tesseract TSV, SVG/XML, common contracts in `src/raglab/contracts/evidence.py`, authorization in `src/raglab/security/authorization.py`.

## Global Constraints

- Use synthetic fixtures only until company approval; do not open a socket or execute OCR/model binaries in this plan.
- L16 process working set is <=12 GiB; L32 is <=26 GiB.
- Return at most three candidates or the typed state `ABSTAIN`/`PART_ABSENT`.
- Bind highlights to source, document version, generation, artifact hash, render profile, and transform.
- Cover visible labels only; engineering-function inference, CAD/BIM, and safety judgment are excluded.
- Keep common tests in `tests/contracts`, authorization tests in `tests/security`, and Track A tests in `tests/track_a`.
- Develop test-first and commit after each task.

---

## File Structure

```text
src/raglab/track_a/{__init__,models,providers,tesseract_tsv,svg_regions}.py
src/raglab/track_a/{normalization,matcher,ranker,evidence,service,evaluation}.py
tests/track_a/fixtures/{mixed_labels.tsv,parts_ko_en_ja.svg,evaluation_cases.json}
tests/track_a/test_{models,providers,matcher,ranker,evidence,service,evaluation}.py
docs/track-a-evaluation.md
```

### Task 1: Define immutable models, providers, and complete synthetic fixtures

**Files:**
- Create: `src/raglab/track_a/__init__.py`
- Create: `src/raglab/track_a/models.py`
- Create: `src/raglab/track_a/providers.py`
- Create: `src/raglab/track_a/tesseract_tsv.py`
- Create: `src/raglab/track_a/svg_regions.py`
- Create: `tests/track_a/fixtures/mixed_labels.tsv`
- Create: `tests/track_a/fixtures/parts_ko_en_ja.svg`
- Test: `tests/track_a/test_models.py`
- Test: `tests/track_a/test_providers.py`

**Interfaces:**
- Consumes: `EvidenceRegionV1`, `NativeLocator`, `QueryPin`.
- Produces: `OcrToken`, `TargetRegion`, `GroundingCandidate`, `ArtifactProvenance`, `GroundingRequest`, `GroundingResponse`, `OcrProvider`, `GeometryProvider`, `parse_tsv`, `read_svg_regions`.

- [ ] **Step 1: Write failing tests and full fixtures**

```tsv
level	page_num	block_num	par_num	line_num	word_num	left	top	width	height	conf	text
5	1	1	1	1	1	100	100	80	20	96	A-12
5	1	1	1	2	1	410	100	90	20	94	부품B
5	1	1	1	3	1	710	100	90	20	92	部品C
4	1	1	1	4	0	0	0	1000	500	-1	
```

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 500">
  <rect data-region-id="part-a" data-kind="part" x="200" y="100" width="180" height="180" />
  <rect data-region-id="part-b" data-kind="part" x="500" y="100" width="180" height="180" />
  <rect data-region-id="part-c" data-kind="part" x="800" y="100" width="150" height="180" />
</svg>
```

```python
# tests/track_a/test_models.py
import pytest
from raglab.track_a.models import OcrToken, TargetRegion

def test_models_reject_invalid_geometry() -> None:
    with pytest.raises(ValueError, match="normalized box"):
        OcrToken("A", "en", (0.9, 0.2, 0.2, 0.1), 1.0)
    with pytest.raises(ValueError, match="positive area"):
        TargetRegion("r", ((0.2, 0.2),) * 3, "part")
```

```python
# tests/track_a/test_providers.py
from pathlib import Path
from raglab.track_a.svg_regions import read_svg_regions
from raglab.track_a.tesseract_tsv import parse_tsv

def test_synthetic_inputs_normalize_coordinates() -> None:
    tsv = Path("tests/track_a/fixtures/mixed_labels.tsv").read_text(encoding="utf-8")
    tokens = parse_tsv(tsv, 1000, 500)
    regions = read_svg_regions("tests/track_a/fixtures/parts_ko_en_ja.svg")
    assert [item.text for item in tokens] == ["A-12", "부품B", "部品C"]
    assert tokens[0].box == (0.1, 0.2, 0.08, 0.04)
    assert [item.region_id for item in regions] == ["part-a", "part-b", "part-c"]
    assert regions[0].polygon[0] == (0.2, 0.2)
```

- [ ] **Step 2: Run tests and verify missing-module failures**

Run: `py -3.11 -m pytest tests/track_a/test_models.py tests/track_a/test_providers.py -v`

Expected: collection fails because `raglab.track_a` is absent.

- [ ] **Step 3: Implement complete models and providers**

```python
# src/raglab/track_a/models.py
from dataclasses import dataclass
from typing import Literal
from raglab.contracts.evidence import EvidenceRegionV1, NativeLocator
from raglab.security.authorization import QueryPin

Box = tuple[float, float, float, float]
Point = tuple[float, float]

def validate_box(box: Box) -> None:
    x, y, w, h = box
    if min(x, y) < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
        raise ValueError("normalized box must have positive area inside [0,1]")

@dataclass(frozen=True, slots=True)
class OcrToken:
    text: str; language: str; box: Box; confidence: float
    def __post_init__(self) -> None:
        validate_box(self.box)
        if not self.text.strip() or not 0 <= self.confidence <= 1:
            raise ValueError("OCR text and confidence are invalid")

@dataclass(frozen=True, slots=True)
class TargetRegion:
    region_id: str; polygon: tuple[Point, ...]; kind: str
    def __post_init__(self) -> None:
        if len(self.polygon) < 3:
            raise ValueError("target polygon must have positive area")
        xs, ys = zip(*self.polygon)
        if min(xs) < 0 or min(ys) < 0 or max(xs) > 1 or max(ys) > 1 or max(xs) <= min(xs) or max(ys) <= min(ys):
            raise ValueError("target polygon must have positive area inside [0,1]")

@dataclass(frozen=True, slots=True)
class PartQuery: raw: str; label: str
@dataclass(frozen=True, slots=True)
class GroundingCandidate: token: OcrToken; target: TargetRegion; score: float

@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    source_hash: str; document_version: str; generation_id: str
    artifact_id: str; artifact_hash: str; artifact_mime_type: str
    renderer_name: str; renderer_version: str; render_profile_hash: str
    canvas_width_px: int; canvas_height_px: int; dpi: int; page_rotation: int; page_box: str
    transform_chain: tuple[tuple[float, ...], ...]
    native_locator: NativeLocator; acl_snapshot_id: str

@dataclass(frozen=True, slots=True)
class GroundingRequest:
    question: str; artifact_path: str; provenance: ArtifactProvenance; pin: QueryPin

@dataclass(frozen=True, slots=True)
class GroundingResponse:
    state: Literal["VERIFIED", "PART_ABSENT", "ABSTAIN"]
    evidence: tuple[EvidenceRegionV1, ...]
    reason: str
```

```python
# src/raglab/track_a/providers.py
from typing import Protocol
from .models import OcrToken, TargetRegion
class OcrProvider(Protocol):
    def read(self, artifact_path: str) -> tuple[OcrToken, ...]:
        raise NotImplementedError
class GeometryProvider(Protocol):
    def read(self, artifact_path: str) -> tuple[TargetRegion, ...]:
        raise NotImplementedError
```

```python
# src/raglab/track_a/tesseract_tsv.py
import csv, io
from .models import OcrToken
def parse_tsv(text: str, width: int, height: int) -> tuple[OcrToken, ...]:
    if width <= 0 or height <= 0:
        raise ValueError("artifact dimensions must be positive")
    result: list[OcrToken] = []
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        confidence, value = float(row["conf"]), row["text"].strip()
        if confidence < 0 or not value:
            continue
        box = (int(row["left"])/width, int(row["top"])/height,
               int(row["width"])/width, int(row["height"])/height)
        result.append(OcrToken(value, "und", box, confidence/100.0))
    return tuple(result)
```

```python
# src/raglab/track_a/svg_regions.py
from xml.etree import ElementTree as ET
from .models import TargetRegion
def read_svg_regions(path: str) -> tuple[TargetRegion, ...]:
    root = ET.parse(path).getroot()
    ox, oy, vw, vh = map(float, root.attrib["viewBox"].split())
    if ox != 0 or oy != 0 or vw <= 0 or vh <= 0:
        raise ValueError("SVG viewBox is unsupported")
    result: list[TargetRegion] = []
    for node in root.iter():
        rid = node.attrib.get("data-region-id")
        if not rid or node.tag.rsplit("}", 1)[-1] != "rect":
            continue
        x, y, w, h = (float(node.attrib[key]) for key in ("x", "y", "width", "height"))
        polygon = ((x/vw,y/vh),((x+w)/vw,y/vh),((x+w)/vw,(y+h)/vh),(x/vw,(y+h)/vh))
        result.append(TargetRegion(rid, polygon, node.attrib.get("data-kind", "unknown")))
    return tuple(sorted(result, key=lambda item: item.region_id))
```

Create `src/raglab/track_a/__init__.py` containing `"""Explicit-label grounding track."""`.

- [ ] **Step 4: Run focused tests**

Run: `py -3.11 -m pytest tests/track_a/test_models.py tests/track_a/test_providers.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```powershell
git add src/raglab/track_a tests/track_a
git commit -m "feat(track-a): add synthetic grounding inputs"
```

### Task 2: Normalize, parse, and exactly match multilingual labels

**Files:**
- Create: `src/raglab/track_a/normalization.py`
- Create: `src/raglab/track_a/matcher.py`
- Test: `tests/track_a/test_matcher.py`

**Interfaces:**
- Consumes: raw question and `OcrToken` tuple.
- Produces: `normalize_label`, `parse_part_query`, `matching_tokens`.

- [ ] **Step 1: Write failing tests**

```python
# tests/track_a/test_matcher.py
import pytest
from raglab.track_a.matcher import matching_tokens, parse_part_query
from raglab.track_a.models import OcrToken
from raglab.track_a.normalization import normalize_label

def test_multilingual_templates_and_exact_match() -> None:
    assert normalize_label(" Ａ－１２ ") == "a-12"
    assert parse_part_query("A-12는 어디에 있어?").label == "a-12"
    assert parse_part_query("Where is A-12?").label == "a-12"
    assert parse_part_query("部品Cはどこですか？").label == "部品c"
    tokens = (OcrToken("Ａ－１２", "en", (0.1,0.1,0.1,0.05),0.9),
              OcrToken("A-120", "en", (0.3,0.1,0.1,0.05),0.9))
    assert matching_tokens(parse_part_query("Where is A-12?"), tokens) == (tokens[0],)

def test_free_form_inference_is_rejected() -> None:
    with pytest.raises(ValueError, match="explicit-label"):
        parse_part_query("Find the part that controls pressure")
```

- [ ] **Step 2: Run tests and verify missing modules**

Run: `py -3.11 -m pytest tests/track_a/test_matcher.py -v`

Expected: collection fails because matcher modules are absent.

- [ ] **Step 3: Implement full matcher**

```python
# src/raglab/track_a/normalization.py
import re, unicodedata
def normalize_label(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("−", "-").replace("‐", "-")
    return re.sub(r"\s+", "", value).strip("?？.,")
```

```python
# src/raglab/track_a/matcher.py
import re
from .models import OcrToken, PartQuery
from .normalization import normalize_label
_PATTERNS = (
    re.compile(r"^\s*(?P<label>[^\s?？]{1,64})\s*는\s*어디에\s*있어\s*[?？]?\s*$", re.I),
    re.compile(r"^\s*where\s+is\s+(?P<label>[^\s?]{1,64})\s*\?\s*$", re.I),
    re.compile(r"^\s*(?P<label>[^\s?？]{1,64})\s*は\s*どこですか\s*[?？]?\s*$", re.I),
)
def parse_part_query(raw: str) -> PartQuery:
    for pattern in _PATTERNS:
        match = pattern.fullmatch(raw)
        if match:
            label = normalize_label(match.group("label"))
            if label:
                return PartQuery(raw, label)
    raise ValueError("question must use a supported explicit-label template")
def matching_tokens(query: PartQuery, tokens: tuple[OcrToken, ...]) -> tuple[OcrToken, ...]:
    return tuple(item for item in tokens if normalize_label(item.text) == query.label)
```

- [ ] **Step 4: Run tests**

Run: `py -3.11 -m pytest tests/track_a/test_matcher.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```powershell
git add src/raglab/track_a/normalization.py src/raglab/track_a/matcher.py tests/track_a/test_matcher.py
git commit -m "feat(track-a): match explicit multilingual labels"
```

### Task 3: Rank label-to-target candidates deterministically

**Files:**
- Create: `src/raglab/track_a/ranker.py`
- Test: `tests/track_a/test_ranker.py`

**Interfaces:**
- Consumes: matching tokens and targets.
- Produces: `rank_candidates(tokens: tuple[OcrToken, ...], targets: tuple[TargetRegion, ...], limit: int = 3) -> tuple[GroundingCandidate, ...]`.

- [ ] **Step 1: Write complete failing tests**

```python
# tests/track_a/test_ranker.py
import pytest
from raglab.track_a.models import OcrToken, TargetRegion
from raglab.track_a.ranker import rank_candidates
def rect(name: str, x: float) -> TargetRegion:
    return TargetRegion(name, ((x,.2),(x+.1,.2),(x+.1,.4),(x,.4)), "part")
def test_distance_order_and_limit() -> None:
    token = OcrToken("A", "en", (.1,.25,.08,.04), .98)
    ranked = rank_candidates((token,), (rect("far",.7),rect("near",.2),rect("x",.85),rect("middle",.45)),3)
    assert [item.target.region_id for item in ranked] == ["near","middle","far"]
    assert ranked[0].score > ranked[1].score > ranked[2].score
def test_limit_is_one_to_three() -> None:
    with pytest.raises(ValueError, match="limit"):
        rank_candidates((), (), 4)
```

- [ ] **Step 2: Run tests and verify missing module**

Run: `py -3.11 -m pytest tests/track_a/test_ranker.py -v`

Expected: collection fails because ranker is absent.

- [ ] **Step 3: Implement full ranker**

```python
# src/raglab/track_a/ranker.py
import math
from .models import GroundingCandidate, OcrToken, TargetRegion
def _center_box(box):
    x,y,w,h=box; return x+w/2,y+h/2
def _center_polygon(polygon):
    return sum(p[0] for p in polygon)/len(polygon), sum(p[1] for p in polygon)/len(polygon)
def _contains(point, polygon):
    x,y=point; inside=False; previous=polygon[-1]
    for current in polygon:
        x1,y1=previous; x2,y2=current
        if (y1>y)!=(y2>y) and x < (x2-x1)*(y-y1)/(y2-y1)+x1:
            inside=not inside
        previous=current
    return inside
def rank_candidates(tokens: tuple[OcrToken,...], targets: tuple[TargetRegion,...], limit: int=3) -> tuple[GroundingCandidate,...]:
    if not 1 <= limit <= 3: raise ValueError("limit must be between one and three")
    result=[]
    for token in tokens:
        label=_center_box(token.box)
        for target in targets:
            center=_center_polygon(target.polygon)
            distance=math.hypot(label[0]-center[0],label[1]-center[1])
            score=min(1.0,.65*token.confidence+.35*max(0.0,1-distance/.5)+(.1 if _contains(label,target.polygon) else 0))
            result.append(GroundingCandidate(token,target,score))
    return tuple(sorted(result,key=lambda item:(-item.score,item.target.region_id,item.token.text))[:limit])
```

- [ ] **Step 4: Run twice**

Run: `py -3.11 -m pytest tests/track_a/test_ranker.py -v; py -3.11 -m pytest tests/track_a/test_ranker.py -v`

Expected: both runs report `2 passed` with identical order.

- [ ] **Step 5: Commit**

```powershell
git add src/raglab/track_a/ranker.py tests/track_a/test_ranker.py
git commit -m "feat(track-a): rank label target regions"
```

### Task 4: Convert candidates into the common EvidenceRegionV1 envelope

**Files:**
- Create: `src/raglab/track_a/evidence.py`
- Test: `tests/track_a/test_evidence.py`

**Interfaces:**
- Consumes: candidate, provenance, `EvidenceRegionV1`, nested `EvidenceRegion`, `EvidenceRole`, `ExtractionMethod`, `EvidenceRelation`.
- Produces: `candidate_to_evidence`.

- [ ] **Step 1: Write a failing paired-region test**

```python
# tests/track_a/test_evidence.py
from raglab.contracts.evidence import EvidenceRole, NativeLocator
from raglab.track_a.evidence import candidate_to_evidence
from raglab.track_a.models import *
def test_paired_regions_and_relation() -> None:
    candidate=GroundingCandidate(OcrToken("A-12","en",(.1,.2,.1,.05),.96),
        TargetRegion("part-a",((.2,.2),(.4,.2),(.4,.5),(.2,.5)),"part"),.88)
    provenance=ArtifactProvenance("b"*64,"doc-v1","gen-01","artifact-01","a"*64,"image/png",
        "synthetic","1.0","c"*64,1000,500,144,0,"crop",
        ((1.,0.,0.,0.,1.,0.,0.,0.,1.),),NativeLocator.page(1),"acl-01")
    evidence=candidate_to_evidence(candidate,provenance)
    assert [r.role for r in evidence.regions] == [EvidenceRole.LABEL,EvidenceRole.TARGET]
    assert evidence.relations[0].source_region_id.endswith(":label")
    assert evidence.relations[0].target_region_id.endswith(":target")
```

- [ ] **Step 2: Run test and verify missing module**

Run: `py -3.11 -m pytest tests/track_a/test_evidence.py -v`

Expected: collection fails because evidence converter is absent.

- [ ] **Step 3: Implement complete conversion**

```python
# src/raglab/track_a/evidence.py
import hashlib
from raglab.contracts.evidence import BBox,EvidenceRegion,EvidenceRegionV1,EvidenceRelation,EvidenceRole,ExtractionMethod,Point
from .models import ArtifactProvenance,GroundingCandidate
from .normalization import normalize_label
def _bbox(points):
    xs,ys=zip(*points); return BBox(min(xs),min(ys),max(xs)-min(xs),max(ys)-min(ys))
def candidate_to_evidence(candidate: GroundingCandidate, p: ArtifactProvenance) -> EvidenceRegionV1:
    key="|".join((p.source_hash,p.artifact_hash,normalize_label(candidate.token.text),candidate.target.region_id))
    eid="ev_"+hashlib.sha256(key.encode()).hexdigest()[:24]
    lid,tid=f"{eid}:label",f"{eid}:target"
    x,y,w,h=candidate.token.box
    label=(Point(x,y),Point(x+w,y),Point(x+w,y+h),Point(x,y+h))
    target=tuple(Point(a,b) for a,b in candidate.target.polygon)
    common=dict(artifact_id=p.artifact_id,artifact_hash=p.artifact_hash,artifact_mime_type=p.artifact_mime_type,
        renderer_name=p.renderer_name,renderer_version=p.renderer_version,render_profile_hash=p.render_profile_hash,
        canvas_width_px=p.canvas_width_px,canvas_height_px=p.canvas_height_px,dpi=p.dpi,page_rotation=p.page_rotation,
        page_box=p.page_box,transform_chain=p.transform_chain)
    regions=(EvidenceRegion(region_id=lid,polygon=label,bbox=BBox(x,y,w,h),role=EvidenceRole.LABEL,**common),
             EvidenceRegion(region_id=tid,polygon=target,bbox=_bbox(candidate.target.polygon),role=EvidenceRole.TARGET,**common))
    return EvidenceRegionV1(evidence_id=eid,source_hash=p.source_hash,document_version=p.document_version,
        generation_id=p.generation_id,native_locator=p.native_locator,role=EvidenceRole.TARGET,text=candidate.token.text,
        regions=regions,relations=(EvidenceRelation(EvidenceRole.LABEL,lid,tid),),extraction_method=ExtractionMethod.OCR,
        confidence=candidate.score,acl_snapshot_id=p.acl_snapshot_id,
        metadata={"target_region_id":candidate.target.region_id,"language":candidate.token.language})
```

- [ ] **Step 4: Run Track A and common evidence tests**

Run: `py -3.11 -m pytest tests/track_a/test_evidence.py tests/contracts/test_evidence.py -v`

Expected: all tests pass and common validation remains authoritative.

- [ ] **Step 5: Commit**

```powershell
git add src/raglab/track_a/evidence.py tests/track_a/test_evidence.py
git commit -m "feat(track-a): emit paired grounding evidence"
```

### Task 5: Build the authorization-safe grounding service

**Files:**
- Create: `src/raglab/track_a/service.py`
- Test: `tests/track_a/test_service.py`

**Interfaces:**
- Consumes: providers, matcher, ranker, converter, `QueryPin`, `AuthorizationGuard`.
- Produces: `GroundingService.query`.

- [ ] **Step 1: Write complete service tests**

```python
# tests/track_a/test_service.py
import pytest
from raglab.contracts.errors import ErrorCode,RaglabError
from raglab.contracts.evidence import NativeLocator
from raglab.security.authorization import AuthorizationGuard,MemoryPolicyStore
from raglab.track_a.models import *
from raglab.track_a.service import GroundingService
class Ocr:
    def __init__(self,tokens): self.tokens=tokens
    def read(self,path): return self.tokens
class Geometry:
    def __init__(self,regions,callback=lambda:None): self.regions,self.callback=regions,callback
    def read(self,path): self.callback(); return self.regions
def setup():
    store=MemoryPolicyStore(); store.allow("u","doc",group_hash="g"*64); guard=AuthorizationGuard(store)
    pin=guard.pin("u","g"*64,"doc","doc-v1","gen-01")
    p=ArtifactProvenance("b"*64,"doc-v1","gen-01","a1","a"*64,"image/png","synthetic","1","c"*64,
        1000,500,144,0,"crop",((1.,0.,0.,0.,1.,0.,0.,0.,1.),),NativeLocator.page(1),pin.query_snapshot_id)
    return store,guard,GroundingRequest("Where is A-12?","fixture.svg",p,pin)
def target(): return TargetRegion("part-a",((.2,.2),(.4,.2),(.4,.5),(.2,.5)),"part")
def test_verified_and_absent() -> None:
    _,guard,request=setup()
    assert GroundingService(guard,Ocr((OcrToken("A-12","en",(.1,.2,.1,.05),1.),)),Geometry((target(),))).query(request).state=="VERIFIED"
    assert GroundingService(guard,Ocr(()),Geometry(())).query(request).state=="PART_ABSENT"
def test_revocation_discards_result() -> None:
    store,guard,request=setup()
    service=GroundingService(guard,Ocr((OcrToken("A-12","en",(.1,.2,.1,.05),1.),)),Geometry((target(),),lambda:store.revoke("doc")))
    with pytest.raises(RaglabError) as raised: service.query(request)
    assert raised.value.code is ErrorCode.AUTHORIZATION_CHANGED
```

- [ ] **Step 2: Run tests and verify missing service**

Run: `py -3.11 -m pytest tests/track_a/test_service.py -v`

Expected: collection fails because service is absent.

- [ ] **Step 3: Implement exact guarded ordering**

```python
# src/raglab/track_a/service.py
from .evidence import candidate_to_evidence
from .matcher import matching_tokens,parse_part_query
from .models import GroundingRequest,GroundingResponse
from .ranker import rank_candidates
class GroundingService:
    def __init__(self,guard,ocr,geometry,minimum_score=.55):
        self.guard,self.ocr,self.geometry,self.minimum_score=guard,ocr,geometry,minimum_score
    def query(self,request: GroundingRequest) -> GroundingResponse:
        self.guard.check(request.pin,"retrieve",diagnostic_id="track-a-retrieve")
        query=parse_part_query(request.question)
        self.guard.check(request.pin,"source_load",diagnostic_id="track-a-source")
        tokens=matching_tokens(query,self.ocr.read(request.artifact_path))
        if not tokens:
            self.guard.check(request.pin,"serialize",diagnostic_id="track-a-serialize")
            return GroundingResponse("PART_ABSENT",(),"no exact visible label matched")
        ranked=rank_candidates(tokens,self.geometry.read(request.artifact_path),3)
        accepted=tuple(item for item in ranked if item.score>=self.minimum_score)
        if not accepted:
            self.guard.check(request.pin,"serialize",diagnostic_id="track-a-serialize")
            return GroundingResponse("ABSTAIN",(),"no target met spatial threshold")
        self.guard.check(request.pin,"model",diagnostic_id="track-a-model-boundary")
        evidence=tuple(candidate_to_evidence(item,request.provenance) for item in accepted)
        self.guard.check(request.pin,"serialize",diagnostic_id="track-a-serialize")
        for item in evidence:
            self.guard.check(request.pin,"highlight_open",diagnostic_id=item.evidence_id)
        return GroundingResponse("VERIFIED",evidence,"explicit candidates verified")
```

- [ ] **Step 4: Run service and common authorization tests**

Run: `py -3.11 -m pytest tests/track_a/test_service.py tests/security/test_authorization.py -v`

Expected: all tests pass; an epoch change yields no response object.

- [ ] **Step 5: Commit**

```powershell
git add src/raglab/track_a/service.py tests/track_a/test_service.py
git commit -m "feat(track-a): add authorization-safe service"
```

### Task 6: Add deterministic evaluation and exploratory gates

**Files:**
- Create: `src/raglab/track_a/evaluation.py`
- Create: `tests/track_a/fixtures/evaluation_cases.json`
- Test: `tests/track_a/test_evaluation.py`
- Create: `docs/track-a-evaluation.md`

**Interfaces:**
- Consumes: frozen cases and observations.
- Produces: `EvaluationCase`, `EvaluationObservation`, `TrackAMetrics`, `evaluate`.

- [ ] **Step 1: Add full fixture and failing test**

```json
[
 {"case_id":"a-ko-01","language":"ko","scan_quality":"native","expected_state":"VERIFIED","expected_label":"부품b","expected_box":[0.2,0.2,0.2,0.3]},
 {"case_id":"a-en-01","language":"en","scan_quality":"native","expected_state":"VERIFIED","expected_label":"a-12","expected_box":[0.5,0.2,0.2,0.3]},
 {"case_id":"a-ja-01","language":"ja","scan_quality":"native","expected_state":"PART_ABSENT","expected_label":"部品c","expected_box":null}
]
```

```python
# tests/track_a/test_evaluation.py
from raglab.track_a.evaluation import *
def test_metrics_and_exploratory_gate() -> None:
    cases=(EvaluationCase("c1","ko","native","VERIFIED","부품b",(.2,.2,.2,.3)),
           EvaluationCase("c2","ja","native","PART_ABSENT","部品c",None))
    obs=(EvaluationObservation("c1","VERIFIED",("부품b",),((.2,.2,.2,.3),),1.,1000,0),
         EvaluationObservation("c2","PART_ABSENT",(),(),2.,2000,0))
    result=evaluate(cases,obs)
    assert (result.label_recall_at_3,result.target_recall_at_3,result.absent_precision)==(1.,1.,1.)
    assert result.status=="EXPLORATORY" and result.p95_latency_seconds==2.
```

- [ ] **Step 2: Run test and verify missing evaluation**

Run: `py -3.11 -m pytest tests/track_a/test_evaluation.py -v`

Expected: collection fails because evaluation is absent.

- [ ] **Step 3: Implement complete metrics**

```python
# src/raglab/track_a/evaluation.py
from dataclasses import asdict,dataclass
import math
Box=tuple[float,float,float,float]
@dataclass(frozen=True,slots=True)
class EvaluationCase: case_id:str; language:str; scan_quality:str; expected_state:str; expected_label:str; expected_box:Box|None
@dataclass(frozen=True,slots=True)
class EvaluationObservation:
    case_id:str; response_state:str; candidate_labels:tuple[str,...]; candidate_boxes:tuple[Box,...]
    latency_seconds:float; peak_working_set_bytes:int; unsupported_critical_claims:int
@dataclass(frozen=True,slots=True)
class TrackAMetrics:
    label_recall_at_3:float; target_recall_at_3:float; absent_precision:float; absent_recall:float
    abstention_rate:float; p50_latency_seconds:float; p95_latency_seconds:float
    maximum_working_set_bytes:int; unsupported_critical_claims:int; status:str; slices:dict[str,dict[str,int]]
    def to_dict(self): return asdict(self)
def _iou(a,b):
    ax,ay,aw,ah=a; bx,by,bw,bh=b; l,t=max(ax,bx),max(ay,by); r,d=min(ax+aw,bx+bw),min(ay+ah,by+bh)
    inter=max(0.,r-l)*max(0.,d-t); union=aw*ah+bw*bh-inter
    return inter/union if union else 0.
def _pct(values,fraction):
    ordered=sorted(values); return ordered[min(len(ordered)-1,math.ceil(len(ordered)*fraction)-1)]
def evaluate(cases:tuple[EvaluationCase,...],observations:tuple[EvaluationObservation,...])->TrackAMetrics:
    by_id={x.case_id:x for x in observations}
    if set(by_id)!={x.case_id for x in cases}: raise ValueError("case and observation IDs must match")
    answerable=[x for x in cases if x.expected_state=="VERIFIED"]
    absent=[x for x in cases if x.expected_state=="PART_ABSENT"]
    labels=sum(x.expected_label in by_id[x.case_id].candidate_labels[:3] for x in answerable)
    targets=sum(any(_iou(x.expected_box,b)>=.5 for b in by_id[x.case_id].candidate_boxes[:3]) for x in answerable if x.expected_box)
    predicted=[x for x in observations if x.response_state=="PART_ABSENT"]
    true_absent=sum(by_id[x.case_id].response_state=="PART_ABSENT" for x in absent)
    slices={}
    for x in cases:
        key=f"{x.language}:{x.scan_quality}"; slices.setdefault(key,{"cases":0,"verified":0})
        slices[key]["cases"]+=1; slices[key]["verified"]+=int(by_id[x.case_id].response_state=="VERIFIED")
    latency=[x.latency_seconds for x in observations]
    return TrackAMetrics(labels/len(answerable) if answerable else 0.,targets/len(answerable) if answerable else 0.,
        true_absent/len(predicted) if predicted else 0.,true_absent/len(absent) if absent else 0.,
        sum(x.response_state=="ABSTAIN" for x in observations)/len(observations),_pct(latency,.5),_pct(latency,.95),
        max(x.peak_working_set_bytes for x in observations),sum(x.unsupported_critical_claims for x in observations),
        "DECISION" if len(cases)>=150 else "EXPLORATORY",slices)
```

- [ ] **Step 4: Write exact evaluation rules and run all relevant tests**

```markdown
# Track A Evaluation Rules

- Synthetic results always remain exploratory.
- Decision status requires at least 150 frozen blind questions or approved power analysis.
- Label Recall@3 uses normalized exact equality; Target Recall@3 requires bbox IoU >= 0.50.
- Report language and scan-quality slices, P50/P95 latency, and peak working set.
- ACL leaks, outbound events, and unsupported critical claims are hard-gate failures.
```

Run: `py -3.11 -m pytest tests/track_a tests/contracts tests/security -v`

Expected: all tests pass and the small fixture reports `EXPLORATORY`.

- [ ] **Step 5: Commit**

```powershell
git add src/raglab/track_a/evaluation.py tests/track_a docs/track-a-evaluation.md
git commit -m "test(track-a): add grounding evaluation gates"
```

## Self-Review Results

- Spec coverage: multilingual explicit matching, at most three candidates, paired label/target regions, artifact binding, five ACL checks, absent/abstain states, CPU measurements, and exploratory status all map to tasks.
- Complete-code check: fixture bodies and matcher, ranker, evidence converter, service, and evaluation implementations are included in full.
- Type consistency: the common envelope is `EvidenceRegionV1`; geometry uses nested `EvidenceRegion`; roles use `EvidenceRole`; extraction uses `ExtractionMethod`; authorization uses `QueryPin` and `AuthorizationGuard`.
- Exclusions: no function inference, model fine-tuning, CAD/BIM interpretation, real security document, or unapproved OCR executable.
