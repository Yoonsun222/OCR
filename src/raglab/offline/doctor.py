import json
import math
import re
import struct
import sysconfig
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from raglab.offline.bundle import BundleReport
from raglab.offline.config import EffectiveConfig
from raglab.offline.models import CheckResult, CheckStatus
from raglab.offline.ports import ActiveEgressCanary, EgressPolicyProbe, SystemProbe, VerificationDeadline
from raglab.offline.windows_names import WindowsFilenameIndex
from raglab.offline.windows_paths import PinnedPath


_PROFILE_LIMITS = {"local16": (16, 12), "local32": (32, 26)}
_DISK_MINIMUM_GIB = 10
_APPROVAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_MODELS = (".gguf", ".onnx", ".safetensors", ".tflite", ".pt", ".pth", ".bin")
_PE_MACHINE = {"win-amd64": (0x8664, 0x20B), "win-arm64": (0xAA64, 0x20B), "win32": (0x14C, 0x10B)}


@dataclass(frozen=True)
class CanaryAuthorization:
    endpoint: str
    approval_id: str


@dataclass(frozen=True)
class CanaryRequest:
    requested: bool = False
    approval_id: str | None = None
    adapter: ActiveEgressCanary | None = None


@dataclass(frozen=True)
class VerifiedBundleEvidence:
    generation_fingerprint: str
    dll_count: int
    dll_compatible: bool
    model_count: int
    canary: CanaryAuthorization | None = None
    site_policy_valid: bool = True


@dataclass(frozen=True)
class DoctorContext:
    system: SystemProbe
    egress: EgressPolicyProbe
    bundle: BundleReport
    config: EffectiveConfig
    evidence: VerifiedBundleEvidence | None = None
    canary: CanaryRequest = CanaryRequest()


@dataclass(frozen=True)
class DoctorReport:
    schema_version: int
    run_id: str
    profile: str
    overall_status: CheckStatus
    checks: tuple[CheckResult, ...]
    generated_at: str
    generation_fingerprint: str | None


def _valid_approval_id(value: object) -> bool:
    return isinstance(value, str) and _APPROVAL_ID.fullmatch(value) is not None


def _canary_metadata(value: object) -> CanaryAuthorization:
    if not isinstance(value, dict) or value.keys() != {"schema_version", "canary"}:
        raise ValueError("invalid signed site policy")
    canary = value["canary"]
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or not (
        isinstance(canary, dict) and canary.keys() == {"endpoint", "approval_id"}
    ):
        raise ValueError("invalid signed canary policy")
    endpoint, approval_id = canary["endpoint"], canary["approval_id"]
    if not _valid_approval_id(approval_id) or not isinstance(endpoint, str) or not (
        0 < len(endpoint) <= 2048 and all(33 <= ord(char) < 127 and char != "\\" for char in endpoint)
    ):
        raise ValueError("invalid signed canary authorization")
    url = urlsplit(endpoint)
    if not (
        url.scheme == "https" and url.hostname and url.username is None and url.password is None
        and not url.fragment and url.port != 0
    ):
        raise ValueError("invalid signed canary endpoint")
    return CanaryAuthorization(endpoint, approval_id)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate signed policy field")
        result[name] = value
    return result


def _dll_compatible(member: PinnedPath) -> bool:
    # Interpret only a bounded DOS and PE header. DllMain and imports never run.
    if not member.stream or not member.info or member.info.size < 90:
        return False
    member.stream.seek(0)
    dos = member.stream.read(64)
    if len(dos) != 64 or dos[:2] != b"MZ":
        return False
    offset = struct.unpack_from("<I", dos, 60)[0]
    if not 64 <= offset <= min(1024**2, member.info.size - 26):
        return False
    member.stream.seek(offset)
    header = member.stream.read(26)
    if len(header) != 26 or header[:4] != b"PE\0\0":
        return False
    machine, sections, _, _, _, optional_size, flags, magic = struct.unpack_from("<HHIIIHHH", header, 4)
    minimum_optional_size = {0x10B: 96, 0x20B: 112}.get(magic, 0)
    return (
        _PE_MACHINE.get(sysconfig.get_platform()) == (machine, magic)
        and 0 < sections <= 96 and flags & 0x2002 == 0x2002
        and minimum_optional_size <= optional_size <= 4096
        and offset + 24 + optional_size + 40 * sections <= member.info.size
    )


