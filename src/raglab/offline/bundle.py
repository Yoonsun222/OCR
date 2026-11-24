import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from raglab.offline.ports import (
    CatalogDecision,
    CatalogVerifier,
    PublisherAuthority,
    VerificationDeadline,
    VerificationLimits,
    normalize_thumbprint,
)
from raglab.offline.windows_names import WindowsFilenameIndex, windows_name_compare
from raglab.offline.windows_paths import PinnedPath, WindowsLocalPaths, path_segment_is_safe


_PATH_FAILURE = "RAGLAB-BUNDLE-001"
_HASH_FAILURE = "RAGLAB-BUNDLE-002"
_CATALOG_FAILURE = "RAGLAB-BUNDLE-003"
_APPROVAL_FAILURE = "RAGLAB-BUNDLE-004"
_LICENSE_FAILURE = "RAGLAB-BUNDLE-005"
_CONTROLS = frozenset({"manifest.json", "approval.json", "bundle.cat"})
_APPROVAL_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


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
    generation_fingerprint: str | None = None

    @property
    def exit_code(self) -> int:
        if self.status == "PASS":
            return 0
        if any(failure.artifact == "." for failure in self.failures):
            return 2
        if any(failure.code in {_APPROVAL_FAILURE, _LICENSE_FAILURE} for failure in self.failures):
            return 3
        return 4


@dataclass(frozen=True)
class _Artifact:
    name: str
    sha256: str
    license_approved: bool


@dataclass(frozen=True)
class _Approval:
    identifier: str
    expires_at: datetime
    signers: frozenset[str]


class _BundleBlocked(Exception):
    def __init__(self, code: str, artifact: str, reason: str):
        self.failure = BundleFailure(code, artifact, reason)


def _block(code: str, artifact: str, reason: str) -> BundleReport:
    return BundleReport("BLOCK", None, (BundleFailure(code, artifact, reason),))


def _relative_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("path must be text")
    parts = value.replace("\\", "/").split("/")
    if not all(path_segment_is_safe(part) for part in parts):
        raise ValueError("path is not a safe relative path")
    return "/".join(parts)


def _is_control(name: str) -> bool:
    return any(windows_name_compare(name, control) == 0 for control in _CONTROLS)


def _control_code(name: str) -> str:
    if windows_name_compare(name, "approval.json") == 0:
        return _APPROVAL_FAILURE
    if windows_name_compare(name, "bundle.cat") == 0:
        return _CATALOG_FAILURE
    return _PATH_FAILURE


def _inventory(
    root: Path,
    paths: WindowsLocalPaths,
    locks: ExitStack,
    limits: VerificationLimits,
    deadline: VerificationDeadline,
    previous: Mapping[str, PinnedPath] | None = None,
) -> dict[str, PinnedPath]:
    inventory: dict[str, PinnedPath] = {}
    directories = [(root, "", 0)]
    while directories:
        directory, prefix, depth = directories.pop()
        deadline.check()
        with os.scandir(directory) as entries:
            for entry in entries:
                deadline.check()
                if len(inventory) >= limits.inventory_entries or depth >= limits.inventory_depth:
                    raise _BundleBlocked(_PATH_FAILURE, "manifest.json", "inventory limit exceeded")
                if not path_segment_is_safe(entry.name):
                    raise _BundleBlocked(_PATH_FAILURE, "manifest.json", "unsafe bundle member name")
                name = prefix + entry.name
                if name in inventory:
                    raise _BundleBlocked(_PATH_FAILURE, "manifest.json", "duplicate bundle member")
                try:
                    if previous is None:
                        member = locks.enter_context(paths.pin_entry(directory / entry.name))
                    else:
                        member = previous[name]
                        with paths.pin_entry(directory / entry.name) as current:
                            if current.info != member.info:
                                raise ValueError("member identity changed")
                except (KeyError, OSError, ValueError):
                    raise _BundleBlocked(_control_code(name), name, "unsafe or changed bundle member") from None
                deadline.check()
                inventory[name] = member
                if member.info.is_directory:
                    directories.append((member.path, name + "/", depth + 1))
    WindowsFilenameIndex(inventory)
    if previous is not None and inventory.keys() != previous.keys():
        raise _BundleBlocked(_PATH_FAILURE, "manifest.json", "bundle inventory changed")
    return inventory


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _read_document(
    member: PinnedPath | None, name: str, limits: VerificationLimits,
) -> tuple[dict[str, object], str]:
    try:
        if member is None:
            raise ValueError("control file missing")
        raw = member.read_bytes(limits.metadata_bytes)
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_invalid_constant,
        )
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
    except (OSError, RuntimeError, ValueError):
        raise _BundleBlocked(_control_code(name), name, "missing or invalid metadata") from None
    return value, hashlib.sha256(raw).hexdigest()


