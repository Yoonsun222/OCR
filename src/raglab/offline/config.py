import hashlib
import json
import ntpath
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType

from raglab.offline.windows_paths import WindowsLocalPaths, path_segment_is_safe


CONFIG_DIRECTORY = Path(__file__).parents[3] / "configs"
PROFILES = ("local16", "local32")
LOCKED_SECURITY_FIELDS = frozenset({"network_mode", "diagnostic_content"})
FIELD_RULES = MappingProxyType({
    "network_mode": (str, frozenset({"deny"})),
    "profile": (str, frozenset(PROFILES)),
    "max_working_set_gib": (int, range(1, 27)),
    "interactive_concurrency": (int, frozenset({1, 2})),
    "rerank_candidates": (int, range(1, 31)),
    "visual_candidates": (int, range(0, 6)),
    "temp_root": (str, None),
    "cache_root": (str, None),
    "output_root": (str, None),
    "diagnostic_content": (bool, frozenset({False})),
})

_PATH_FIELDS = frozenset({"temp_root", "cache_root", "output_root"})
_SECURITY_BASELINE = MappingProxyType({"network_mode": "deny", "diagnostic_content": False})
_PROFILE_KEYS = frozenset({
    "profile", "max_working_set_gib", "interactive_concurrency", "rerank_candidates", "visual_candidates",
})
_MAX_CONFIG_BYTES = 1024 * 1024
_SCHEMA_ISSUE = "RAGLAB-CONFIG-001"
_VALUE_ISSUE = "RAGLAB-CONFIG-002"
_POLICY_ISSUE = "RAGLAB-CONFIG-003"
_LAYER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}")
_PATH_TOKEN = re.compile(r"<path:[0-9a-f]{64}>")


@dataclass(frozen=True)
class ConfigLayer:
    name: str
    values: Mapping[str, object]
    locked: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ConfigIssue:
    code: str
    field: str | None
    reason: str


@dataclass(frozen=True)
class EffectiveConfig:
    status: str
    values: Mapping[str, object]
    provenance: Mapping[str, str]
    locked: frozenset[str]
    issues: tuple[ConfigIssue, ...]

    @property
    def exit_code(self) -> int:
        if self.status == "PASS":
            return 0
        return 3 if any(issue.code == _POLICY_ISSUE for issue in self.issues) else 2


def _safe_config_path(value: str) -> bool:
    if not value or len(value) > 32760:
        return False
    path = value.replace("\\", "/")
    if path.startswith("//"):
        return False
    drive, tail = ntpath.splitdrive(path)
    if drive:
        if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("/"):
            return False
        tail = tail[1:]
        if not tail:
            return True
    elif tail.startswith("/"):
        return False
    return all(part == "." or path_segment_is_safe(part) for part in tail.split("/"))


def _field_issue(key: str, value: object, *, redacted: bool = False) -> ConfigIssue | None:
    expected_type, allowed = FIELD_RULES[key]
    if type(value) is not expected_type:
        return ConfigIssue(_VALUE_ISSUE, key, "invalid field type")
    if allowed is not None and value not in allowed:
        if key in LOCKED_SECURITY_FIELDS:
            return ConfigIssue(_POLICY_ISSUE, key, "security field is locked")
        return ConfigIssue(_VALUE_ISSUE, key, "value outside the allowed range")
    if key in _PATH_FIELDS:
        valid = _PATH_TOKEN.fullmatch(value) if redacted else _safe_config_path(value)
        if not valid:
            return ConfigIssue(_VALUE_ISSUE, key, "invalid configuration path")
    return None


def _field_issues(value: Mapping[str, object]) -> tuple[ConfigIssue, ...]:
    if not isinstance(value, Mapping):
        return (ConfigIssue(_SCHEMA_ISSUE, None, "configuration must be an object"),)
    issues = []
    if value.keys() - FIELD_RULES.keys():
        issues.append(ConfigIssue(_SCHEMA_ISSUE, None, "unknown configuration field"))
    for key in sorted(value.keys() & FIELD_RULES.keys()):
        issue = _field_issue(key, value[key])
        if issue is not None:
            issues.append(issue)
    return tuple(issues)


def _profile_issues(values: Mapping[str, object]) -> tuple[ConfigIssue, ...]:
    if values.get("profile") != "local16":
        return ()
    issues = []
    for key, ceiling in (("interactive_concurrency", 1), ("max_working_set_gib", 12)):
        value = values.get(key)
        if type(value) is int and ceiling < value <= max(FIELD_RULES[key][1]):
            issues.append(ConfigIssue(_VALUE_ISSUE, key, "value exceeds the selected profile ceiling"))
    return tuple(issues)


def validate_config(value: Mapping[str, object]) -> tuple[ConfigIssue, ...]:
    """Validate a full or partial layer against the exact supported field schema."""
    issues = _field_issues(value)
    if not isinstance(value, Mapping):
        return issues
    return issues + _profile_issues(value)


