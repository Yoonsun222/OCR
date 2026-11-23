# Windows Offline Toolkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows-first, offline-safe operator toolkit that verifies an approved bundle, blocks unsafe runs, executes a synthetic evidence demo, and emits reproducible non-content diagnostics.

**Architecture:** A Python 3.11 standard-library CLI owns deterministic validation and report generation, while a PowerShell 5.1 wrapper supplies the operator entry point and Windows-specific probes. Security defaults are immutable at runtime, system access is isolated behind injectable ports, and every command writes machine-readable JSON plus a concise console result without document content.

**Tech Stack:** Python 3.11 standard library, PowerShell 5.1, `pytest` as the only development dependency, JSON configuration and manifests, Windows catalog/Authenticode verification, static offline HTML.

## Global Constraints

- Run only on a company-controlled Windows CPU workstation; real security documents remain blocked until company approvals and all Phase 0 gates pass.
- Python support is `>=3.11,<3.13`; PowerShell support is Windows PowerShell 5.1 without PowerShell 7-only syntax.
- Runtime code uses the Python standard library only; every additional package, DLL, model, font, or executable requires a hash, provenance, license decision, and approval record before loading.
- L16 is limited to 12 GiB process working set and one interactive query; L32 is limited to 26 GiB and two interactive queries.
- Security settings are fail-closed and cannot be weakened by a run-file or CLI override.
- Routine logs and repro bundles exclude document names, document text, OCR output, crops, embeddings, questions, and secrets.
- Default network verification is passive. An active egress canary may run only in a dedicated company test environment with an explicit approval ID and a security-owned sink.
- The first synthetic result must be achievable within 30 minutes after receiving an approved bundle; readiness diagnosis must finish within five minutes.
- Tests use only synthetic fixtures and mocks. They never load company documents, external models, or public network services.
- Every task follows red-green-refactor TDD and ends in a focused commit.

---

## File Structure

```text
raglab.ps1                              # PowerShell 5.1 operator entry point
pyproject.toml                          # Python metadata and pytest configuration
src/raglab/__init__.py                  # Package version
src/raglab/__main__.py                  # python -m raglab entry point
src/raglab/cli.py                       # argparse command routing and exit codes
src/raglab/offline/models.py            # Immutable report/config/run dataclasses
src/raglab/offline/ports.py             # Windows/system probe protocols
src/raglab/offline/jsonio.py            # Canonical JSON and atomic file writes
src/raglab/offline/bundle.py            # Hash/catalog/approval verification
src/raglab/offline/config.py            # Layered config validation and provenance
src/raglab/offline/doctor.py            # PASS/WARN/BLOCK readiness checks
src/raglab/offline/errors.py            # Stable operator-facing error records
src/raglab/offline/progress.py          # Payload-free progress event stream
src/raglab/offline/demo.py              # Synthetic evidence and overlay demo
src/raglab/offline/repro.py             # Non-content reproducibility bundle
src/raglab/offline/windows.py           # Windows 5.1 subprocess probes
configs/security-defaults.json          # Non-overridable security policy
configs/profiles/local16.json            # L16 resource envelope
configs/profiles/local32.json            # L32 resource envelope
fixtures/synthetic-mini/source.svg       # Harmless diagram with a labeled part
fixtures/synthetic-mini/expected.json    # Expected evidence coordinates
scripts/offline/install.ps1              # Idempotent standard-user installation
scripts/offline/uninstall.ps1            # Policy-aware removal and report
scripts/offline/test-clean-vm.ps1        # Clean-VM acceptance harness
tests/offline/                           # Unit, contract, and CLI tests
docs/operator/START-HERE.md              # Golden path
docs/operator/OFFLINE-INSTALL.md         # Install, repair, and uninstall
docs/operator/CONFIGURATION.md            # Layer and override reference
docs/operator/ERROR-CATALOG.md            # Stable errors and next commands
docs/operator/REPRODUCIBILITY.md          # Replay and environment comparison
docs/operator/SECURITY-REVIEW.md          # Reviewer evidence and limitations
```

## Exit-Code Contract

| Code | Meaning |
|---:|---|
| 0 | Command completed and all required checks passed |
| 2 | Operator input or configuration is invalid |
| 3 | Policy or approval blocked execution |
| 4 | Bundle integrity or signature failed |
| 5 | Environment doctor found a blocking condition |
| 6 | Synthetic demo contract failed |
| 7 | Repro bundle validation failed |
| 10 | Unexpected internal failure; no unsafe continuation occurred |

### Task 1: CLI Skeleton and Atomic JSON Output

**Files:**
- Create: `pyproject.toml`
- Create: `src/raglab/__init__.py`
- Create: `src/raglab/__main__.py`
- Create: `src/raglab/cli.py`
- Create: `src/raglab/offline/__init__.py`
- Create: `src/raglab/offline/models.py`
- Create: `src/raglab/offline/jsonio.py`
- Create: `raglab.ps1`
- Test: `tests/offline/test_cli.py`
- Test: `tests/offline/test_jsonio.py`

