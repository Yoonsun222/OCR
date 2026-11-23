# Secure Multimodal RAG Lab Design

Date: 2026-11-23  
Status: Approved for detailed review  
Primary environment: company-controlled Windows CPU workstation  
Target scale: 100-1,000 documents, approximately 50,000 pages  
Target query latency: P95 within 30 seconds after offline indexing

## 1. Problem statement

The organization needs to answer questions over security-sensitive PDF, PowerPoint, Word, Excel, and image documents. Documents mix prose, tables, charts, engineering drawings, photos, and diagrams. Korean, English, and Japanese may coexist on the same page. The highest-value questions require understanding tables, graphs, and image regions, for example: “Where is part A?”

The system must return a short answer plus up to three ranked evidence candidates. Each candidate must identify the source document, page/slide/sheet, cell range or normalized image coordinates, confidence, and a highlighted source region. A bare text answer without traceable evidence is not acceptable.

Actual security documents may be used only inside the company. This external project contains design files, schemas, synthetic examples, and evaluation scaffolding only.

## 2. User outcome and product boundary

The first product is an evidence-location assistant, not an autonomous engineering authority. It locates explicitly visible evidence, connects labels to nearby structures, performs deterministic table calculations, and presents up to three candidates. It does not certify engineering correctness or make expert safety decisions.

The default response for ambiguous evidence is multiple candidates. If all candidates are weak, the system returns `판단 불가` and still shows the most relevant regions when safe.

### In scope

- Track A: engineering drawings and part-image localization
- Track B: tables, multi-sheet Excel, and graph interpretation
- Track C: mixed-document RAG across related files
- Microsoft-authorized baseline
- CPU-only local baseline for 16 GB and 32 GB RAM classes
- Company-server comparison only after separate approval
- Evidence coordinates, highlighting, audit metadata, and access-control filtering
- Korean, English, and Japanese retrieval and OCR evaluation
- Offline indexing followed by interactive queries
- Security, licensing, performance, and quality gates

### Not in scope

- Training a foundation model from scratch
- Uploading company documents to unapproved services
- Treating Microsoft 365 Copilot approval as approval for Azure or third-party agents
- Expert certification of engineering conclusions
- Automatic execution of instructions found inside documents
- Model fine-tuning during the first pilot
- Public release of model weights or company-derived datasets
- A production end-user UI in the first experiment phase

## 3. Success criteria

Security and licensing are hard gates, not weighted scores. Any unauthorized disclosure, external transmission, unclear license, untraceable artifact, failed derivative-data deletion, or execution of document-borne instructions is No-Go for that path.

Performance and quality targets:

| Area | Target |
|---|---:|
| Track A Recall@3 | >= 85% |
| Track A page accuracy | >= 90% |
| Track B native cell-value accuracy | >= 98% |
| Track B calculation accuracy | >= 95% |
| Track B chart series-to-legend accuracy | >= 90% |
| Track C document Recall@10 | >= 95% |
| Track C evidence Recall@3 | >= 85% |
| Citation support rate | >= 95% |
| Unsupported critical claims | <= 2% |
| Access-control leaks | 0 |
| External transmissions in local mode | 0 |
| P95 query latency after indexing | <= 30 seconds |

Passing these targets does not validate domain-expert reasoning because domain experts are unavailable for annotation. It validates retrieval, localization, deterministic computation, and surface-level evidence interpretation.

## 4. Architecture

```text
                         COMPANY SECURITY BOUNDARY
+----------------------------------------------------------------------------+
| Document inventory + ACL + hashes + approved processing paths              |
|                                  |                                         |
|             +--------------------+--------------------+                    |
|             v                    v                    v                    |
|   Track A: drawings     Track B: tables/charts   Track C: mixed RAG        |
|             |                    |                    |                    |
|             +--------------------+--------------------+                    |
|                                  v                                         |
|          native parsers + render/OCR + visual-region analysis              |
|                                  v                                         |
|                 normalized Evidence records with ACL                       |
|                    /             |              \                          |
|             text index      structure index      visual index              |
|                    \             |              /                          |
|               ACL filter -> fast candidate retrieval                       |
|                                  v                                         |
|                 top-region reranking and verification                      |
|                 /                |                 \                        |
|        Microsoft path       CPU local path       server path               |
|                 \                |                 /                        |
|                     up to 3 evidence candidates                            |
|                                  v                                         |
|         answer + confidence + source + coordinates + highlight             |
+----------------------------------------------------------------------------+
```

