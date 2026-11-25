"""Build and verify local, non-content reproducibility bundles.

The SHA-256 inventory checks bundle consistency. It provides no external
authentication or attestation of the run and its inputs.
"""

import hashlib
import json
import locale
import os
import re
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from raglab.contracts.errors import ErrorCode
from raglab.offline.bundle import BundleReport
from raglab.offline.config import FIELD_RULES, EffectiveConfig, validate_config
from raglab.offline.demo import DemoRegion, DemoResult
from raglab.offline.doctor import ACTIVE_EGRESS_CHECK, DoctorReport, validate_pass_report
from raglab.offline.jsonio import serialize_json
from raglab.offline.models import CheckStatus
from raglab.offline.progress import ProgressEvent, Redactor
from raglab.offline.windows_paths import PinnedPath, WindowsLocalPaths


@dataclass(frozen=True, slots=True)
class ApprovedDatasetManifest:
    """Export-approved dataset summary.

    The caller independently approves the dataset ID, version and aggregate
    manifest digest for export. Do not supply a source file digest here.
    """

    dataset_id: str
    version: str
    question_count: int
    manifest_hash: str


@dataclass(frozen=True, slots=True)
class ReproInputs:
    """Reports from one synthetic run and its approved environment metadata.

    Environment uses normalized Windows build, CPU architecture, RAM profile,
    Python version, locale, UTC offset, and a standard power plan category.
    """

    bundle_report: BundleReport
    config: EffectiveConfig
    doctor_report: DoctorReport
    demo_result: DemoResult
    environment: Mapping[str, object]
    progress: tuple[ProgressEvent, ...]
    deletion_report: Mapping[str, object]
    dataset: ApprovedDatasetManifest
    errors: tuple[ErrorCode, ...] = ()


@dataclass(frozen=True, slots=True)
class ReproReport:
    status: CheckStatus
    environment: dict[str, str] | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.status == CheckStatus.PASS else 7


_MANIFEST = "manifest.sha256"
_ERROR_LOG = "errors.jsonl"
_DOCUMENT_LIMIT = 64 * 1024
_MAX_ERROR_ROWS = 256
_MAX_PROGRESS_ROWS = 2048
_CONFIG_SOURCE_FIELDS = frozenset(FIELD_RULES)
_CONFIG_FIELDS = frozenset({
    "network_mode", "profile", "max_working_set_gib", "interactive_concurrency",
    "rerank_candidates", "visual_candidates", "diagnostic_content",
})
_LOCALES = frozenset(value.replace("_", "-") for value in locale.windows_locale.values())
_ERROR_CODES = frozenset(code.value for code in ErrorCode)


def _pattern(expression: str) -> Callable[[object], bool]:
    pattern = re.compile(expression, re.ASCII)
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _one_of(*options: str) -> Callable[[object], bool]:
    return lambda value: type(value) is str and value in options


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**64


def _v1(value: object) -> bool:
    return type(value) is int and value == 1


def _diagnostic_disabled(value: object) -> bool:
    return value is False