**Interfaces:**
- Consumes: no project code.
- Produces: `raglab.cli.main(argv: Sequence[str] | None) -> int`, `raglab.offline.jsonio.write_json_atomic(path: Path, value: Mapping[str, object]) -> None`, and `CheckStatus = PASS | WARN | BLOCK`.

- [ ] **Step 1: Write failing CLI and atomic-write tests**

```python
# tests/offline/test_cli.py
from raglab.cli import main

def test_no_command_returns_operator_input_error(capsys):
    assert main([]) == 2
    assert "usage: raglab" in capsys.readouterr().err

# tests/offline/test_jsonio.py
import json
from raglab.offline.jsonio import write_json_atomic

def test_atomic_json_is_canonical_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "report.json"
    write_json_atomic(target, {"z": 1, "a": "한글"})
    assert target.read_text(encoding="utf-8") == '{\n  "a": "한글",\n  "z": 1\n}\n'
    assert list(tmp_path.glob("*.tmp")) == []
```

- [ ] **Step 2: Run the tests and verify the missing package failure**

Run: `python -m pytest tests/offline/test_cli.py tests/offline/test_jsonio.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'raglab'`.

- [ ] **Step 3: Add package metadata, models, canonical JSON, CLI routing, and PowerShell wrapper**

```toml
# pyproject.toml
[project]
name = "secure-multimodal-rag-lab"
version = "0.1.0"
requires-python = ">=3.11,<3.13"
dependencies = []

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

```python
# src/raglab/offline/models.py
from dataclasses import dataclass
from enum import StrEnum

class CheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"

@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: CheckStatus
    message: str
    action: str | None = None
```

```python
# src/raglab/offline/jsonio.py
import json
import os
from pathlib import Path
from typing import Mapping

def write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
```

```python
# src/raglab/cli.py
import argparse
from collections.abc import Sequence

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="raglab")
    parser.add_subparsers(dest="command", required=True)
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    return 0
```

```powershell
# raglab.ps1
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
& $python -m raglab @Arguments
exit $LASTEXITCODE
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python -m pytest tests/offline/test_cli.py tests/offline/test_jsonio.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Verify PowerShell 5.1 parses the wrapper**

Run: `powershell.exe -NoProfile -Command "$errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path '.\raglab.ps1'),[ref]$null,[ref]$errors); if($errors.Count){$errors; exit 1}"`

Expected: exit code 0 and no output.

- [ ] **Step 6: Commit the skeleton**

```bash
git add pyproject.toml raglab.ps1 src/raglab tests/offline/test_cli.py tests/offline/test_jsonio.py
git commit -m "feat: add offline toolkit CLI skeleton"
```

### Task 2: Approved Bundle Verification

**Files:**
- Create: `src/raglab/offline/ports.py`
- Create: `src/raglab/offline/bundle.py`
- Create: `src/raglab/offline/windows.py`
- Modify: `src/raglab/cli.py`
- Create: `tests/offline/test_bundle.py`
- Create: `tests/offline/test_verify_bundle_cli.py`
- Create: `tests/fixtures/bundle-valid/manifest.json`
- Create: `tests/fixtures/bundle-valid/approval.json`

**Interfaces:**
- Consumes: `write_json_atomic()` from Task 1.
- Produces: `CatalogVerifier.verify(catalog_path: Path) -> CatalogDecision`, `verify_bundle(root: Path, verifier: CatalogVerifier) -> BundleReport`, and CLI `verify-bundle --bundle PATH --report PATH`.

- [ ] **Step 1: Write failing integrity, approval, and CLI tests**

```python
# tests/offline/test_bundle.py
from pathlib import Path
from raglab.offline.bundle import verify_bundle
from raglab.offline.ports import CatalogDecision

class AcceptCatalog:
    def verify(self, catalog_path: Path) -> CatalogDecision:
        return CatalogDecision(valid=True, signer="CN=RagLab Test", reason=None)

def test_hash_mismatch_blocks_bundle(tmp_path):
    (tmp_path / "artifact.bin").write_bytes(b"changed")
    (tmp_path / "manifest.json").write_text(
        '{"schema_version":1,"artifacts":[{"path":"artifact.bin","sha256":"' + "0" * 64 + '","license_decision":"approved"}]}',
        encoding="utf-8",
    )
    (tmp_path / "approval.json").write_text(
        '{"approval_id":"APR-TEST","expires_at":"2099-01-01T00:00:00Z"}', encoding="utf-8"
    )
    (tmp_path / "bundle.cat").write_bytes(b"test catalog")
    report = verify_bundle(tmp_path, AcceptCatalog())
    assert report.status == "BLOCK"
    assert report.failures[0].code == "RAGLAB-BUNDLE-002"
```

- [ ] **Step 2: Run tests and verify the missing verifier failure**

Run: `python -m pytest tests/offline/test_bundle.py tests/offline/test_verify_bundle_cli.py -v`

Expected: FAIL with import errors for `CatalogDecision` and `verify_bundle`.

