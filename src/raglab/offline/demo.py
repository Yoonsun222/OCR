"""Offline evidence demonstration for the two pinned synthetic fixture files."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
from time import monotonic_ns
from uuid import uuid4
import xml.etree.ElementTree as ET

from raglab.contracts.evidence import BBox
from raglab.offline.config import CONFIG_DIRECTORY, PROFILES, EffectiveConfig, load_profile_config
from raglab.offline.doctor import DoctorReport
from raglab.offline.errors import OperatorError
from raglab.offline.jsonio import serialize_json
from raglab.offline.models import CheckResult, CheckStatus
from raglab.offline.progress import ProgressEvent, serialize_progress_event
from raglab.offline.windows_names import windows_name_compare
from raglab.offline.windows_paths import WindowsLocalPaths


FIXTURE_DIRECTORY = Path(__file__).parents[3] / "fixtures" / "synthetic-mini"
FIXTURE_HASHES = (
    ("source.svg", "1912fd9ed4d810437bd151852a8820b812396f1025c09a232c89425b2c726b59"),
    ("expected.json", "8b7b1f4d0bb5af4d122e6f436b287ba30144aff6d583aebb98bb2d17f3030285"),
)
DEMO_OUTPUTS = (
    "result.json", "overlay.svg", "summary.html", "events.jsonl", "deletion-smoke-report.json",
)
_REQUIRED_CHECKS = frozenset({
    "BUNDLE_APPROVAL", "OS_PYTHON", "RESOURCE_PROFILE", "FREE_DISK", "WRITABLE_PATHS",
    "LONG_UNICODE_PATH", "CATALOG_SIGNER", "DLL_INVENTORY", "OFFLINE_EGRESS_POLICY",
    "MODEL_INVENTORY", "COORDINATE_ROUND_TRIP", "ATOMIC_WRITE_CLEANUP", "DERIVATIVE_DELETION",
})
_OPTIONAL_CHECK = "ACTIVE_EGRESS_CANARY"
_REPORT_MAX_BYTES = 32 * 1024
_FIXTURE_MAX_BYTES = 16 * 1024
_MAX_REPORT_AGE = timedelta(hours=24)
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
_SVG = "{http://www.w3.org/2000/svg}"
_DIMENSIONS = (1000, 600)
_EXPECTED_REGION = "part-a"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DemoContext:
    doctor_report: DoctorReport
    output: Path
    config: EffectiveConfig | None = None
    clock: Callable[[], datetime] = _utc_now


@dataclass(frozen=True)
class DemoRegion:
    region_id: str
    bbox: tuple[float, float, float, float]
    artifact_hash: str


@dataclass(frozen=True)
class DemoResult:
    schema_version: int
    run_id: str
    answer: str
    artifact_hash: str
    regions: tuple[DemoRegion, ...]
    overlay_path: str
    elapsed_ms: int


def _doctor_error() -> OperatorError:
    return OperatorError(
        "RAGLAB-DEMO-DOCTOR", "demo", "a current matching PASS doctor report is required",
        "readiness gate blocked", "complete matching doctor report issued within 24 hours",
        "synthetic demo did not run", True, r".\raglab.ps1 doctor --help",
        "docs/operator/ERROR-CATALOG.md#raglab-demo-doctor", 5,
    )


def _output_error() -> OperatorError:
    return OperatorError(
        "RAGLAB-DEMO-OUTPUT", "demo", "synthetic output directory is inaccessible or already in use",
        "output could not be published", "local output directory with free output names",
        "existing files were preserved", True, r".\raglab.ps1 demo --help",
        "docs/operator/ERROR-CATALOG.md#raglab-demo-output", 2,
    )


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON field")
        result[name] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("unexpected JSON constant")


def _read_json(content: bytes) -> object:
    return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)


def read_doctor_report(path: Path) -> DoctorReport:
    """Read the bounded version 1 format for synthetic readiness only."""
    paths = WindowsLocalPaths()
    try:
        with paths.pin(paths.validate(path), directory_write_sharing=True) as source:
            value = _read_json(source.read_bytes(_REPORT_MAX_BYTES))
        if not isinstance(value, dict) or value.keys() != {field.name for field in fields(DoctorReport)}:
            raise ValueError("invalid report fields")
        checks = value["checks"]
        if not isinstance(checks, list) or not len(_REQUIRED_CHECKS) <= len(checks) <= len(_REQUIRED_CHECKS) + 1:
            raise ValueError("invalid number of checks")
        check_fields = {field.name for field in fields(CheckResult)}
        if any(not isinstance(check, dict) or check.keys() != check_fields for check in checks):
            raise ValueError("invalid check fields")
        value["checks"] = tuple(CheckResult(**check) for check in checks)
        return DoctorReport(**value)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _doctor_error() from exc


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("invalid UTC report time")
    return datetime.fromisoformat(value)


def _is_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _check_doctor(report: DoctorReport, config: EffectiveConfig, now: datetime) -> None:
    # The report is unsigned. This gate is confined to the fixed synthetic assets.
    if (
        type(report) is not DoctorReport
        or type(report.schema_version) is not int or report.schema_version != 1
        or config.status != "PASS"
        or config.values.get("profile") != report.profile or report.profile not in PROFILES
        or report.overall_status != CheckStatus.PASS
        or not _is_hex(report.run_id, 32)
        or not _is_hex(report.generation_fingerprint, 64)
        or type(report.checks) is not tuple
        or not len(_REQUIRED_CHECKS) <= len(report.checks) <= len(_REQUIRED_CHECKS) + 1
    ):
        raise _doctor_error()
    try:
        generated = _timestamp(report.generated_at)
        expiry = _timestamp(report.bundle_approval_expires_at)
        if not timedelta(0) <= now - generated <= _MAX_REPORT_AGE or not now < expiry:
            raise ValueError("report time outside freshness window")
        seen = set()
        for check in report.checks:
            if (
                type(check) is not CheckResult or not isinstance(check.check_id, str)
                or check.check_id in seen or check.status != CheckStatus.PASS
                or check.action is not None or not isinstance(check.message, str)
                or not 0 < len(check.message) <= 512 or not check.message.isprintable()
            ):
                raise ValueError("invalid PASS check")
            seen.add(check.check_id)
        if seen not in (_REQUIRED_CHECKS, _REQUIRED_CHECKS | {_OPTIONAL_CHECK}):
            raise ValueError("incomplete or unfamiliar readiness checks")
    except (TypeError, ValueError) as exc:
        raise _doctor_error() from exc


def _fixture_evidence(paths: WindowsLocalPaths) -> tuple[str, str, DemoRegion, tuple[int, ...]]:
    verified = {}
    with paths.pin(paths.validate(FIXTURE_DIRECTORY)) as root:
        for name, expected_hash in FIXTURE_HASHES:
            with paths.pin_entry(root.path / name) as member:
                content = member.read_bytes(_FIXTURE_MAX_BYTES)
            actual_hash = sha256(content).hexdigest()
            if actual_hash != expected_hash:
                raise ValueError("synthetic fixture hash mismatch")
            verified[name] = (content, actual_hash)
    source, artifact_hash = verified["source.svg"]
    expected, _ = verified["expected.json"]
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", source, flags=re.IGNORECASE):
        raise ValueError("unsafe XML declaration")
    svg = ET.fromstring(source.decode("utf-8"))
    label = svg.find(f"{_SVG}text")
    rect = svg.find(f"{_SVG}rect[@id='{_EXPECTED_REGION}']")
    if (
        svg.tag != f"{_SVG}svg" or svg.get("viewBox") != "0 0 1000 600"
        or (svg.get("width"), svg.get("height")) != ("1000", "600")
        or rect is None or label is None or label.text != "PART A"
    ):
        raise ValueError("unexpected synthetic source geometry")
    pixels = tuple(int(rect.get(key, "")) for key in ("x", "y", "width", "height"))
    value = _read_json(expected)
    if (
        not isinstance(value, dict) or value.keys() != {"schema_version", "answer", "region_id", "bbox"}
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["region_id"] != _EXPECTED_REGION or not isinstance(value["answer"], str)
        or not isinstance(value["bbox"], list) or len(value["bbox"]) != 4
    ):
        raise ValueError("unexpected synthetic evidence shape")
    bbox = BBox(*value["bbox"])
    normalized = (bbox.x, bbox.y, bbox.width, bbox.height)
    if normalized != tuple(pixel / axis for pixel, axis in zip(pixels, _DIMENSIONS * 2)):
        raise ValueError("synthetic coordinates do not match the source")
    return value["answer"], artifact_hash, DemoRegion(_EXPECTED_REGION, normalized, artifact_hash), pixels


def _overlay(pixels: tuple[int, ...]) -> bytes:
    x, y, width, height = pixels
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="600" viewBox="0 0 1000 600">\n'
        '  <rect width="1000" height="600" fill="white"/>\n'
        f'  <rect id="evidence-part-a" x="{x}" y="{y}" width="{width}" height="{height}"'
        ' fill="#d9eafd" stroke="#174a7e" stroke-width="5"/>\n'
        '  <text x="630" y="275" font-size="48">PART A</text>\n'
        '</svg>\n'
    ).encode("utf-8")


def _summary(result: DemoResult) -> bytes:
    return (
        '<!doctype html>\n<html lang="ko"><meta charset="utf-8">\n'
        '<title>Synthetic evidence demo</title>\n<h1>Synthetic evidence demo</h1>\n'
        f'<p>{escape(result.answer)}</p>\n'
        f'<p>SHA-256 <code>{escape(result.artifact_hash)}</code></p>\n'
        f'<p>Evidence <code>{escape(result.overlay_path)}</code></p>\n</html>\n'
    ).encode("utf-8")


def _relative_to(candidate: Path, root: Path) -> bool:
    return len(candidate.parts) >= len(root.parts) and all(
        windows_name_compare(left, right) == 0 for left, right in zip(candidate.parts, root.parts)
    )


def run_synthetic_demo(context: DemoContext) -> DemoResult:
    """Publish evidence for the pinned demo only, with result.json written last."""
    started = monotonic_ns()
    report = context.doctor_report
    if type(report) is not DoctorReport or not isinstance(report.profile, str):
        raise _doctor_error()
    config = context.config if context.config is not None else load_profile_config(report.profile)
    _check_doctor(report, config, context.clock())

    paths = WindowsLocalPaths()
    try:
        answer, artifact_hash, region, pixels = _fixture_evidence(paths)
    except (OSError, RuntimeError, TypeError, ValueError, ET.ParseError) as exc:
        raise OperatorError(
            "RAGLAB-DEMO-EVIDENCE", "demo", "synthetic fixture integrity or evidence check failed",
            "fixture did not verify", "approved synthetic fixture and matching coordinates",
            "no demo outputs were published", False, r".\raglab.ps1 verify-bundle --help",
            "docs/operator/ERROR-CATALOG.md#raglab-demo-evidence", 6,
        ) from exc

    run_id = uuid4().hex
    try:
        with ExitStack() as protected_roots, ExitStack() as destination_locks:
            output = paths.validate(context.output)
            protected = [
                protected_roots.enter_context(paths.pin(paths.validate(location), directory_write_sharing=True)).path
                for location in (FIXTURE_DIRECTORY, CONFIG_DIRECTORY)
            ]
            with paths.pin(output, missing_ok=True, directory_write_sharing=True) as preview:
                if any(_relative_to(preview.path, root) for root in protected):
                    raise ValueError("output collides with a protected directory")
            parent = destination_locks.enter_context(paths.pin(preview.path, create_directories=True))
            if any(_relative_to(parent.path, root) for root in protected):
                raise ValueError("output collides with a protected directory")
            temporary_name = f".raglab-demo-{run_id}.tmp"
            paths.probe_file(
                parent, temporary_name, b'{"synthetic":true}\n', release_protection=destination_locks.close,
            )
            with paths.pin(parent.path / temporary_name, missing_ok=True) as probe:
                if probe.info is not None:
                    raise OSError("synthetic derivative still exists")

            _check_doctor(report, config, context.clock())
            elapsed = (monotonic_ns() - started) // 1_000_000
            result = DemoResult(1, run_id, answer, artifact_hash, (region,), "overlay.svg", elapsed)
            events = serialize_progress_event(
                ProgressEvent(1, run_id, 1, "synthetic_demo", 1, 1, elapsed, 0, 0, "PASS")
            )
            deletion = serialize_json({"schema_version": 1, "run_id": run_id, "status": "PASS"}).encode("utf-8")
            paths.publish_new_files(parent, {
                "overlay.svg": _overlay(pixels),
                "summary.html": _summary(result),
                "events.jsonl": events,
                "deletion-smoke-report.json": deletion,
                "result.json": serialize_json(asdict(result)).encode("utf-8"),
            }, destination_locks.close)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _output_error() from exc
    return result
