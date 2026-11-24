# Common Security Contracts Task 8 Report

## Result

DONE. Added the common security contract reference and the public import-surface conformance test. The existing `raglab.contracts.__all__` already exactly matched the evidence/error API frozen by the brief, so no production exports needed changing. The document preserves reviewed `EvidenceRegion`, `EvidenceRole`, and `ExtractionMethod` exports and records security, indexing, deletion, and observability APIs in their actual package/module surfaces.

## TDD and verification

- The public-surface test was written before any implementation change. Its first run passed because Tasks 1-2 had already implemented the exact export list; therefore there was no missing production behavior to change, and the required red phase was not applicable to this documentation/public-surface test.
- `py -3.11 -m pytest tests/conformance/test_public_contract.py -v`: 1 passed.
- `py -3.11 -m pytest tests/conformance -q`: 10 passed.
- `py -3.11 -m pytest -v`: 165 passed, zero failures/errors.
- `py -3.11 -m pytest --collect-only -q`: 165 tests collected.
- `git diff --check`: passed.

## Review notes

The reference distinguishes `policy_only` from `deletion_managed`, states the five authorization boundaries and fail-closed outcomes, enumerates EvidenceRegionV1 fields and geometry requirements, documents all three manifests and ACTIVE compare-and-swap, retired generation ID fingerprints, derivative backends and tombstone-first deletion, terminal deletion states, payload-free event metrics and the writer's single-process thread-safety boundary, safe error serialization, compatibility rules, and conformance commands/counts.

## Commit

Commit `24773ac` (`docs: freeze common security contracts`).