- [ ] **Step 3: Implement manifest validation and injectable catalog verification**

```python
# src/raglab/offline/ports.py
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

@dataclass(frozen=True)
class CatalogDecision:
    valid: bool
    signer: str | None
    reason: str | None

class CatalogVerifier(Protocol):
    def verify(self, catalog_path: Path) -> CatalogDecision: ...
```

```python
# src/raglab/offline/bundle.py
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from raglab.offline.ports import CatalogVerifier

@dataclass(frozen=True)
class BundleFailure:
    code: str
    artifact: str
    reason: str

@dataclass(frozen=True)
class BundleReport:
    status: str
    approval_id: str | None
    failures: tuple[BundleFailure, ...]

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def verify_bundle(root: Path, verifier: CatalogVerifier) -> BundleReport:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    approval = json.loads((root / "approval.json").read_text(encoding="utf-8"))
    failures: list[BundleFailure] = []
    if datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        failures.append(BundleFailure("RAGLAB-BUNDLE-004", "approval.json", "approval expired"))
    for artifact in manifest["artifacts"]:
        path = (root / artifact["path"]).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            failures.append(BundleFailure("RAGLAB-BUNDLE-001", artifact["path"], "missing or outside bundle"))
        elif _sha256(path) != artifact["sha256"]:
            failures.append(BundleFailure("RAGLAB-BUNDLE-002", artifact["path"], "hash mismatch"))
        elif artifact.get("license_decision") != "approved":
            failures.append(BundleFailure("RAGLAB-BUNDLE-005", artifact["path"], "license not approved"))
    catalog = verifier.verify(root / "bundle.cat")
    if not catalog.valid:
        failures.append(BundleFailure("RAGLAB-BUNDLE-003", "bundle.cat", catalog.reason or "invalid catalog"))
    return BundleReport("BLOCK" if failures else "PASS", approval.get("approval_id"), tuple(failures))
```

The production `PowerShellCatalogVerifier` invokes `powershell.exe -NoProfile -NonInteractive` with a constant script containing `Get-AuthenticodeSignature -LiteralPath $args[0] | Select-Object Status,@{n='Thumbprint';e={$_.SignerCertificate.Thumbprint}} | ConvertTo-Json -Compress`. It accepts only `Status = Valid` and a signer thumbprint listed in `approval.json`. The path is passed as an argument, never interpolated into script source. Development tests supply a deterministic fake.

- [ ] **Step 4: Add the CLI command and stable exit mapping**

Add `verify-bundle` arguments to `build_parser()` and route the report so `PASS` returns 0, an invalid operator path returns 2, approval failure returns 3, and any hash/catalog failure returns 4. The emitted report must contain paths relative to the bundle root, never absolute operator paths.

```python
verify = subparsers.add_parser("verify-bundle")
verify.add_argument("--bundle", type=Path, required=True)
verify.add_argument("--report", type=Path, default=Path("runs/verify-bundle.json"))
```

- [ ] **Step 5: Run the bundle tests**

Run: `python -m pytest tests/offline/test_bundle.py tests/offline/test_verify_bundle_cli.py -v`

Expected: all tests PASS, including missing artifact, traversal, expired approval, license denial, hash mismatch, invalid catalog, and valid bundle cases.

- [ ] **Step 6: Commit bundle verification**

```bash
git add src/raglab/offline/ports.py src/raglab/offline/bundle.py src/raglab/offline/windows.py src/raglab/cli.py tests/offline tests/fixtures/bundle-valid
git commit -m "feat: verify approved offline bundles"
```

### Task 3: Layered Configuration with Non-Weakenable Security Defaults

**Files:**
- Create: `src/raglab/offline/config.py`
- Create: `configs/security-defaults.json`
- Create: `configs/profiles/local16.json`
- Create: `configs/profiles/local32.json`
- Modify: `src/raglab/cli.py`
- Test: `tests/offline/test_config.py`
- Test: `tests/offline/test_config_cli.py`

**Interfaces:**
- Consumes: canonical JSON writer from Task 1.
- Produces: `load_effective_config(layers: Sequence[ConfigLayer]) -> EffectiveConfig`, `validate_config(value: Mapping[str, object]) -> tuple[ConfigIssue, ...]`, and CLI commands `config validate`, `config explain`, and `config diff`.

- [ ] **Step 1: Write failing tests for layer provenance and security downgrade rejection**

