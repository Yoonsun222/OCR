<!-- /autoplan restore point: git commit fa6282e -->
# Secure Multimodal RAG Lab Design

Date: 2026-11-23  
Status: Revised after gstack + Superpowers review; awaiting execution-plan approval
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

## 21. Binding review amendments

The following decisions supersede conflicting language in Sections 1-19. They are the result of two independent CEO reviews, two engineering reviews, two developer-experience reviews, and a Superpowers design check.

### 21.1 Product wedge and phased scope

The first product is an **ACL-safe evidence locator**, not a general multimodal reasoning system. It finds explicit evidence in documents and returns the original document location, one to three candidate regions, provenance, and an abstention state.

Execution is gate-based rather than calendar-only:

1. **Phase 0 — authorization and feasibility:** exact Microsoft SKU/capability profile, local package/model approval, synthetic fixtures, L16/L32 kill tests, metric contract, and operator ownership.
2. **Phase 1 — narrow evidence pilot:** Track B native cells/tables/charts and Track A explicit text labels with adjacent target regions. Natural-language answer generation is optional; evidence search is mandatory.
3. **Phase 2 — difficult vision:** raster tables/charts, scanned drawings, rotation, low quality, and multilingual label-target grounding.
4. **Phase 3 — mixed-document RAG:** Track C starts only after A/B evidence adapters pass their contract gates.

Track A/B/C may develop adapters in parallel only after the common security, Evidence, indexing, deletion, and evaluation contracts freeze. Track C integration is not independent of A/B.

### 21.2 Baselines are not equivalent execution candidates

- Microsoft 365 Copilot is the currently approved **user-outcome baseline**. It is measured for answer usefulness, completion time, citations shown to the user, and manual verification effort.
- A Microsoft path is a full system candidate only if the approved SKU exposes the required machine-readable evidence and reproducible interface. Unsupported coordinate or deletion capabilities are reported as `N/A`, never scored as zero or silently imputed.
- Manual Office/PDF search is the minimum-cost baseline.
- The local deterministic parser/OCR path is the default product candidate.
- Local VLM use is an optional quality increment that must earn its memory, latency, license, and reapproval cost.
- Build-versus-buy and a Copilot-plus-local-evidence-panel option are included in the final decision.

### 21.3 Phase 0 stop rules

No security document is processed on an unapproved path. Before full implementation:

- L16 and L32 run a two-day representative kill test covering cold/warm load, 1/3/10 visual regions, two concurrent requests, peak working set, page-file activity, and Korean/Japanese OCR.
- If L16 cannot remain below 12 GiB process working set or cannot meet the task SLA without sustained page-file thrashing, L16 becomes deterministic/OCR-only.
- If a local artifact lacks approval, provenance, license decision, or hash lock, that path remains synthetic-only.
- If no domain owner or document custodian can audit even a small critical sample, Go language is limited to "retrieval/location pilot"; engineering interpretation is not claimed.
- If exclusion/adjudication exceeds 10% overall or 15% in a required slice, the affected quality gate cannot pass.

## 22. Security and consistency invariants

### 22.1 Authorization epoch and TOCTOU closure

Every request pins `principal_id`, group hash, `policy_epoch`, `query_snapshot_id`, document version, and active index generation. Cache keys include these values.

Authorization is checked before retrieval, before loading source/crop data, before optional model execution, and immediately before serialization or opening a highlight. If the current policy epoch differs, all partial results are discarded and the request ends as `AUTHORIZATION_CHANGED`; it may restart once from a fresh snapshot. ACL service failure is fail-closed.

Revocation first publishes a deny tombstone, cancels in-flight work, invalidates caches and history access, and only then rebuilds projections. Target revocation-to-block SLO is P99 within two seconds. Multiple-document answers require current authorization for every cited document.

### 22.2 Immutable index generations

Text, structure, and visual indexes are not assumed to share a database transaction. Each immutable `IndexGeneration` records source/version hashes, schema version, ACL projection version, per-index manifests, row counts, checksums, and state.

Only a validated generation becomes `ACTIVE` through a single catalog compare-and-swap. A query pins one generation for its lifetime. Partial or mixed-generation results cannot be served. Crash recovery retains the previous ACTIVE generation and quarantines orphan builds.

### 22.3 EvidenceRegionV1

Each evidence item supports multiple typed regions and relations rather than one bbox. Required fields include:

```text
evidence_id, source_hash, document_version, generation_id
artifact_id, artifact_hash, artifact_mime_type
renderer_name, renderer_version, render_profile_hash
canvas_width_px, canvas_height_px, dpi, page_rotation, page_box
coordinate_space = rendered_top_left_normalized
polygon[], bbox, transform_chain[]
native_locator = page | slide | sheet+cell-range | object-id
role = label | target | legend | series | axis | value | context
extraction_method, language, confidence, schema_version
```