### 4.1 Component boundaries

1. Inventory and policy layer owns file identities, hashes, classification, and ACL snapshots.
2. Format adapters extract native structure without deciding semantic answers.
3. Rendering and OCR produce coordinate-bearing alternatives and never overwrite native extraction.
4. Evidence normalization maps all formats into one typed schema.
5. Index builders create text, structure, and visual indexes from Evidence records.
6. Retrieval enforces ACL before ranking and returns candidate evidence only.
7. Path adapters run Microsoft, local CPU, or approved-server interpretation behind the same contract.
8. Answer composition refuses to publish claims without an attached evidence candidate.
9. Evaluation compares paths using frozen datasets and blind annotations.

## 5. Data flow

### 5.1 Ingestion happy path

```text
file discovered
  -> validate type/size/policy
  -> compute hash and ACL snapshot
  -> native parse
  -> render pages/slides/sheets
  -> OCR and region classification
  -> reconcile native and rendered coordinates
  -> validate Evidence records
  -> build three indexes atomically
  -> mark document index READY
```

### 5.2 Shadow paths

```text
missing file       -> INDEX_REJECTED, no partial index
zero-byte file     -> INDEX_REJECTED_EMPTY, visible inventory error
unsupported format -> INDEX_REJECTED_FORMAT, retain metadata only
encrypted file     -> INDEX_BLOCKED_ENCRYPTION, request approved handling
parser failure     -> render/OCR fallback, mark native extraction unavailable
OCR failure        -> preserve native evidence, mark OCR unavailable
coordinate mismatch-> quarantine affected regions from grounded answers
partial index      -> never promote; keep previous READY version
ACL change         -> block document, rebuild ACL projection before serving
file deletion      -> tombstone, remove indexes/cache, verify deletion
```

### 5.3 Query path

```text
question
  -> validate language, length, and request identity
  -> resolve caller ACL
  -> retrieve text/structure/visual candidates
  -> fuse and deduplicate candidates
  -> keep 10-30 regions
  -> rerank/verify with selected execution path
  -> validate evidence coordinates and claim support
  -> return <= 3 candidates or 판단 불가
```

Queries never trigger full-corpus reprocessing. CPU-heavy interpretation is limited to retrieved regions.

## 6. Evidence contract

```text
Evidence {
  evidence_id: string
  document_id: string
  document_version: string
  source_hash: sha256
  source_type: pdf|pptx|docx|xlsx|image
  locator: page|slide|sheet|cell_range
  region: normalized x,y,width,height in [0,1]
  text: string|null
  language: ko|en|ja|mixed|unknown
  element_type: paragraph|table|chart|drawing|image|label|legend
  relations: typed references to caption, legend, cell, shape, connector
  extraction_method: native|ocr|visual|derived
  confidence: calibrated number in [0,1]
  acl_snapshot_id: string
  processing_path: microsoft|local16|local32|server
  created_at: timestamp
}
```

Every coordinate is normalized against the rendered artifact whose hash is recorded. Excel cells use cell ranges as the primary locator and rendered coordinates as secondary evidence. Native and OCR text are separate observations with provenance.

## 7. Execution paths

### 7.1 Microsoft baseline

Microsoft 365 Copilot is the approved baseline only inside the existing company tenant. Before testing, administrators must confirm exact SKU, organizational grounding, interaction retention, Purview coverage, web-search settings, third-party model/agent settings, SharePoint and OneDrive oversharing, export permissions, and any usage-based credits.

Microsoft states that prompts, responses, and Microsoft Graph data used by Copilot are not used to train foundation models and that existing permissions govern exposure. Existing oversharing remains a risk. Sources:

- https://learn.microsoft.com/en-us/microsoft-365/copilot/microsoft-365-copilot-privacy
- https://learn.microsoft.com/en-us/microsoft-365/copilot/microsoft-365-copilot-architecture-data-protection-auditing
- https://learn.microsoft.com/en-us/microsoft-365/copilot/enterprise-data-protection