```python
from raglab.offline.config import ConfigLayer, load_effective_config

def test_run_layer_cannot_enable_network():
    layers = [
        ConfigLayer("security", {"network_mode": "deny", "max_working_set_gib": 12}, locked=frozenset({"network_mode"})),
        ConfigLayer("run", {"network_mode": "allow"}, locked=frozenset()),
    ]
    result = load_effective_config(layers)
    assert result.status == "BLOCK"
    assert result.issues[0].code == "RAGLAB-CONFIG-003"

def test_explain_records_source_layer():
    result = load_effective_config([
        ConfigLayer("security", {"network_mode": "deny"}, frozenset({"network_mode"})),
        ConfigLayer("local16", {"max_working_set_gib": 12}, frozenset()),
    ])
    assert result.provenance["max_working_set_gib"] == "local16"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `python -m pytest tests/offline/test_config.py tests/offline/test_config_cli.py -v`

Expected: FAIL with `ModuleNotFoundError: raglab.offline.config`.

- [ ] **Step 3: Implement explicit schema, merge rules, and profile files**

Use an allowlist rather than a permissive dictionary. The initial schema is exact:

```python
FIELD_RULES = {
    "network_mode": (str, {"deny"}),
    "profile": (str, {"local16", "local32"}),
    "max_working_set_gib": (int, range(1, 27)),
    "interactive_concurrency": (int, {1, 2}),
    "rerank_candidates": (int, range(1, 31)),
    "visual_candidates": (int, range(0, 6)),
    "temp_root": (str, None),
    "cache_root": (str, None),
    "output_root": (str, None),
    "diagnostic_content": (bool, {False}),
}
LOCKED_SECURITY_FIELDS = frozenset({"network_mode", "diagnostic_content"})
```

`local16.json` fixes 12 GiB, concurrency 1, 20 rerank candidates, and 3 visual candidates. `local32.json` fixes 26 GiB, concurrency 2, 30 rerank candidates, and 5 visual candidates. Unknown keys, wrong types, out-of-range values, relative traversal, or a weaker locked field produce `BLOCK`.

- [ ] **Step 4: Implement config CLI output**

```text
raglab config validate --profile local16 --run experiment.json
raglab config explain --profile local16 --run experiment.json --output runs/effective-config.json
raglab config diff --left runs/a/effective-config.json --right runs/b/effective-config.json
```

`explain` stores each value as `{value, source_layer, locked}` and redacts absolute user-profile prefixes. `diff` returns 0 for identical effective values and 2 for differences, printing deterministic JSON Patch-like rows.

- [ ] **Step 5: Run the configuration tests**

Run: `python -m pytest tests/offline/test_config.py tests/offline/test_config_cli.py -v`

Expected: all tests PASS for valid L16/L32, locked downgrade, unknown key, range violation, provenance, redaction, and diff exit codes.

- [ ] **Step 6: Commit configuration contracts**

```bash
git add src/raglab/offline/config.py src/raglab/cli.py configs tests/offline/test_config.py tests/offline/test_config_cli.py
git commit -m "feat: add locked offline configuration profiles"
```

### Task 4: Offline Doctor and Windows Probes

**Files:**
- Modify: `src/raglab/offline/windows.py`
- Create: `src/raglab/offline/doctor.py`
- Modify: `src/raglab/offline/ports.py`
- Modify: `src/raglab/cli.py`
- Test: `tests/offline/test_doctor.py`
- Test: `tests/offline/test_windows_probes.py`
- Test: `tests/offline/test_doctor_cli.py`

**Interfaces:**
- Consumes: `EffectiveConfig`, `BundleReport`, `CheckResult`, and atomic JSON output.
- Produces: `SystemProbe.snapshot() -> SystemSnapshot`, `EgressPolicyProbe.inspect() -> EgressPolicy`, `CatalogVerifier.verify()`, `run_doctor(context: DoctorContext) -> DoctorReport`, and CLI `doctor --profile PROFILE --offline`.

- [ ] **Step 1: Write failing aggregation and fail-closed tests**

```python
from raglab.offline.doctor import DoctorContext, run_doctor

class FakeSystem:
    def snapshot(self):
        return {"os": "Windows 11", "python": "3.11.9", "ram_gib": 16, "free_disk_gib": 80, "path_write": True}

class UnknownFirewall:
    def inspect(self):
        return {"status": "UNKNOWN", "reason": "policy query failed"}

def test_unknown_egress_policy_blocks_offline_doctor(valid_bundle_report, local16_config):
    report = run_doctor(DoctorContext(FakeSystem(), UnknownFirewall(), valid_bundle_report, local16_config))
    assert report.overall_status == "BLOCK"
    assert any(item.check_id == "OFFLINE_EGRESS_POLICY" and item.status == "BLOCK" for item in report.checks)
```

- [ ] **Step 2: Run tests and verify missing doctor failure**

Run: `python -m pytest tests/offline/test_doctor.py tests/offline/test_windows_probes.py tests/offline/test_doctor_cli.py -v`

Expected: FAIL because `DoctorContext` and Windows probes do not exist.

- [ ] **Step 3: Implement ports and deterministic doctor aggregation**

```python
class SystemProbe(Protocol):
    def snapshot(self) -> SystemSnapshot: ...

class EgressPolicyProbe(Protocol):
    def inspect(self) -> EgressPolicy: ...

@dataclass(frozen=True)
class DoctorReport:
    schema_version: int
    run_id: str
    profile: str
    overall_status: str
    checks: tuple[CheckResult, ...]