def _parse_manifest(document: dict[str, object], maximum: int) -> dict[str, _Artifact]:
    version = document.get("schema_version")
    rows = document.get("artifacts")
    if type(version) is not int or version != 1 or not isinstance(rows, list) or not 0 < len(rows) <= maximum:
        raise ValueError("invalid manifest schema")

    artifacts: dict[str, _Artifact] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed artifact")
        name = _relative_name(row.get("path"))
        sha256 = row.get("sha256")
        if name in artifacts or _is_control(name):
            raise ValueError("duplicate or reserved artifact path")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValueError("invalid artifact digest")
        artifacts[name] = _Artifact(name, sha256.lower(), row.get("license_decision") == "approved")
    WindowsFilenameIndex(artifacts)
    return artifacts


def _parse_approval(document: dict[str, object]) -> _Approval:
    identifier = document.get("approval_id")
    expires = document.get("expires_at")
    allowlist = document.get("allowed_signer_thumbprints")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identifier):
        raise ValueError("invalid approval identifier")
    if not isinstance(expires, str) or not _APPROVAL_TIME.fullmatch(expires) or expires.endswith("-00:00"):
        raise ValueError("expected an expiration with a known time zone")
    if not isinstance(allowlist, list) or not allowlist:
        raise ValueError("expected a signer allowlist")
    signers = {normalize_thumbprint(value) for value in allowlist}
    if None in signers:
        raise ValueError("invalid signer thumbprint")
    return _Approval(identifier, datetime.fromisoformat(expires), frozenset(signers))


def _sha256(member: PinnedPath, deadline: VerificationDeadline) -> str:
    digest = hashlib.sha256()
    member.stream.seek(0)
    size = 0
    for chunk in iter(lambda: member.stream.read(1024 * 1024), b""):
        deadline.check()
        size += len(chunk)
        digest.update(chunk)
    if size != member.info.size:
        raise ValueError("file size changed")
    deadline.check()
    return digest.hexdigest()


def _opens_held_file(
    supplied: str,
    actual: str,
    member: PinnedPath,
    root: Path,
    paths: WindowsLocalPaths,
    deadline: VerificationDeadline,
) -> bool:
    if supplied == actual:
        return True
    deadline.check()
    try:
        with paths.pin(root.joinpath(*supplied.split("/"))) as alias:
            matches = alias.info.file_id == member.info.file_id
    except (OSError, ValueError):
        return False
    deadline.check()
    return matches


def _catalog_matches(
    decision: CatalogDecision,
    files: Mapping[str, PinnedPath],
    metadata_digests: Mapping[str, str],
    trusted: frozenset[str],
    root: Path,
    paths: WindowsLocalPaths,
    deadline: VerificationDeadline,
) -> bool:
    if not isinstance(decision, CatalogDecision) or decision.valid is not True:
        return False
    if not isinstance(decision.members, Mapping):
        return False
    members: dict[str, str] = {}
    for name, digest in decision.members.items():
        key = _relative_name(name)
        if key in members or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return False
        members[key] = digest.lower()
    member_names = WindowsFilenameIndex(members)
    if normalize_thumbprint(decision.signer) not in trusted or len(members) != len(files):
        return False
    for name, file in files.items():
        signed_name = member_names.find(name)
        if signed_name is None or not _opens_held_file(signed_name, name, file, root, paths, deadline):
            return False
        if name in metadata_digests and members[signed_name] != metadata_digests[name]:
            return False
    return True