_DIGEST = _pattern("[0-9a-f]{64}")
_RUN_ID = _pattern("[0-9a-f]{32}")
_EXPORT_ID = _pattern("[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_PASS = _one_of("PASS")
_PROFILE = _one_of("local16", "local32")
_SCHEMAS: dict[str, dict[str, Callable[[object], bool]]] = {
    "approvals.snapshot.json": {
        "schema_version": _v1, "approval_id": _EXPORT_ID, "status": _PASS,
    },
    "artifact-lock.json": {"schema_version": _v1, "generation_fingerprint": _DIGEST},
    "dataset-manifest.json": {
        "dataset_id": _EXPORT_ID, "version": _pattern(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}"),
        "question_count": _count, "manifest_hash": _DIGEST,
    },
    "deletion-report.json": {"schema_version": _v1, "run_id": _RUN_ID, "status": _PASS},
    "effective-config.redacted.json": {
        "schema_version": _v1, "network_mode": _one_of("deny"), "profile": _PROFILE,
        "max_working_set_gib": _count, "interactive_concurrency": _count,
        "rerank_candidates": _count, "visual_candidates": _count, "diagnostic_content": _diagnostic_disabled,
    },
    "environment.json": {
        "schema_version": _v1, "os_build": _pattern(r"10\.0\.[0-9]{4,6}(?:\.[0-9]{1,6})?"),
        "cpu": _one_of("x64", "arm64", "x86"), "ram_tier": _PROFILE,
        "python": _pattern(r"3\.(?:11|12)\.[0-9]{1,3}"),
        "locale": lambda value: type(value) is str and value in _LOCALES,
        "timezone": _pattern(r"(?:[+-](?:0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00)"),
        "power_plan": _one_of("balanced", "high_performance", "power_saver", "other", "unknown"),
    },
    "metrics.json": {
        "schema_version": _v1, "elapsed_ms": _count, "peak_working_set_bytes": _count,
        "retry_count": _count, "progress_events": _count, "completed": _count, "total": _count,
    },
    "network-check.json": {
        "schema_version": _v1, "status": _PASS, "active_canary": _one_of("PASS", "NOT_REQUESTED"),
    },
    "pipeline-manifest.json": {"schema_version": _v1, "pipeline": _one_of("synthetic_demo")},
    "run-manifest.json": {
        "schema_version": _v1, "run_id": _RUN_ID, "doctor_run_id": _RUN_ID,
        "profile": _PROFILE, "status": _PASS,
    },
}
_FILES = frozenset(_SCHEMAS) | {_ERROR_LOG, _MANIFEST}


def _safe_values(value: object) -> bool:
    return Redactor().redact(value) == value


def _validate_document(name: str, value: object) -> dict:
    schema = _SCHEMAS[name]
    if (
        type(value) is not dict or value.keys() != schema.keys()
        or any(not check(value[key]) for key, check in schema.items())
        or not _safe_values(value)
    ):
        raise ValueError("invalid export document")
    return value


def _has_fields(value: object, expected: set[str] | frozenset[str]) -> bool:
    return isinstance(value, Mapping) and value.keys() == expected


def _require_original_fields(value: object, expected: type) -> None:
    if type(value) is not expected or vars(value).keys() != expected.__dataclass_fields__.keys():
        raise ValueError("unexpected source report fields")


def _snapshot_contract(documents: dict[str, dict]) -> None:
    run = documents["run-manifest.json"]
    metrics = documents["metrics.json"]
    settings = {key: documents["effective-config.redacted.json"][key] for key in _CONFIG_FIELDS}
    # Only the public non-path settings participate in the config digest.
    effective = {**settings, "temp_root": ".", "cache_root": ".", "output_root": "."}
    if (
        run["run_id"] != documents["deletion-report.json"]["run_id"]
        or run["profile"] != settings["profile"]
        or validate_config(effective)
        or not 0 < metrics["progress_events"] <= _MAX_PROGRESS_ROWS
        or metrics["completed"] != metrics["total"]
    ):
        raise ValueError("inconsistent export documents")


def _metrics(events: tuple[ProgressEvent, ...], demo: DemoResult) -> dict:
    if type(events) is not tuple or not 0 < len(events) <= _MAX_PROGRESS_ROWS:
        raise ValueError("invalid progress summary")
    previous = -1
    for event in events:
        _require_original_fields(event, ProgressEvent)
        if (
            not _v1(event.schema_version) or event.run_id != demo.run_id
            or event.stage != "synthetic_demo" or event.status != "PASS"
            or not all(_count(value) for value in (
                event.sequence, event.completed, event.total, event.elapsed_ms,
                event.peak_working_set_bytes, event.retry_count,
            ))
            or event.completed > event.total or event.sequence <= previous
        ):
            raise ValueError("invalid progress row")
        previous = event.sequence
    end = events[-1]
    if end.elapsed_ms != demo.elapsed_ms:
        raise ValueError("progress does not match demo timing")
    return {
        "schema_version": 1, "elapsed_ms": end.elapsed_ms,
        "peak_working_set_bytes": max(event.peak_working_set_bytes for event in events),
        "retry_count": end.retry_count, "progress_events": len(events),
        "completed": end.completed, "total": end.total,
    }


def _error_line(code: object) -> bytes:
    if type(code) is not str or code not in _ERROR_CODES:
        raise ValueError("invalid typed error code")
    return (f'{{"code":"{code}","schema_version":1}}\n').encode("ascii")


def _export(inputs: ReproInputs) -> dict[str, bytes]:
    if type(inputs) is not ReproInputs or type(inputs.dataset) is not ApprovedDatasetManifest:
        raise ValueError("approved repro inputs required")
    bundle, config, doctor, demo = inputs.bundle_report, inputs.config, inputs.doctor_report, inputs.demo_result
    _require_original_fields(bundle, BundleReport)
    _require_original_fields(config, EffectiveConfig)
    _, _, checks = validate_pass_report(doctor)
    _require_original_fields(demo, DemoResult)
    if (
        bundle.status != "PASS" or bundle.failures != ()
        or config.status != "PASS" or config.issues != ()
        or not _has_fields(config.values, _CONFIG_SOURCE_FIELDS)
        or not _has_fields(config.provenance, _CONFIG_SOURCE_FIELDS)
        or type(config.locked) is not frozenset or not config.locked <= _CONFIG_SOURCE_FIELDS
        or validate_config(config.values)
        or type(demo.regions) is not tuple or len(demo.regions) > 32
        or not _v1(demo.schema_version) or doctor.generation_fingerprint != bundle.generation_fingerprint
        or not _count(demo.elapsed_ms)
        or not _has_fields(inputs.environment, _SCHEMAS["environment.json"].keys() - {"schema_version"})
        or not isinstance(inputs.deletion_report, Mapping)
    ):
        raise ValueError("inconsistent source reports")
    for region in demo.regions:
        _require_original_fields(region, DemoRegion)
    dataset = inputs.dataset
    documents = {
        "approvals.snapshot.json": {"schema_version": 1, "approval_id": bundle.approval_id, "status": "PASS"},
        "artifact-lock.json": {"schema_version": 1, "generation_fingerprint": bundle.generation_fingerprint},
        "dataset-manifest.json": {
            "dataset_id": dataset.dataset_id, "version": dataset.version,
            "question_count": dataset.question_count, "manifest_hash": dataset.manifest_hash,
        },
        "deletion-report.json": dict(inputs.deletion_report),
        "effective-config.redacted.json": {
            "schema_version": 1, **{key: config.values[key] for key in _CONFIG_FIELDS},
        },
        "environment.json": {**inputs.environment, "schema_version": 1},
        "metrics.json": _metrics(inputs.progress, demo),
        "network-check.json": {
            "schema_version": 1, "status": "PASS",
            "active_canary": "PASS" if ACTIVE_EGRESS_CHECK in checks else "NOT_REQUESTED",
        },
        "pipeline-manifest.json": {"schema_version": 1, "pipeline": "synthetic_demo"},
        "run-manifest.json": {
            "schema_version": 1, "run_id": demo.run_id, "doctor_run_id": doctor.run_id,
            "profile": doctor.profile, "status": "PASS",
        },
    }
    for name, document in documents.items():
        _validate_document(name, document)
    _snapshot_contract(documents)
    if (
        type(inputs.errors) is not tuple or len(inputs.errors) > _MAX_ERROR_ROWS
        or any(type(code) is not ErrorCode for code in inputs.errors)
    ):
        raise ValueError("unexpected errors")
    output = {name: serialize_json(document).encode("utf-8") for name, document in documents.items()}
    output[_ERROR_LOG] = b"".join(_error_line(code.value) for code in inputs.errors)
    return output


def _manifest(files: Mapping[str, bytes]) -> bytes:
    return "".join(
        f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n" for name in sorted(files)
    ).encode("ascii")


def _decode_document(name: str, content: bytes) -> dict:
    value = _validate_document(name, json.loads(content))
    if serialize_json(value).encode("utf-8") != content:
        raise ValueError("noncanonical export document")
    return value


def _validate_errors(content: bytes) -> None:
    lines = content.splitlines(keepends=True)
    if len(lines) > _MAX_ERROR_ROWS:
        raise ValueError("too many error rows")
    for line in lines:
        row = json.loads(line)
        if type(row) is not dict or _error_line(row.get("code")) != line:
            raise ValueError("invalid error row")


def _local_path(paths: WindowsLocalPaths, path: Path) -> Path:
    if ".." in Path(path).parts or not Path(path).name:
        raise ValueError("invalid repro path")
    return paths.validate(path)


def _snapshot(
    paths: WindowsLocalPaths, root: PinnedPath, locks: ExitStack,
) -> tuple[dict[str, bytes], Callable[[], None]]:
    if not root.info.is_directory:
        raise ValueError("bundle must be a directory")
    unchanged = locks.enter_context(paths.watch_namespace(root))
    with os.scandir(root.path) as entries:
        names = set()
        for entry in entries:
            if entry.name not in _FILES:
                raise ValueError("unexpected bundle member")
            names.add(entry.name)
    if names != _FILES:
        raise ValueError("missing bundle member")
    files = {}
    for name in sorted(names):
        member = locks.enter_context(paths.pin_entry(root.path / name))
        if member.info.link_count != 1 or member.path.name != name:
            raise ValueError("aliased bundle member")
        files[name] = member.read_bytes(_DOCUMENT_LIMIT)
    return files, unchanged


def _verify_files(files: dict[str, bytes]) -> ReproReport:
    manifest = files.pop(_MANIFEST)
    if manifest != _manifest(files):
        raise ValueError("bundle digest mismatch")
    _validate_errors(files[_ERROR_LOG])
    documents = {name: _decode_document(name, files[name]) for name in _SCHEMAS}
    _snapshot_contract(documents)
    environment = {key: value for key, value in documents["environment.json"].items() if key != "schema_version"}
    environment["artifact_lock"] = documents["artifact-lock.json"]["generation_fingerprint"]
    environment["effective_config_hash"] = hashlib.sha256(files["effective-config.redacted.json"]).hexdigest()
    return ReproReport(CheckStatus.PASS, environment)


def verify_repro_bundle(path: Path) -> ReproReport:
    """Check the fixed inventory, content schemas, and local SHA-256 consistency."""
    paths = WindowsLocalPaths()
    try:
        with ExitStack() as locks:
            root = locks.enter_context(paths.pin(_local_path(paths, path), reject_aliases=True))
            files, unchanged = _snapshot(paths, root, locks)
            report = _verify_files(files)
            unchanged()
            return report
    except (OSError, RuntimeError, TypeError, ValueError):
        return ReproReport(CheckStatus.BLOCK)


def build_repro_bundle(inputs: ReproInputs, output: Path) -> ReproReport:
    """Publish a complete bundle at a new local directory."""
    paths = WindowsLocalPaths()
    try:
        files = _export(inputs)
        files[_MANIFEST] = _manifest(files)
        report = _verify_files(files.copy())
        destination = _local_path(paths, output)
        with paths.pin(
            destination.parent, create_directories=True, directory_write_sharing=True, reject_aliases=True,
        ) as parent:
            paths.publish_new_directory(parent, destination.name, files)
        return report
    except (OSError, RuntimeError, TypeError, ValueError):
        return ReproReport(CheckStatus.BLOCK)