```

Checks execute in this order: bundle approval, OS and Python range, physical/available RAM, free disk, writable temp/cache/output, long/unicode path round-trip, catalog signer, DLL dry-load inventory, passive firewall/proxy state, model inventory without model load, synthetic coordinate round-trip, atomic-write cleanup, and derivative deletion smoke test. A required check exception becomes `BLOCK`, never `WARN`.

- [ ] **Step 4: Implement PowerShell 5.1-safe Windows adapters**

`WindowsSystemProbe` calls `powershell.exe -NoProfile -NonInteractive -Command` with a checked-in constant script, parses JSON from `ConvertTo-Json -Compress`, enforces a 10-second timeout, and never interpolates document paths into script source. `PowerShellCatalogVerifier` validates `Status`, signer thumbprint, and catalog path. `WindowsEgressPolicyProbe` inspects active firewall profiles, outbound defaults, proxy environment variables, WinHTTP proxy, and configured security-owned canary metadata without sending a packet.

The active canary is a separate adapter unavailable from the default doctor. It requires all of `--active-egress-canary`, `--approval-id`, and a canary endpoint already present in the signed site profile. Its test double verifies failure semantics; actual company execution belongs to the security acceptance runbook.

- [ ] **Step 5: Add doctor CLI and reports**

Run shape:

```powershell
.\raglab.ps1 doctor --bundle .\approved-bundle --profile local16 --offline --report .\runs\doctor.json
```

Expected console contract:

```text
PASS  BUNDLE_APPROVAL       approval APR-2026-0042 is valid
PASS  RESOURCE_PROFILE      local16: 16.0 GiB physical, 12.0 GiB process cap
BLOCK OFFLINE_EGRESS_POLICY outbound policy could not be proven
ACTION Review runs\doctor.json; execution remains blocked.
```

The CLI returns 5 for any BLOCK and 0 otherwise. WARN is permitted only for non-security issues such as power-plan performance.

- [ ] **Step 6: Run doctor and probe tests**

Run: `python -m pytest tests/offline/test_doctor.py tests/offline/test_windows_probes.py tests/offline/test_doctor_cli.py -v`

Expected: all tests PASS, including subprocess timeout, malformed PowerShell JSON, signer mismatch, firewall query failure, low disk, unicode path round-trip, and valid local16/local32 snapshots.

- [ ] **Step 7: Commit the doctor**

```bash
git add src/raglab/offline/ports.py src/raglab/offline/windows.py src/raglab/offline/doctor.py src/raglab/cli.py tests/offline
git commit -m "feat: add fail-closed Windows offline doctor"
```

### Task 5: Stable Errors, Progress, and Redacted Diagnostics

**Files:**
- Create: `src/raglab/offline/errors.py`
- Create: `src/raglab/offline/progress.py`
- Modify: `src/raglab/cli.py`
- Create: `tests/offline/test_errors.py`
- Create: `tests/offline/test_progress.py`
- Create: `tests/offline/test_redaction.py`

**Interfaces:**
- Consumes: atomic JSON writer.
- Produces: `OperatorError`, `ProgressEvent`, `Redactor.redact(value: object) -> object`, and `JsonlProgressSink.emit(event: ProgressEvent) -> None`.

- [ ] **Step 1: Write failing schema and leakage tests**

```python
from raglab.offline.errors import OperatorError
from raglab.offline.progress import Redactor

def test_error_contains_next_action_and_retry_safety():
    error = OperatorError(
        code="RAGLAB-MEM-002", stage="demo", summary="profile memory cap exceeded",
        observed="13.1 GiB", expected="<=12 GiB", protected_state="no index changed",
        retry_safe=True, next_command=".\\raglab.ps1 demo --profile local16",
        docs="docs/operator/ERROR-CATALOG.md#raglab-mem-002",
    )
    assert error.next_command.startswith(".\\raglab.ps1")

def test_redactor_removes_forbidden_payload_fields():
    redacted = Redactor().redact({"document_name": "secret.pdf", "ocr_text": "secret", "run_id": "run-1"})
    assert redacted == {"document_name": "[REDACTED]", "ocr_text": "[REDACTED]", "run_id": "run-1"}
```

- [ ] **Step 2: Run tests and verify missing modules**

Run: `python -m pytest tests/offline/test_errors.py tests/offline/test_progress.py tests/offline/test_redaction.py -v`

Expected: FAIL with missing modules.

- [ ] **Step 3: Implement immutable error and progress contracts**

```python
FORBIDDEN_FIELDS = frozenset({
    "document_name", "document_path", "document_text", "ocr_text",
    "question", "crop", "embedding", "secret", "token",
})

@dataclass(frozen=True)
class ProgressEvent:
    schema_version: int
    run_id: str
    sequence: int
    stage: str
    completed: int
    total: int
    elapsed_ms: int
    peak_working_set_bytes: int
    retry_count: int
    status: str