def _verify_snapshot(
    root: PinnedPath,
    verifier: CatalogVerifier,
    authority: PublisherAuthority,
    paths: WindowsLocalPaths,
    locks: ExitStack,
    limits: VerificationLimits,
    deadline: VerificationDeadline,
    require_unchanged: Callable[[], None],
) -> BundleReport:
    inventory = _inventory(root.path, paths, locks, limits, deadline)
    files = {
        name: item for name, item in inventory.items() if not item.info.is_directory
    }
    names = WindowsFilenameIndex(files)
    manifest_name = names.find("manifest.json") or "manifest.json"
    approval_name = names.find("approval.json") or "approval.json"
    manifest, manifest_digest = _read_document(files.get(manifest_name), "manifest.json", limits)
    approval_document, approval_digest = _read_document(files.get(approval_name), "approval.json", limits)
    metadata_digests = {manifest_name: manifest_digest, approval_name: approval_digest}
    try:
        artifacts = _parse_manifest(manifest, limits.artifact_count)
    except ValueError:
        return _block(_PATH_FAILURE, "manifest.json", "invalid manifest schema")
    try:
        approval = _parse_approval(approval_document)
        provisioned = deadline.call(authority.approved_thumbprints)
        trusted = frozenset(normalize_thumbprint(value) for value in provisioned)
        if None in trusted or not trusted:
            raise ValueError("no valid publisher policy")
    except Exception:
        return _block(_APPROVAL_FAILURE, "approval.json", "invalid approval or machine publisher policy")
    trusted = trusted & approval.signers
    if not trusted:
        return _block(_CATALOG_FAILURE, "bundle.cat", "catalog or publisher verification failed")

    catalog_name = names.find("bundle.cat") or "bundle.cat"
    catalog = files.pop(catalog_name, None)
    if catalog is None or catalog.info.size > limits.catalog_bytes:
        return _block(_CATALOG_FAILURE, "bundle.cat", "missing, unsafe, or oversized catalog")

    payload = files.keys() - metadata_digests.keys()
    payload_names = WindowsFilenameIndex(payload)
    matched: dict[str, _Artifact] = {}
    failures = []
    for name, artifact in artifacts.items():
        actual_name = payload_names.find(name)
        if actual_name is None or not _opens_held_file(
            name, actual_name, files[actual_name], root.path, paths, deadline,
        ):
            failures.append(BundleFailure(_PATH_FAILURE, artifact.name, "missing payload"))
        else:
            matched[actual_name] = artifact
    if payload - matched.keys():
        failures.append(BundleFailure(_PATH_FAILURE, "manifest.json", "unlisted payload in bundle"))
    if failures:
        return BundleReport("BLOCK", None, tuple(failures))
    if sum(files[name].info.size for name in payload) > limits.payload_bytes:
        return _block(_PATH_FAILURE, "manifest.json", "aggregate payload limit exceeded")

    digests = dict(metadata_digests)
    for name, artifact in matched.items():
        actual = _sha256(files[name], deadline)
        digests[name] = actual
        if actual != artifact.sha256:
            failures.append(BundleFailure(_HASH_FAILURE, artifact.name, "hash mismatch"))
        elif not artifact.license_approved:
            failures.append(BundleFailure(_LICENSE_FAILURE, artifact.name, "license not approved"))
    digests[catalog_name] = _sha256(catalog, deadline)

    try:
        decision = deadline.call(verifier.verify, catalog, files, deadline)
        catalog_valid = _catalog_matches(
            decision, files, metadata_digests, trusted, root.path, paths, deadline,
        )
    except Exception:
        catalog_valid = False
    if not catalog_valid:
        failures.append(BundleFailure(_CATALOG_FAILURE, "bundle.cat", "catalog or publisher verification failed"))
    if approval.expires_at <= datetime.now(timezone.utc):
        failures.insert(0, BundleFailure(_APPROVAL_FAILURE, "approval.json", "approval expired"))
    if failures:
        return BundleReport("BLOCK", approval.identifier if catalog_valid else None, tuple(failures))

    _inventory(root.path, paths, locks, limits, deadline, inventory)
    with paths.pin_entry(root.path) as current_root:
        if current_root.info != root.info:
            return _block(_PATH_FAILURE, "manifest.json", "bundle directory changed")
    deadline.check()
    generation = hashlib.sha256(
        json.dumps(digests, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    require_unchanged()
    if approval.expires_at <= datetime.now(timezone.utc):
        expired = BundleFailure(_APPROVAL_FAILURE, "approval.json", "approval expired")
        return BundleReport("BLOCK", approval.identifier, (expired,))
    return BundleReport("PASS", approval.identifier, (), generation)


def verify_bundle(
    root: Path,
    verifier: CatalogVerifier,
    *,
    publisher_authority: PublisherAuthority | None = None,
    limits: VerificationLimits = VerificationLimits(),
    local_paths: WindowsLocalPaths | None = None,
) -> BundleReport:
    from raglab.offline.windows import MachinePublisherAuthority

    authority = publisher_authority if publisher_authority is not None else MachinePublisherAuthority()
    paths = local_paths if local_paths is not None else WindowsLocalPaths()
    deadline = VerificationDeadline(limits.seconds)
    try:
        with ExitStack() as locks:
            try:
                pinned = locks.enter_context(paths.pin(paths.validate(root)))
                if not pinned.info.is_directory:
                    raise ValueError("bundle root is not a directory")
            except (OSError, RuntimeError, ValueError):
                return _block(_PATH_FAILURE, ".", "missing or invalid local bundle directory")
            require_unchanged = locks.enter_context(paths.watch_namespace(pinned))
            return _verify_snapshot(pinned, verifier, authority, paths, locks, limits, deadline, require_unchanged)
    except _BundleBlocked as blocked:
        return BundleReport("BLOCK", None, (blocked.failure,))
    except (OSError, RuntimeError, ValueError):
        return _block(_PATH_FAILURE, "manifest.json", "bundle verification could not finish safely")
