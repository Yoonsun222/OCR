# Secure Multimodal RAG Engineering Test Plan

Date: 2026-11-24  
Status: Approval artifact  
Applies to: common contracts, Windows offline toolkit, and Tracks A/B/C

## Release gates

No real-document run is permitted unless security, supply-chain, offline-install, and synthetic contract tests pass. Quality scores cannot override a failed hard gate.

## Test topology

```text
approved bundle -> clean Windows VM -> doctor -> synthetic contract suite
       |                                      |
       v                                      v
 hash/SBOM/license                    Evidence/ACL/deletion
       |                                      |
       +--------------> ingestion generation-+
                                      |
                       atomic ACTIVE promotion
                                      |
 identity + policy epoch -> retrieval/rerank -> final reauthorization
                                      |
                        answer/abstain + evidence overlay
                                      |
                        deletion registry + receipt
                                      |
                 blind quality + security + performance report
```

## Mandatory suites

### Authorization

- Revoke before retrieval, during retrieval, during model work, and before response serialization.
- Group membership, inherited folder ACL, disabled account, policy-service timeout, and cross-user cache isolation.
- Conversation/history re-access after revocation.
- Mixed-ACL multi-document answers and side-channel-safe empty/error behavior.

### Evidence coordinates

- PDF MediaBox/CropBox, rotation, DPI, and normalized-pixel round trip.
- PPT group transform, rotated/cropped image, z-order, and hidden object.
- DOCX renderer/font profile reproducibility.
- XLSX merged cells, hidden/filtered rows, chart series/legend/source range, and sheet stable ID.
- Artifact hash mismatch, old document version, polygon validity, and label-target relation.

### Atomicity and recovery

- One of three indexes fails; mixed generation must not serve.
- Process kill, power loss, disk full, parser hang, model OOM, corrupt input, and retry idempotency.
- Previous ACTIVE remains available; orphan artifacts are quarantined.

### Deletion

- Enumerate and delete original copies, render/crop, extraction, embedding, indexes, caches, temp, history, exports, logs, and backup references.
- Negative lookup, key destruction where applicable, interrupted deletion resume, and backup-expiry state.
- Restart and index compaction must not resurrect tombstoned documents.

### Supply chain and Windows offline operation

- Hash mismatch, missing SBOM/license decision, unsigned or unapproved model, DLL drift, and quantization provenance.
- Clean supported Windows VM, standard user, no internet, Korean/Japanese/space/long paths, repair, uninstall, and network canary.
- Telemetry, crash reporting, proxy/DNS/HTTP/QUIC attempts, temp locations, page file, and Defender quarantine are inspected.

### Performance

- L16 and L32, cold/warm, concurrency 1/2/4, candidate counts 1/3/10/30, memory pressure, page-file activity, and eight-hour soak.
- Report queue time, stage P50/P95/P99, timeouts, peak working set, cache state, and CPU/storage profile.

### Evaluation

- Frozen document-level splits and blind-set access audit.
- Stratified question quotas and no-answer/absent cases.
- Document-cluster bootstrap confidence intervals and paired path comparisons.
- Inter-rater agreement, disagreement/adjudication/exclusion rate, and critical-claim review.
- User time-to-evidence, candidate clicks, first-candidate acceptance, manual verification time, and re-question rate.

## Exit criteria

- Zero observed ACL leakage, unapproved egress, tampered-artifact load, or unsupported critical claim in mandatory scenarios.
- Coordinate and quality metrics meet their pre-registered confidence-interval lower-bound gates.
- L16/L32 remain within their approved resource envelopes.
- Every run produces a valid manifest, redacted diagnostics, network report, and deletion report.
- A second operator can reproduce the synthetic run from the offline handoff without assistance.