```

The JSONL sink writes one canonical object per line under a lock, requires strictly increasing sequence numbers, rejects unknown fields, and redacts recursively before serialization. Console output uses opaque document IDs only.

- [ ] **Step 4: Add stable CLI exception mapping**

The top-level CLI catches only known `OperatorError` subclasses plus a final internal boundary. It prints the stable code, impact, next command, and diagnostics path. Unexpected exceptions return 10 and store a traceback only after passing it through the redactor; locals and source document values are never captured.

- [ ] **Step 5: Run error/progress tests**

Run: `python -m pytest tests/offline/test_errors.py tests/offline/test_progress.py tests/offline/test_redaction.py -v`

Expected: all tests PASS, including nested leakage, absolute-user-path redaction, sequence regression, concurrent writes, and internal exception mapping.

- [ ] **Step 6: Commit operator diagnostics**

```bash
git add src/raglab/offline/errors.py src/raglab/offline/progress.py src/raglab/cli.py tests/offline
git commit -m "feat: add redacted operator diagnostics"
```

### Task 6: Synthetic Evidence Demo and Offline Report

**Files:**
- Create: `src/raglab/offline/demo.py`
- Create: `fixtures/synthetic-mini/source.svg`
- Create: `fixtures/synthetic-mini/expected.json`
- Create: `tests/fixtures/doctor-pass.json`
- Create: `tests/offline/test_demo.py`
- Create: `tests/offline/test_demo_cli.py`
- Modify: `src/raglab/cli.py`

**Interfaces:**
- Consumes: a PASS `DoctorReport`, effective profile, error/progress contracts, and atomic JSON output.
- Produces: `run_synthetic_demo(context: DemoContext) -> DemoResult` and CLI `demo --profile PROFILE --doctor-report PATH`.

- [ ] **Step 1: Create the harmless fixture and failing evidence test**

```xml
<!-- fixtures/synthetic-mini/source.svg -->
<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="600" viewBox="0 0 1000 600">
  <rect width="1000" height="600" fill="white"/>
  <rect id="part-a" x="600" y="180" width="240" height="180" fill="#d9eafd" stroke="#174a7e"/>
  <text x="630" y="275" font-family="Arial" font-size="48">PART A</text>
</svg>
```

```python
def test_demo_returns_reproducible_part_a_region(passing_doctor, tmp_path):
    result = run_synthetic_demo(DemoContext(passing_doctor, tmp_path))
    assert result.answer == "PART A는 합성 도면 오른쪽에 있습니다."
    assert result.regions[0].bbox == (0.6, 0.3, 0.24, 0.3)
    assert result.regions[0].artifact_hash == result.artifact_hash
    assert (tmp_path / "overlay.svg").is_file()
```

- [ ] **Step 2: Run tests and verify the missing demo failure**

Run: `python -m pytest tests/offline/test_demo.py tests/offline/test_demo_cli.py -v`

Expected: FAIL with missing `run_synthetic_demo`.

- [ ] **Step 3: Implement deterministic demo and safe SVG overlay**

The demo parses checked-in `expected.json`; it does not perform OCR, model inference, or arbitrary XML execution. Parse with `xml.etree.ElementTree`, reject DTD/entity declarations by byte pre-check, compute the source SHA-256 once and propagate that exact value into the artifact and every region, validate normalized coordinates, and create a new overlay SVG from fixed templates rather than copying untrusted markup. Bundle verification protects the checked-in fixture from replacement.

```python
@dataclass(frozen=True)
class DemoResult:
    schema_version: int
    run_id: str
    answer: str
    artifact_hash: str
    regions: tuple[DemoRegion, ...]
    overlay_path: str
    elapsed_ms: int
```

The output directory contains `result.json`, `overlay.svg`, `summary.html`, `events.jsonl`, and `deletion-smoke-report.json`. `summary.html` escapes every value and has no script, remote link, font, or image reference.

- [ ] **Step 4: Enforce doctor and artifact gates in CLI**

`demo` rejects a missing, stale, profile-mismatched, or BLOCK doctor report with exit 5. A fixture hash or evidence mismatch returns exit 6. The CLI prints one answer and relative result paths only.

- [ ] **Step 5: Run demo tests and an end-to-end synthetic command**

Run: `python -m pytest tests/offline/test_demo.py tests/offline/test_demo_cli.py -v`

Expected: all tests PASS.

Run: `python -m raglab demo --profile local16 --doctor-report tests/fixtures/doctor-pass.json --output runs/synthetic-demo`

Expected: exit 0, `PART A는 합성 도면 오른쪽에 있습니다.`, and five output files with no external URLs.

- [ ] **Step 6: Commit the synthetic demo**

```bash
git add src/raglab/offline/demo.py src/raglab/cli.py fixtures/synthetic-mini tests/offline
git commit -m "feat: add deterministic evidence demo"
```

### Task 7: Run Manifest and Non-Content Repro Bundle

**Files:**
- Create: `src/raglab/offline/repro.py`
- Modify: `src/raglab/cli.py`
- Test: `tests/offline/test_repro.py`
- Test: `tests/offline/test_reproduce_cli.py`

**Interfaces:**
- Consumes: bundle report, effective configuration, doctor report, demo result, environment snapshot, progress, and deletion report.
- Produces: `build_repro_bundle(inputs: ReproInputs, output: Path) -> ReproReport`, `verify_repro_bundle(path: Path) -> ReproReport`, and CLI `reproduce --bundle PATH --verify-only`.

- [ ] **Step 1: Write failing completeness, leakage, and tamper tests**

```python
from raglab.offline.repro import build_repro_bundle, verify_repro_bundle