Web search is disabled for experiments unless explicitly approved. Copilot outputs that cannot provide machine-readable coordinates remain useful as answer-quality baselines but cannot pass the evidence-localization gate by themselves.

Azure AI Document Intelligence, Azure OpenAI, Foundry, Copilot Studio connectors, and third-party agents require separate approval. Azure Document Intelligence may temporarily store encrypted input and results during processing, so region, retention, deletion, network, identity, and diagnostic-log settings must be reviewed. Source:

- https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/document-intelligence/data-privacy-security

### 7.2 Local CPU baseline

The local path runs without external API calls. Candidate components, subject to company review:

| Role | Candidate | License starting point | Experiment role |
|---|---|---|---|
| Document structure | Docling | MIT codebase | PDF/Office normalization baseline |
| Primary OCR | PaddleOCR | Apache-2.0 project | multilingual OCR, layout, table/chart candidates |
| OCR comparator | Tesseract | Apache-2.0 | simple OCR baseline |
| CPU runtime | llama.cpp | MIT | quantized LLM/VLM inference |
| Compact document VLM | PaddleOCR-VL family | verify exact weight revision | document region parsing comparator |
| Compact general VLM | Qwen3-VL small family | verify exact checkpoint and conversion | image QA and grounding comparator |

Code license, model-weight license, converted artifact provenance, dependency licenses, training-data notices, and commercial/redistribution restrictions are reviewed separately.

Local tiers:

- L16: 16 GB RAM, conservative model size and context, deterministic tools preferred.
- L32: 32 GB RAM, larger reranker/VLM candidate where latency permits.

Document structure and arithmetic use deterministic libraries. VLMs inspect only top-ranked regions.

### 7.3 Approved server path

This path is dormant until approved. It must preserve user-level ACLs, network isolation, auditability, deletion, and artifact provenance. It is not a shared unfiltered vector store.

## 8. Security model

### 8.1 Classified derivatives

File names, metadata, rendered pages, crops, OCR text, embeddings, indexes, prompts, answers, annotations, logs, and caches inherit the document classification.

### 8.2 Required controls

- deny-by-default processing-path allowlist
- ACL filtering before retrieval and again before response composition
- no document-derived content in routine logs
- fixed cache/temp directories with verified cleanup
- model and binary hashes pinned
- outbound network denial test for local mode
- no remote-code execution in model loaders
- immutable audit records for access and processing path
- tombstone and verified derivative deletion on source deletion
- prompt-injection isolation: document content is data, never authority
- malicious instructions in text, image, metadata, or filenames are ignored

### 8.3 No-Go security tests

- query names/content of unauthorized documents
- revoke access then query immediately
- attempt cross-user history access
- place external-upload instructions inside documents
- place hidden instructions inside images and metadata
- observe DNS/HTTP traffic during local execution
- inspect temp/log/cache locations after processing and deletion
- corrupt a model or package and verify hash rejection

## 9. License and supply-chain gate

For every component, record name, version, source URL, commit/model revision, code license, weight license, dependency licenses, commercial use, redistribution, notices, patents, use restrictions, hash, reviewer, date, and approval status.

Reject artifacts with unclear licensing, research-only restrictions that conflict with company use, unofficial converted weights, mandatory external telemetry, remote code execution, model-card/file-license conflicts, or incomplete dependency inventories.

Offline intake procedure:

1. download only from official sources in a non-secret staging environment;
2. pin versions and revisions;
3. record SHA-256 hashes;
4. scan for malware;
5. generate SBOM and license notices;
6. inspect installation and runtime network behavior;
7. transfer through company-approved intake;
8. test offline installation;
9. verify no runtime external traffic;
10. distribute only approved artifacts internally.

Automatic updates are disabled. Every upgrade is a new reviewed artifact.

## 10. Experimental protocol

### 10.1 Shared matrix

Each track compares M (Microsoft), L16, L32, and optionally S (approved server) on identical documents and questions. Initial pilot per track: 60 documents and 120 questions.

Split by document, never page or sheet:

- development 40%
- validation 30%
- final blind evaluation 30%

The final set is opened only after pipeline configuration freezes.

### 10.2 Track A: drawings and part images

Corpus includes drawings, assembly/exploded views, labeled photos, legends, repeated components, low-quality scans, rotated pages, and mixed-language labels.

Question classes:

- direct location
- translated or synonymous label
- all occurrences
- relationship location
- symbol/legend association
- absent-part query
- visually similar distractor

Ground truth includes page, region, acceptable alternate regions, visible label, multiplicity, and unanswerable status. Questions requiring domain-expert inference are exploratory and excluded from pass/fail.

Metrics: Recall@3, page accuracy, region overlap, absent-query accuracy, evidence-answer agreement, latency, and peak RAM.

### 10.3 Track B: tables, Excel, and graphs

Corpus includes multiple sheets/tables, merged and multirow headers, hidden structures, formulas, pivots, Office charts, rasterized tables/charts, multilingual headers, and mixed axes/units.

Question classes:

- single-cell value
- conditional row/column
- cross-sheet comparison
- sum/difference/ratio
- extrema/ranking
- trend/inflection
- legend-series mapping
- table-chart consistency
- unit conversion
- absent item

Native workbook values and formulas create machine ground truth. Image-chart numeric answers use a tolerance and dual human review. Hidden data is excluded by default unless explicit permissions and query intent allow it.

Metrics: cell/range localization, structure recovery, numeric accuracy, calculation accuracy, legend mapping, trend agreement, citation coverage, latency, and RAM.

### 10.4 Track C: mixed-document RAG

Corpus groups related files, cross-file text and imagery, tables and charts in separate files, old/new versions, similar names, mixed languages, and differing ACLs.

Question classes:

- single-document retrieval
- cross-document comparison
- text-to-image location
- table-to-chart connection
- version change
- conflict detection
- unauthorized related-document query
- no-answer query
- combined location and specification

Metrics: document Recall@10, evidence Recall@3, answer accuracy, claim-level citation completeness and support, version accuracy, zero ACL leakage, abstention accuracy, latency, and RAM.

## 11. Annotation without domain experts

Two general evaluators annotate independently and are blinded to model/path identity. Only answers explicitly verifiable from the source are scored.

- both agree correct: correct
- both agree incorrect: incorrect
- disagree: adjudication bucket, excluded from headline metric until resolved
- requires expert knowledge: exploratory bucket
- source is ambiguous: unanswerable/ambiguous bucket

Report inter-rater agreement and the share of excluded questions. A system cannot receive Go if excluded/ambiguous questions are so numerous that headline metrics are unstable.

## 12. Reproducibility

Each run records run ID, timestamp, dataset versions, pipeline version, component/model revisions, quantization, prompt version, hardware, threads, memory limit, network state, input hashes, output, evidence coordinates, latency, peak RAM, and typed errors.

Run each candidate at least three times under controlled power and background-process settings. Separate cold and warm-cache performance.

## 13. Error and state model

Document index states:

```text
DISCOVERED -> VALIDATING -> EXTRACTING -> NORMALIZING -> INDEXING -> READY
     |            |             |              |            |
     +----------> REJECTED/BLOCKED/FAILED <-----+------------+
READY -> STALE -> REINDEXING -> READY
READY/STALE/FAILED -> DELETING -> DELETED_VERIFIED
```

Invalid transitions are rejected and audited. Only READY indexes serve answers. Reindexing retains the previous READY version until atomic promotion succeeds, unless ACL revocation requires immediate blocking.

Typed error families include invalid/empty/unsupported/encrypted inputs, parse/OCR/render/coordinate failures, index conflicts, ACL failures, retrieval timeout, model timeout/OOM/refusal/malformed output, evidence validation failure, deletion verification failure, and policy denial.

Every error must state the problem, cause, corrective action, and whether retry is safe. Generic catch-and-continue behavior is prohibited.

## 14. Performance plan

Offline extraction may be slow; interactive query latency is the hard target. The retrieval stage reduces approximately 50,000 pages to 10-30 candidate regions. Only candidates receive expensive visual verification.

