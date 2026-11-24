# Common Security Contracts Task 6 Report

## Result

DONE. Added payload-free structured observability in `src/raglab/observability/`, then tightened its API and validation in response to review.

`SafeEvent` accepts only known event names, registry-issued `OpaqueId` instances of the registered kinds, registered `EventStage` values, and finite numeric or boolean values from the strict metric allowlist. `OpaqueId` has no public issue or parse constructor. `OpaqueIdRegistry.map(kind, source_id)` accepts upstream identifiers as nonblank strings up to 256 characters with no Unicode control characters, then maps them to random 128-bit observability IDs and returns the same mapping for repeated calls during that registry's lifetime. This supports upstream forms such as UUID4 query snapshot IDs and nonblank document/generation IDs. The source ID is held only by the in-memory registry and is never copied into an event, JSON record, or validation error. `SafeEvent` rejects IDs that were not sealed by a registry. Registered stages include `source_load`, `model`, `serialize`, and `highlight_open`. Events snapshot and sort metrics. Metric keys must exactly match the lowercase allowlist; case-fold collisions are rejected, and forbidden-key errors use a fixed message without reflecting key content. `JsonlEventWriter` appends compact UTF-8 JSONL records with UTC timestamps, serializes concurrent writers within one process, flushes, and calls `fsync` after each record. Its documentation specifies that separate processes must use separate paths. Parent directories are created only when the configured path has a parent other than the current directory.

Tests reject payload and unregistered fields, invalid upstream ID bounds, wrong ID kinds, direct ID construction, strings in typed fields, and unregistered stages. They verify stable registry mappings, source-value non-disclosure for both legacy and encoded source IDs, actual QueryPin identifier compatibility, unsafe metric values and keys, compact newline-safe output, UTC timestamps, immutable metric snapshots, concurrent thread appends, and the writer's single-process scope.

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

## Registry sealing follow-up

- Red: the updated tests failed during collection because `OpaqueIdRegistry` was missing. The old direct `OpaqueId.parse` test was replaced with tests requiring no public parse/issue API, a sealed registry mapping, token non-disclosure, upstream QueryPin compatibility, and the four additional registered stage values.
- Green: added `OpaqueIdRegistry` with bounded, nonblank, control-free source ID validation, random observability IDs, and a per-registry stable mapping. `SafeEvent` now checks registry issuance; direct ID construction alone cannot create an accepted event ID. Added `source_load`, `model`, `serialize`, and `highlight_open` to `EventStage`.
- Focused: `py -3.11 -m pytest tests/observability/test_events.py -v` — 39 passed.
- Full suite: `py -3.11 -m pytest -q` — 150 passed.
- `git diff --check` — clean.
- API guidance: create one `OpaqueIdRegistry` for the relevant process lifetime; map upstream run/query/document/generation IDs with `map(OpaqueIdKind.<KIND>, source_id)` and pass the returned IDs to `SafeEvent`. Do not include raw source IDs in metrics or stage values.

## Upstream identifier compatibility follow-up

- Red: added a test using `MemoryPolicyStore` and `AuthorizationGuard.policy_only(...).pin(...)`; the real UUID4 `query_snapshot_id` failed the registry's former numeric-suffix validation. Added additional bounds tests for empty, control-containing, and overlong source IDs.
- Green: `OpaqueIdRegistry.map` now checks only string type, nonblank content, a 256-character maximum, and absence of Unicode control characters. It no longer depends on upstream prefixes or suffix formats. Registry outputs remain random typed IDs; source IDs are not copied into events, JSON, or validation errors.
- Replaced probabilistic substring checks with exact field-value comparisons for the serialized observability IDs against their source IDs. Encoded source tokens can be mapped, but only their independent random observability token appears in JSON.
- A regression test first exposed acceptance of Unicode C1 control character U+0085; validation now rejects all Unicode `Cc` characters.
- Focused: `py -3.11 -m pytest tests/observability/test_events.py -v` — 44 passed.
- Full suite: `py -3.11 -m pytest -q` — 155 passed.
- `git diff --check` — clean.

Pytest 8.3.5 was installed into the local Python 3.11 user environment because it was initially unavailable. No network or company documents were used; identifiers and test content are synthetic.

## Review notes

The allowed metric `coordinate_validation_failures` is explicitly permitted even though other coordinate-related fields are rejected. The append lock protects threads in this process; coordination between separate processes is not provided.