def test_repro_bundle_contains_only_allowed_files(repro_inputs, tmp_path):
    report = build_repro_bundle(repro_inputs, tmp_path)
    assert report.status == "PASS"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "approvals.snapshot.json", "artifact-lock.json", "dataset-manifest.json",
        "deletion-report.json", "environment.json", "errors.jsonl",
        "effective-config.redacted.json", "manifest.sha256", "metrics.json",
        "network-check.json", "pipeline-manifest.json", "run-manifest.json",
    ]
    assert b"secret.pdf" not in b"".join(path.read_bytes() for path in tmp_path.iterdir())

def test_modified_repro_file_is_rejected(valid_repro_bundle):
    (valid_repro_bundle / "metrics.json").write_text("{}", encoding="utf-8")
    assert verify_repro_bundle(valid_repro_bundle).status == "BLOCK"
```

- [ ] **Step 2: Run tests and verify missing repro implementation**

Run: `python -m pytest tests/offline/test_repro.py tests/offline/test_reproduce_cli.py -v`

Expected: FAIL with missing repro functions.

- [ ] **Step 3: Implement allowlisted bundle creation and verification**

`build_repro_bundle` rejects any source field not in a fixed allowlist, recursively scans output values for forbidden keys, writes to a staging directory, computes SHA-256 for every file, writes `manifest.sha256`, then atomically renames the directory. It refuses to overwrite an existing bundle. `verify_repro_bundle` rejects extra files, missing files, hash mismatches, unsupported schema versions, or absolute document paths.

`dataset-manifest.json` contains only dataset ID, version, question count, and a manifest hash. It never contains file names or content hashes that a reviewer has not approved for export.

- [ ] **Step 4: Implement verification and environment diff CLI**

```text
raglab reproduce --bundle runs/run-0042/repro --verify-only
raglab reproduce --left runs/run-0042/repro --right runs/run-0043/repro --diff-env
```

Verify-only returns 0 on a valid bundle and 7 on any mismatch. Environment diff reports OS build, CPU, RAM tier, Python, artifact lock, effective config hash, locale, timezone, and power plan without emitting user names or absolute paths.

- [ ] **Step 5: Run repro tests**

Run: `python -m pytest tests/offline/test_repro.py tests/offline/test_reproduce_cli.py -v`

Expected: all tests PASS, including symlink rejection, unknown file, traversal, tamper, sensitive-key scan, deterministic hashes, and environment diff.

- [ ] **Step 6: Commit reproducibility support**

```bash
git add src/raglab/offline/repro.py src/raglab/cli.py tests/offline
git commit -m "feat: emit verifiable non-content repro bundles"
```

### Task 8: Offline Install, Clean-VM Acceptance, and Operator Documentation

**Files:**
- Create: `scripts/offline/install.ps1`
- Create: `scripts/offline/uninstall.ps1`
- Create: `scripts/offline/test-clean-vm.ps1`
- Create: `tests/offline/test_powershell_scripts.py`
- Create: `tests/offline/test_operator_docs.py`
- Create: `docs/operator/START-HERE.md`
- Create: `docs/operator/OFFLINE-INSTALL.md`
- Create: `docs/operator/CONFIGURATION.md`
- Create: `docs/operator/ERROR-CATALOG.md`
- Create: `docs/operator/REPRODUCIBILITY.md`
- Create: `docs/operator/SECURITY-REVIEW.md`

**Interfaces:**
- Consumes: all commands from Tasks 1-7.
- Produces: idempotent install/repair/uninstall scripts, a clean-VM acceptance report, and role-specific offline documentation.

- [ ] **Step 1: Write failing PowerShell and documentation contract tests**

```python
# tests/offline/test_operator_docs.py
from pathlib import Path

def test_start_here_contains_copyable_golden_path():
    text = Path("docs/operator/START-HERE.md").read_text(encoding="utf-8")
    for command in ("verify-bundle", "doctor --profile local16 --offline", "demo --profile local16"):
        assert command in text
    assert "30분" in text

def test_error_catalog_lists_every_stable_prefix():
    text = Path("docs/operator/ERROR-CATALOG.md").read_text(encoding="utf-8")
    for code in ("RAGLAB-BUNDLE-", "RAGLAB-CONFIG-", "RAGLAB-DOCTOR-", "RAGLAB-MEM-", "RAGLAB-REPRO-"):
        assert code in text
