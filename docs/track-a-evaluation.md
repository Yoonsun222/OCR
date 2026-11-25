# Track A evaluation rules

Evaluate one frozen annotation and one observation per question using
`raglab.track_a.evaluation.evaluate`. Case and observation IDs must be unique
and match. The evaluator returns aggregate counts and metrics without
serializing question text, candidate labels, or artifact contents.

## Scoring

- Label Recall@3 is the fraction of expected `VERIFIED` cases with a
  normalized exact label match in the first three ranked observations.
- Target Recall@3 uses the same denominator. A single candidate in the first
  three must carry the matching label and a normalized target bounding box
  with intersection over union of at least 0.50. Candidate slots pair label
  and target box by rank.
- `PART_ABSENT` precision and recall use exact response-state matches.
  Abstention rate counts `ABSTAIN` responses and abstention recall measures
  coverage of annotations marked `ABSTAIN`. State accuracy counts exact
  matches across all three states. A zero denominator yields 0.
- Report the number of cases and `VERIFIED` responses for each language and
  scan-quality combination. Supported languages are `en`, `ko`, and `ja`.
  Scan-quality categories are `native`, `clean_scan`, `degraded_scan`, and
  `synthetic-duplicate`. Latency P50 and P95 use nearest-rank
  percentiles of the supplied per-query observations. Report the maximum
  process peak working set supplied in the observations.

## Gates and evidence

Synthetic data always yields `EXPLORATORY`. Sample eligibility for
`DECISION` requires an explicitly declared frozen blind dataset and either
at least 150 unique questions or an independently approved power analysis.
The calling evaluator is responsible for substantiating the dataset and any
approval. Report production quality with document-cluster confidence
intervals and the applicable lower-bound thresholds.

Security counters default to unmeasured and serialize as `null` when a
category has no observations. The security gate reports `FAIL` on any
observed ACL leak, outbound event, load of a tampered artifact, or unsupported
critical claim. It reports `PASS` when all observed counts are zero, each
threat category has an explicit measurement, and every query observation
includes a critical-claim audit. Missing coverage otherwise yields
`NOT_RUN`, including for a decision-eligible blind sample. Supply counts
from the required threat scenarios and record their scope. A run's zero
counts describe the checks actually performed. Production decisions also
require separate approved-host evidence for security and performance.

## Synthetic service fixture

The checked-in questions exercise the real grounding service, an in-memory
authorization policy, the recorded TSV parser, the synthetic SVG parser,
and paired evidence extraction. The fixture covers Korean, English, and
Japanese verification, absence, duplicate-label abstention, and competing
target abstention. The aligned TSV layout positions the Korean and Japanese
recorded tokens inside their annotated targets. The baseline positions keep
the two ambiguous cases reproducible.

The safety probes check rejection of tampered artifact bytes, a mismatched
path, and revocation after OCR. The test intercepts selected Python socket
entry points before they can open a connection. This measures the Python
fixture path in scope. Actual host egress, real OCR accuracy, production
latency, and production memory budgets require measurements on an approved
runtime. Fixture latency measures elapsed query time, and the working set is
the test process lifetime high-water mark.

```powershell
py -3.11 -m pytest tests/track_a tests/contracts tests/security -v
```