Schema validation enforces finite normalized values, positive area, tagged locators, relation integrity, and an exact artifact-hash match before highlighting. PDF rotation/crop, PPT group transforms, fixed DOCX renderer/font profiles, and XLSX merged/hidden/filtered cells receive golden round-trip tests.

### 22.4 Derivative registry and deletion receipt

Every original, render, crop, OCR/native extraction, embedding, index posting, cache, temporary artifact, annotation export, history item, and backup reference is registered against source hash and document version.

Deletion is tombstone-first and idempotent. `DELETED_VERIFIED` requires per-backend receipts, negative lookups, cache and temporary-store checks, and any applicable key-destruction result. Backup retention produces `DELETION_PENDING_RETENTION`, not successful deletion. Failure blocks serving and raises a security alert.

## 23. Performance and statistical contracts

### 23.1 Task-specific latency

- Deterministic cell/text lookup: P95 <= 5 seconds.
- Evidence candidates with highlights: P95 <= 10 seconds when no visual verifier is needed.
- Visual verification or cross-document comparison: P95 <= 30 seconds, including queue time.
- Work exceeding 10 seconds reports progress and can be cancelled safely.

The 30-second budget reserves approximately 0.3 s for authorization, 1.5 s for parallel retrieval, 0.6 s for fusion, 4 s for reranking, 2 s for region loading, 16 s for optional model verification, 1 s for deterministic calculation, 1 s for coordinate validation, 0.3 s for final authorization, 0.8 s for serialization, and 2.5 s contingency. Cold and warm results are reported separately.

### 23.2 Resource envelopes

- L16: <=12 GiB process working set, one interactive query, <=20 rerank candidates, <=3 visual verification candidates.
- L32: <=26 GiB process working set, up to two interactive queries, <=30 rerank candidates, <=5 visual verification candidates.
- OOM permits one explicit reduced-candidate retry and returns `DEGRADED_RESOURCE_MODE`; quality is never silently reduced.

### 23.3 Evaluation statistics

The original 60-document/120-question numbers are exploratory. They cannot authorize a general Go because the 30% blind set is too small.

- Each production-decision track needs 150-300 blind questions or a pre-registered power calculation justifying less.
- Track B cell observations are counted separately and require several hundred cell-level judgments.
- Quotas are stratified by format, language, difficulty, scan quality, answerable/no-answer class, and criticality.
- Headline metrics report document-cluster bootstrap 95% confidence intervals.
- Candidate comparisons use paired tests; repeated runs measure variance and do not increase independent sample size.
- Hard quality gates use a confidence-interval lower bound, not only a point estimate.
- ACL, egress, tampered artifacts, and unsupported critical claims are threat-scenario gates requiring zero observed violations; zero events are not claimed as proof of zero residual risk.

## 24. Operator and reviewer experience

The approved offline bundle exposes one Windows-first golden path:

```powershell
.\raglab.ps1 verify-bundle
.\raglab.ps1 doctor --profile local16 --offline
.\raglab.ps1 demo --profile local16
```

The target after bundle approval is five minutes to readiness diagnosis, thirty minutes to a synthetic evidence result, and ten minutes for a repeat run on an installed machine.

`doctor` is a hard gate. It checks OS/CPU/RAM/disk, runtime and native DLL compatibility, artifact/model hashes, license and approval manifests, path permissions, offline network enforcement, model dry-load, coordinate round-trip, synthetic parse/OCR/index/query, and derivative deletion. Results are `PASS`, `WARN`, or `BLOCK` and are saved as JSON plus an offline-readable report.

Configuration is versioned and layered as security defaults -> approved site profile -> L16/L32 profile -> track/run file -> restricted CLI overrides. Security controls cannot be weakened by a run override. `config validate`, `config explain`, `config diff`, and `--dry-run` report the effective configuration, provenance, expected derivatives, disk use, and memory risk.

Every run emits a self-contained non-content repro bundle containing a run manifest, redacted effective configuration, approval snapshot, artifact lock, dataset manifest, pipeline manifest, environment profile, metrics, typed errors, network check, and deletion report. Sensitive text, crops, embeddings, and document names are excluded by default.

Stable errors contain code, failed stage, observed versus expected value, protected state, retry safety, exact next action, and an offline documentation reference. Progress reports stage, counts, ETA, memory, checkpoint, retries, and prior ACTIVE generation without logging document content.

## 25. Required implementation and test artifacts

Before real-document execution, the project must provide:

1. Common schema conformance kit and synthetic fixtures.
2. ACL epoch, query snapshot, cancellation, cache-isolation, and final reauthorization tests.
3. Coordinate goldens for PDF rotation/crop, PPT transforms, fixed DOCX rendering, and XLSX merged/hidden/filtered/chart cases.
4. Immutable generation promotion and crash/power-loss/disk-full recovery tests.
5. Derivative registry, tombstone-first deletion, negative lookup, and deletion-receipt tests.
6. L16/L32 cold/warm, memory pressure, concurrency, antivirus/EDR, and eight-hour soak tests.
7. Clean-VM offline install, non-admin account, Korean/Japanese/space/long-path, repair and uninstall tests.
8. Signed CycloneDX or SPDX SBOM plus package, DLL, tokenizer, model, quantization, and prompt provenance.
9. Payload-free logs, redaction tests, network egress tests, and tampered-artifact rejection.
10. Role-specific quickstart, operator, security, evaluation, reproducibility, upgrade/rollback, deletion, incident, and result-schema documents.

## 26. Ownership and approval gates

The experiment does not start on real documents until named owners exist for executive stop/go, security boundary, license/model approval, source data, product/question distribution, technical reproducibility, and final risk acceptance. Each approval records scope, expiry, responsible person, and fallback when its deadline is missed.

Production UI, autonomous engineering judgment, CAD/BIM 3D reasoning, macro execution, external workbook refresh, document-authority decisions, safety certification, fine-tuning, and guaranteed precision reconstruction from chart pixels remain outside the pilot.

The only viewer allowed in the pilot is a read-only evaluation viewer or static report for inspecting evidence overlays. It is test infrastructure, not a production application.

## 27. Decision audit trail

| Phase | Decision | Rationale | Source voices | Status |
|---|---|---|---|---|
| Premise gate | Preserve the evidence-first problem | User approved all original design sections; reviewers agreed the core problem is valid | User, CEO x2 | Accepted |
| CEO | Narrow the first wedge | Three tracks and three paths would dilute the first business proof | CEO x2, Codex | Accepted |
| CEO | Separate Copilot baseline from system candidates | Capability and reproducibility contracts differ | CEO x2, Codex | Accepted |
| CEO | Add user time-to-evidence and manual baseline | Technical recall alone does not prove value | CEO x2 | Accepted |
| Design | Skip production design review | This deliverable is an experiment/security plan; no production UI is in scope | Autoplan Phase 0 | Accepted |
| Engineering | Add authorization epoch and final recheck | Close ACL revocation TOCTOU | EM x2, Codex | Accepted |
| Engineering | Add EvidenceRegionV1 and render transforms | Coordinates must reproduce the visible highlight | EM x2, Codex | Accepted |
| Engineering | Add immutable generations and deletion receipts | Prove atomic serving and derivative removal | EM x2, Codex | Accepted |
| Engineering | Increase statistical rigor | Original blind sample is exploratory | EM x2, Codex | Accepted |
| DX | Add offline doctor and golden path | Approval is useless if the bundle cannot be safely run | DX x2, Codex | Accepted |
| DX | Add repro bundle and role-based handoff | Another operator and reviewer must reproduce the experiment | DX x2, Codex | Accepted |
| Scope | Keep a read-only overlay viewer as test infrastructure | Humans must verify coordinate usefulness | CEO/DX reviews | Accepted |

## 28. Approval recommendation

**Recommendation: CONDITIONAL GO for detailed implementation planning; NO-GO for real security-document execution today.**

Implementation planning may begin for common contracts, synthetic fixtures, offline packaging, Phase 0 feasibility, and Track B/native evidence. Real company documents remain blocked until organizational approvals, artifact intake, `doctor`, security tests, and the Phase 0 stop rules pass.

The next approved activity should be five implementation plans in dependency order: common contracts/security, Windows offline toolkit, Track B narrow wedge, Track A explicit-label spike, and later Track C integration. Superpowers `writing-plans` should be invoked only after the user approves this revised specification.

## GSTACK REVIEW REPORT

- Review mode: gstack autoplan with Superpowers design validation.
- Restore point: Git commit `fa6282e`.
- Voices: two CEO/product reviews, two engineering reviews, and two DX reviews.
- Production design phase: skipped because no production UI is in scope; a read-only evaluation viewer is test infrastructure.
- CEO score: 6.8/10, REVISE.
- Engineering score: 6.7-6.8/10, REVISE.
- DX score: 5.9-6.4/10, REVISE.
- Consensus: the evidence-first and security-first premise is sound, but the initial scope, authorization consistency, coordinate contract, deletion proof, statistical power, and offline operator experience required binding amendments.
- Auto-decisions applied: narrow first wedge; Copilot as user baseline; phase-gated execution; Track C dependency; authorization epoch; immutable index generations; EvidenceRegionV1; derivative registry and deletion receipts; task-specific latency; L16/L32 envelopes; stronger statistics; offline doctor; reproducible run bundle.
- Remaining human decisions: exact Microsoft SKU and capability, approved local artifacts, company retention policy, named owners, usable document corpus, and availability of a domain/custodian audit sample.
- Final gate: approve the revised specification before implementation planning. Real security-document execution remains separately gated.