```

- [ ] **Step 2: Run tests and verify missing artifacts**

Run: `python -m pytest tests/offline/test_powershell_scripts.py tests/offline/test_operator_docs.py -v`

Expected: FAIL because scripts and documents do not exist.

- [ ] **Step 3: Implement idempotent standard-user install and removal**

`install.ps1` accepts `-BundlePath`, `-InstallRoot`, and `-Mode Install|Repair`. It rejects admin-only destinations, verifies the catalog and hashes before creating files, creates `.venv` with an approved embedded/system Python 3.11, and writes a `.pth` file in that environment pointing to the verified local `src` directory. It does not invoke pip or a package index. It then runs doctor and writes `install-report.json`. Re-running with the same artifact lock is a no-op; drift enters Repair and re-verifies every file.

`uninstall.ps1` accepts `-InstallRoot`, `-DataRoot`, and `-RetentionPolicy`. It first exports a non-content manifest, stops new runs, removes cache/temp/demo/repro artifacts according to policy, verifies negative lookup, preserves only approved content-free audit receipts, and writes `uninstall-report.json`. It never recursively deletes an unresolved, root, home, or workspace path.

- [ ] **Step 4: Implement the clean-VM acceptance harness**

The harness executes under a standard user in a disposable supported Windows VM and records:

```text
install -> verify-bundle -> doctor local16 -> demo -> reproduce --verify-only
-> repair -> reboot/resume check -> uninstall -> negative lookup
```

It creates and tests paths containing spaces, Korean, Japanese, and a 220-character segment; confirms no administrator token; captures Windows build, Defender/EDR state, elapsed time, peak working set, and file inventory; and fails if any command requests network package installation. The security-owned active egress canary is a separate opt-in step requiring `-ApprovalId` and `-CanaryEndpoint` from the signed site profile.

- [ ] **Step 5: Write operator and reviewer documents**

`START-HERE.md` contains only prerequisites, approval check, the three-command golden path, expected elapsed time/output, and the first troubleshooting link. The remaining documents define exact supported Windows/Python/PowerShell versions, disk estimates, profile selection, command exits, stable errors, run/repro schemas, sensitive-field exclusions, active-canary limitations, install repair, rollback, and deletion evidence.

- [ ] **Step 6: Run all toolkit tests and static policy scans**

Run: `python -m pytest tests/offline -v`

Expected: all offline toolkit tests PASS.

Run: `python -m pytest tests/offline/test_powershell_scripts.py -v`

Expected: PowerShell 5.1 parser, unresolved-variable, destructive-target guard, and network-install-pattern tests PASS.

Run: `rg -n "Invoke-WebRequest|Invoke-RestMethod|Start-BitsTransfer|pip install .*https?://|document_text|ocr_text" scripts src docs/operator`

Expected: no network-download commands; `document_text` and `ocr_text` occur only in the redaction denylist and security documentation.

- [ ] **Step 7: Execute synthetic clean-VM acceptance**

Run from an approved disposable VM: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\test-clean-vm.ps1 -BundlePath D:\approved-bundle -Profile local16 -OutputRoot C:\RagLabAcceptance`

Expected: exit 0; `acceptance-report.json` records PASS for non-admin install, bundle verification, doctor, demo, repro verification, repair, restart check, uninstall, and negative lookup. The active egress canary remains `NOT_RUN_REQUIRES_SECURITY_APPROVAL` unless its explicit approval parameters are supplied.

- [ ] **Step 8: Commit the offline handoff**

```bash
git add scripts/offline tests/offline docs/operator
git commit -m "docs: add Windows offline operator handoff"
```

## Final Verification

- [ ] Run `python -m pytest tests/offline -v` and record the exact pass count.
- [ ] Run `python -m raglab verify-bundle --bundle tests/fixtures/bundle-valid --report runs/final-verify.json`; expect exit 0.
- [ ] Run `python -m raglab doctor --bundle tests/fixtures/bundle-valid --profile local16 --offline --report runs/final-doctor.json`; expect exit 0 on the supported Windows test host.
- [ ] Run `python -m raglab demo --profile local16 --doctor-report runs/final-doctor.json --output runs/final-demo`; expect exit 0 and a valid overlay.
- [ ] Run `python -m raglab reproduce --bundle runs/final-demo/repro --verify-only`; expect exit 0.
- [ ] Run the clean-VM harness as a standard user and attach its non-content acceptance report to the Phase 0 approval package.
- [ ] Confirm `git status --short` contains only the intended implementation files before the final implementation commit.

## Self-Review Result

- Spec coverage: bundle intake, `verify-bundle`, offline doctor, locked L16/L32 configuration, stable errors, progress, synthetic evidence, repro bundle, clean-VM/non-admin/unicode/long-path testing, passive and approval-gated active network checks, install/repair/uninstall, and role-specific handoff are each assigned to a testable task.
- Placeholder scan: the plan contains no deferred implementation markers; company-owned signer thumbprints, approval IDs, and endpoints enter only through signed approval manifests and are never guessed in source.
- Type consistency: `CheckResult`, `BundleReport`, `EffectiveConfig`, `DoctorReport`, `DemoResult`, and `ReproReport` flow in dependency order and are not renamed between tasks.

Plan complete and saved to `docs/superpowers/plans/2026-11-24-windows-offline-toolkit.md`.