Measure ingestion throughput, index size, cold/warm retrieval latency, reranker latency, total P50/P95/P99 query latency, peak working set, model load time, and failure under memory pressure.

If P95 exceeds 30 seconds, the system may return fast retrieval candidates with an explicit `정밀 분석 미완료` state. It must not silently time out or present preliminary results as fully verified.

## 15. Schedule

| Week | Work |
|---|---|
| 1 | security boundary, approvals, schema, hardware inventory, evaluator training |
| 2 | stratified document selection for all tracks |
| 3 | question/evidence annotation and frozen dataset versions |
| 3-4 | Microsoft and deterministic extraction baselines |
| 4-5 | text/structure/visual retrieval and CPU model comparison |
| 6 | error analysis and one controlled improvement cycle |
| 7 | blind final evaluation |
| 8 | decision report, operating recommendation, and reproducibility package |

## 16. Decision rules

After hard gates, weighted comparison:

| Dimension | Weight |
|---|---:|
| answer/candidate retrieval accuracy | 25 |
| evidence location and citation accuracy | 20 |
| table/chart/image interpretation | 20 |
| latency and stability | 15 |
| CPU/RAM efficiency | 10 |
| operations and upgrade burden | 5 |
| direct cost | 5 |

Tie-breakers: clearer ACL/evidence traceability, fewer unsupported answers, local reproducibility, fewer components/models, and lower total cost, in that order.

Possible decisions: Go, Conditional Go, Hybrid Go, Re-test with experts, or No-Go. The expected first outcome is Conditional Go for explicitly visible evidence localization, not general engineering reasoning.

## 17. Deliverables and implementation decomposition

The repository will eventually contain documentation, typed schemas, CSV/XLSX templates, synthetic fixtures, common evaluation tools, and isolated Track A/B/C adapters. No security-document artifacts are committed.

Implementation must be split into five separately testable plans:

1. common security, schemas, inventory, and evaluation harness;
2. Track A drawing/image localization;
3. Track B table/Excel/chart extraction and deterministic QA;
4. Track C mixed-document retrieval and citation composition;
5. integration benchmark, scorecard, and company-run handoff.

Each track may proceed in parallel after the common contracts freeze.

## 18. Open organizational approvals

These are not design ambiguities; they are execution gates to resolve inside the company:

- exact Microsoft product and license SKU
- ability to disable web grounding for experiment accounts
- permitted SharePoint/OneDrive test site and Purview controls
- local installation policy
- approved package/model intake process
- Azure/service eligibility, if any
- standard PC CPU, RAM, and storage specifications
- retention and deletion evidence requirements
- legal owner for open-source and model review

No unapproved path is exercised while these remain unresolved.

## 19. Design self-review checklist

- [x] No placeholder requirements
- [x] Security documents and derivatives remain inside the company
- [x] Architecture supports three parallel tracks and three execution paths
- [x] CPU-only 16 GB/32 GB paths are first-class
- [x] Evidence coordinates and highlighting are mandatory
- [x] Expert-annotation limitation is explicit
- [x] Security and licensing are hard gates
- [x] Failure, deletion, ACL, and timeout states are named
- [x] Experiment split prevents page-level leakage
- [x] Final evaluation is blind and frozen
- [x] Scope is decomposed before implementation planning

## 20. Sources reviewed

- Microsoft Copilot privacy and security: https://learn.microsoft.com/en-us/microsoft-365/copilot/microsoft-365-copilot-privacy
- Microsoft Copilot data protection and auditing: https://learn.microsoft.com/en-us/microsoft-365/copilot/microsoft-365-copilot-architecture-data-protection-auditing
- Microsoft enterprise data protection: https://learn.microsoft.com/en-us/microsoft-365/copilot/enterprise-data-protection
- Microsoft Document Intelligence data privacy and security: https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/document-intelligence/data-privacy-security
- Docling repository: https://github.com/docling-project/docling
- PaddleOCR repository: https://github.com/PaddlePaddle/PaddleOCR
- Tesseract license: https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE
- llama.cpp repository: https://github.com/ggml-org/llama.cpp
- Qwen3-VL license: https://github.com/QwenLM/Qwen3-VL/blob/main/LICENSE