def load_effective_config(layers: Sequence[ConfigLayer]) -> EffectiveConfig:
    values: dict[str, object] = {}
    provenance: dict[str, str] = {}
    locked: set[str] = set()
    issues: list[ConfigIssue] = []
    for layer in layers:
        if not isinstance(layer.name, str) or not _LAYER_NAME.fullmatch(layer.name):
            issues.append(ConfigIssue(_SCHEMA_ISSUE, None, "invalid configuration layer"))
            continue
        layer_issues = _field_issues(layer.values)
        issues.extend(layer_issues)
        if not isinstance(layer.values, Mapping):
            continue

        invalid_keys = {issue.field for issue in layer_issues}
        supplied_valid = (layer.values.keys() & FIELD_RULES.keys()) - invalid_keys
        for key in sorted(supplied_valid):
            candidate = layer.values[key]
            if key in locked:
                if values[key] != candidate:
                    issues.append(ConfigIssue(_POLICY_ISSUE, key, "configuration field is locked"))
                continue
            values[key] = candidate
            provenance[key] = layer.name
            if key in LOCKED_SECURITY_FIELDS:
                locked.add(key)
        if not isinstance(layer.locked, frozenset) or not layer.locked <= supplied_valid:
            issues.append(ConfigIssue(_SCHEMA_ISSUE, None, "layer locks require valid supplied fields"))
        else:
            locked.update(layer.locked)

    for key, baseline in _SECURITY_BASELINE.items():
        if key not in values:
            values[key] = baseline
            provenance[key] = "security"
    issues.extend(_profile_issues(values))
    return EffectiveConfig(
        "BLOCK" if issues else "PASS",
        MappingProxyType(values),
        MappingProxyType(provenance),
        frozenset(locked) | LOCKED_SECURITY_FIELDS,
        tuple(issues),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate configuration key")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid configuration constant")


def read_config_json(path: Path) -> dict[str, object]:
    """Read bounded UTF-8 JSON while applying the Windows local storage gate."""
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("configuration JSON must be local")
    if os.name == "nt":
        paths = WindowsLocalPaths()
        with paths.pin(paths.validate(path)) as source:
            content = source.read_bytes(_MAX_CONFIG_BYTES)
    else:
        with path.open("rb") as source:
            content = source.read(_MAX_CONFIG_BYTES + 1)
        if len(content) > _MAX_CONFIG_BYTES:
            raise ValueError("configuration JSON exceeds the size limit")
    value = json.loads(
        content.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("configuration JSON must be an object")
    return value


def load_profile_config(profile: str, run: Path | None = None) -> EffectiveConfig:
    """Load checked-in security and profile layers, followed by an optional run file."""
    try:
        if profile not in PROFILES:
            raise ValueError("unknown profile")
        defaults = read_config_json(CONFIG_DIRECTORY / "security-defaults.json")
        selected = read_config_json(CONFIG_DIRECTORY / "profiles" / f"{profile}.json")
        if defaults.keys() != LOCKED_SECURITY_FIELDS | _PATH_FIELDS:
            raise ValueError("invalid security defaults")
        if selected.keys() != _PROFILE_KEYS or selected.get("profile") != profile:
            raise ValueError("invalid selected profile")
        layers = [
            ConfigLayer("security", defaults, LOCKED_SECURITY_FIELDS),
            ConfigLayer(profile, selected, frozenset({"profile"})),
        ]
        if run is not None:
            layers.append(ConfigLayer("run", read_config_json(run)))
    except (OSError, RuntimeError, ValueError):
        return EffectiveConfig(
            "BLOCK", _SECURITY_BASELINE,
            MappingProxyType({key: "security" for key in _SECURITY_BASELINE}),
            LOCKED_SECURITY_FIELDS,
            (ConfigIssue(_SCHEMA_ISSUE, None, "could not load valid local configuration JSON"),),
        )
    return load_effective_config(layers)


def summarize_config(config: EffectiveConfig) -> dict[str, object]:
    return {"status": config.status, "issues": [asdict(item) for item in config.issues]}


def explain_config(config: EffectiveConfig) -> dict[str, object]:
    values = {}
    for key, value in config.values.items():
        if key in _PATH_FIELDS:
            # Preserve equality for diff without exposing directory names or user paths.
            value = f"<path:{hashlib.sha256(value.encode('utf-8')).hexdigest()}>"
        values[key] = {"value": value, "source_layer": config.provenance[key], "locked": key in config.locked}
    return {"schema_version": 1, **summarize_config(config), "values": values}


def _report_values(path: Path) -> dict[str, object]:
    report = read_config_json(path)
    values = report.get("values")
    if (
        report.keys() != {"schema_version", "status", "issues", "values"}
        or type(report["schema_version"]) is not int
        or report["schema_version"] != 1
        or report["status"] != "PASS"
        or report["issues"] != []
        or not isinstance(values, dict)
        or values.keys() - FIELD_RULES.keys()
        or not LOCKED_SECURITY_FIELDS <= values.keys()
    ):
        raise ValueError("invalid effective configuration report")
    effective = {}
    for key, field in values.items():
        if (
            not isinstance(field, dict)
            or field.keys() != {"value", "source_layer", "locked"}
            or not isinstance(field["source_layer"], str)
            or not _LAYER_NAME.fullmatch(field["source_layer"])
            or type(field["locked"]) is not bool
            or (key in LOCKED_SECURITY_FIELDS and not field["locked"])
            or _field_issue(key, field["value"], redacted=True) is not None
        ):
            raise ValueError("invalid effective configuration field")
        effective[key] = field["value"]
    if _profile_issues(effective):
        raise ValueError("configuration report exceeds profile limits")
    return effective


def diff_config_reports(left: Path, right: Path) -> list[dict[str, object]]:
    before = _report_values(left)
    after = _report_values(right)
    changes = []
    for key in sorted(before.keys() | after.keys()):
        path = f"/{key}"
        if key not in after:
            changes.append({"op": "remove", "path": path})
        elif key not in before:
            changes.append({"op": "add", "path": path, "value": after[key]})
        elif before[key] != after[key]:
            changes.append({"op": "replace", "path": path, "value": after[key]})
    return changes