def inspect_verified_bundle(
    files: Mapping[str, PinnedPath], fingerprint: str, deadline: VerificationDeadline,
) -> VerifiedBundleEvidence:
    dlls = [member for name, member in files.items() if name.lower().endswith(".dll")]
    models = sum(name.lower().endswith(_MODELS) for name in files)
    compatible = all(deadline.call(_dll_compatible, member) for member in dlls)
    site = WindowsFilenameIndex(files).find("site-profile.json")
    if site is None:
        return VerifiedBundleEvidence(fingerprint, len(dlls), compatible, models)
    try:
        data = deadline.call(files[site].read_bytes, 16 * 1024)
        metadata = _canary_metadata(json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object))
    except (OSError, ValueError, RecursionError):
        return VerifiedBundleEvidence(fingerprint, len(dlls), compatible, models, site_policy_valid=False)
    return VerifiedBundleEvidence(fingerprint, len(dlls), compatible, models, metadata)


def _quantity(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value <= 2**64 and math.isfinite(value)


def _coordinate_round_trip() -> bool:
    width, height = 1000, 600
    pixels = (600, 180, 240, 180)
    dimensions = (width, height, width, height)
    normalized = tuple(pixel / axis for pixel, axis in zip(pixels, dimensions))
    return (
        normalized == (0.6, 0.3, 0.24, 0.3)
        and tuple(round(value * axis) for value, axis in zip(normalized, dimensions)) == pixels
    )


def run_doctor(context: DoctorContext) -> DoctorReport:
    checks: list[CheckResult] = []

    def record(check_id: str, condition: bool, success: str, failure: str, action: str) -> None:
        if condition is True:
            checks.append(CheckResult(check_id, CheckStatus.PASS, success))
        else:
            checks.append(CheckResult(check_id, CheckStatus.BLOCK, failure, action))

    bundle, config = context.bundle, context.config
    approval_ok = bundle.status == "PASS" and _valid_approval_id(bundle.approval_id)
    fingerprint = bundle.generation_fingerprint
    if not approval_ok or not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        fingerprint = None
    record("BUNDLE_APPROVAL", approval_ok, f"approval {bundle.approval_id} is valid",
           "bundle approval could not be verified", "Use a current approved bundle and verify its approval.")
    try:
        snapshot = context.system.snapshot()
    except Exception:
        snapshot = None
    python = getattr(snapshot, "python_version", None)
    runtime_ok = getattr(snapshot, "supported_os", None) is True and python in ((3, 11), (3, 12))
    record("OS_PYTHON", runtime_ok, "supported Windows workstation and Python runtime",
           "Windows workstation or Python support could not be verified", "Use a supported Windows workstation and Python 3.11 or 3.12.")

    selected = config.values.get("profile")
    profile = selected if isinstance(selected, str) and selected in _PROFILE_LIMITS else "unknown"
    minimum_ram, maximum_cap = _PROFILE_LIMITS.get(profile, (0, 0))
    cap = config.values.get("max_working_set_gib")
    config_ok = (
        config.status == "PASS" and profile != "unknown"
        and type(cap) is int and 1 <= cap <= maximum_cap
        and config.values.get("network_mode") == "deny"
        and config.values.get("diagnostic_content") is False
    )
    physical = getattr(snapshot, "physical_ram_gib", None)
    available = getattr(snapshot, "available_ram_gib", None)
    ram_ok = (config_ok and _quantity(physical) and _quantity(available)
              and minimum_ram <= physical and cap <= available <= physical)
    record("RESOURCE_PROFILE", ram_ok,
           f"{profile}: {physical:.1f} GiB physical, {cap:.1f} GiB process cap" if ram_ok else "",
           "effective profile or installed and available memory requirements were not met",
           "Check the profile, installed RAM, and available RAM for its process cap.")
    disk = getattr(snapshot, "free_disk_gib", None)
    record("FREE_DISK", _quantity(disk) and disk >= _DISK_MINIMUM_GIB,
           "temp, cache, and output volumes have sufficient space",
           "temp, cache, or output disk space could not be verified",
           f"Make at least {_DISK_MINIMUM_GIB} GiB available on each working volume.")
    record("WRITABLE_PATHS", getattr(snapshot, "paths_writable", None) is True,
           "temp, cache, and output passed write checks", "working paths could not be written safely",
           "Check permissions and local path configuration.")
    record("LONG_UNICODE_PATH", getattr(snapshot, "long_unicode_path", None) is True,
           "long Unicode path round-trip succeeded", "long Unicode path round-trip failed",
           "Check long-path support and permissions on the local working volume.")
    record("CATALOG_SIGNER", fingerprint is not None, "approved signer and exact catalog membership verified",
           "catalog signer and membership could not be verified", "Verify the bundle against the machine publisher policy.")

    evidence = context.evidence
    bound = evidence is not None and fingerprint is not None and evidence.generation_fingerprint == fingerprint
    dll_ok = bound and type(evidence.dll_count) is int and evidence.dll_count >= 0 and evidence.dll_compatible is True
    record("DLL_INVENTORY", dll_ok, "verified DLL inventory is compatible with the Python architecture",
           "verified DLL inventory or compatibility could not be established",
           "Use approved DLLs built for the selected Python architecture.")
    try:
        egress_ok = context.egress.inspect().status == "DENIED" and config_ok
    except Exception:
        egress_ok = False
    if bound and evidence.site_policy_valid is not True:
        egress_ok = False
    record("OFFLINE_EGRESS_POLICY", egress_ok, "passive outbound firewall and proxy policy verified",
           "outbound policy could not be proven", "Ask security to review the effective outbound firewall and proxy settings.")

    model_ok = bound and type(evidence.model_count) is int and evidence.model_count >= 0
    record("MODEL_INVENTORY", model_ok, "model inventory checked against the verified manifest",
           "verified model inventory is unavailable", "Verify the signed manifest and model artifact hashes.")
    record("COORDINATE_ROUND_TRIP", _coordinate_round_trip(),
           "synthetic coordinate round-trip succeeded", "synthetic coordinate round-trip failed",
           "Repair the approved toolkit installation.")
    record("ATOMIC_WRITE_CLEANUP", getattr(snapshot, "atomic_write_cleanup", None) is True,
           "synthetic atomic write and cleanup succeeded", "synthetic atomic write or cleanup failed",
           "Check output permissions and local filesystem behavior.")
    record("DERIVATIVE_DELETION", getattr(snapshot, "derivative_deleted", None) is True,
           "synthetic derivative deletion succeeded", "synthetic derivative deletion failed",
           "Check temp permissions and derivative deletion policy.")

    request = context.canary
    if request.requested or request.approval_id is not None:
        authorization = evidence.canary if bound else None
        authorized = (
            request.requested is True and request.adapter is not None and authorization is not None
            and _valid_approval_id(request.approval_id) and request.approval_id == authorization.approval_id
            and all(check.status == CheckStatus.PASS for check in checks)
        )
        canary_ok = False
        if authorized:
            try:
                canary_ok = request.adapter.verify_denied(authorization.endpoint, request.approval_id) is True
            except Exception:
                pass
        record("ACTIVE_EGRESS_CANARY", canary_ok, "authorized canary confirmed outbound denial",
               "authorized outbound denial was not confirmed",
               "Use the security acceptance runbook and its approved canary adapter.")

    overall = CheckStatus.BLOCK if any(check.status == CheckStatus.BLOCK for check in checks) else CheckStatus.PASS
    return DoctorReport(1, uuid4().hex, profile, overall, tuple(checks),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"), fingerprint)
