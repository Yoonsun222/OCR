# Common Security Contracts Task 6 Report

## Result

DONE. Added payload-free structured observability in `src/raglab/observability/`, then tightened its API and validation in response to review.

`SafeEvent` accepts only known event names, registered-kind `OpaqueId` instances, registered `EventStage` values, and finite numeric or boolean values from the strict metric allowlist. `OpaqueId` values can be issued for a registered kind or parsed from a strict `kind_` plus 32 lowercase hexadecimal representation; direct construction is blocked. Events snapshot and sort metrics. Metric keys must exactly match the lowercase allowlist; case-fold collisions are rejected, and forbidden-key errors use a fixed message without reflecting key content. `JsonlEventWriter` appends compact UTF-8 JSONL records with UTC timestamps, serializes concurrent writers within one process, flushes, and calls `fsync` after each record. Its documentation specifies that separate processes must use separate paths. Parent directories are created only when the configured path has a parent other than the current directory.

Tests reject payload and unregistered fields, invalid ID representations, wrong ID kinds, strings in typed fields, unregistered stages, unsafe metric values and keys, and verify compact newline-safe output, UTC timestamps, immutable metric snapshots, concurrent thread appends, and the writer's single-process scope.

## Verification

- Red: `py -3.11 -m pytest tests/observability/test_events.py -v` failed during collection because `raglab.observability` was missing.
- Focused: `py -3.11 -m pytest tests/observability/test_events.py -v` — 24 passed.
- Full suite: `py -3.11 -m pytest -q` — 135 passed.
- `git diff --check` — clean.

## Review follow-up

- Red: after updating tests to use the typed API, focused collection failed because `EventStage` was not yet implemented. This confirmed the typed API gap.
- Green: `py -3.11 -m pytest tests/observability/test_events.py -v` — 35 passed after implementing typed IDs/stages, exact metric keys, fixed forbidden-key errors, and the documented writer scope.
- A first assertion on the writer docstring exposed wording that did not explicitly say `single-process`; the docstring now states that scope directly.
- Event IDs and test fixtures remain synthetic. No network or company documents were used.
- Final focused rerun: `py -3.11 -m pytest tests/observability/test_events.py -v` — 35 passed.
- Final full suite: `py -3.11 -m pytest -q` — 146 passed.
- Final `git diff --check` — clean.

Pytest 8.3.5 was installed into the local Python 3.11 user environment because it was initially unavailable. No network or company documents were used; identifiers and test content are synthetic.

## Review notes

The allowed metric `coordinate_validation_failures` is explicitly permitted even though other coordinate-related fields are rejected. The append lock protects threads in this process; coordination between separate processes is not provided.
