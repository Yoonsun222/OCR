import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from raglab.offline.ports import CatalogDecision, CatalogVerifier, normalize_thumbprint


_PATH_FAILURE = "RAGLAB-BUNDLE-001"
_HASH_FAILURE = "RAGLAB-BUNDLE-002"
_CATALOG_FAILURE = "RAGLAB-BUNDLE-003"
_APPROVAL_FAILURE = "RAGLAB-BUNDLE-004"
_LICENSE_FAILURE = "RAGLAB-BUNDLE-005"


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
    parts: tuple[str, ...]
    sha256: str
    license_approved: bool


@dataclass(frozen=True)
class _Approval:
    identifier: str
    expires_at: datetime
    signers: frozenset[str]


def _block(code: str, artifact: str, reason: str) -> BundleReport:
    return BundleReport("BLOCK", None, (BundleFailure(code, artifact, reason),))


def _relative_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("path must be text")
    parts = tuple(value.replace("\\", "/").split("/"))
    for part in parts:
        if (
            not part
            or part.endswith((" ", "."))
            or PureWindowsPath(part).is_reserved()
            or any(
                char in '<>:"|?*' or ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
                for char in part
            )
        ):
            raise ValueError("path is not a safe relative path")
    return parts


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _bundle_file(root: Path, parts: tuple[str, ...]) -> Path:
    path = root
    for part in parts:
        path = path / part
        info = path.lstat()
        if _is_link(info):
            raise ValueError("file traverses a link")
    resolved = path.resolve(strict=True)
    if not stat.S_ISREG(info.st_mode) or not resolved.is_relative_to(root):
        raise ValueError("file is outside the bundle or is not a regular file")
    return resolved


def _unsafe_tree_member(root: Path) -> str | None:
    # Windows catalog membership checks walk the whole tree, including files unlisted
    # in the manifest. Reject links and special files before starting that walk.
    directories = [root]
    while directories:
        for path in sorted(directories.pop().iterdir()):
            relative = path.relative_to(root).as_posix()
            _relative_parts(relative)
            info = path.lstat()
            if _is_link(info):
                return relative
            if stat.S_ISDIR(info.st_mode):
                directories.append(path)
            elif not stat.S_ISREG(info.st_mode):
                return relative
    return None


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _read_document(root: Path, name: str) -> tuple[dict[str, object], str]:
    raw = _bundle_file(root, (name,)).read_bytes()
    value = json.loads(
        raw.decode("utf-8-sig"),
        object_pairs_hook=_json_object,
        parse_constant=_invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _read_manifest(root: Path) -> tuple[tuple[_Artifact, ...], str]:
    document, digest = _read_document(root, "manifest.json")
    version = document.get("schema_version")
    rows = document.get("artifacts")
    if type(version) is not int or version != 1 or not isinstance(rows, list) or not rows:
        raise ValueError("invalid manifest schema")

    artifacts = []
    paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed artifact")
        parts = _relative_parts(row.get("path"))
        sha256 = row.get("sha256")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValueError("invalid artifact digest")
        path_key = "/".join(parts).casefold()
        if path_key in paths:
            raise ValueError("duplicate artifact path")
        paths.add(path_key)
        artifacts.append(_Artifact(parts, sha256.lower(), row.get("license_decision") == "approved"))
    return tuple(artifacts), digest


def _read_approval(root: Path) -> tuple[_Approval, str]:
    document, digest = _read_document(root, "approval.json")
    identifier = document.get("approval_id")
    expires = document.get("expires_at")
    allowlist = document.get("allowed_signer_thumbprints")
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identifier,
    ):
        raise ValueError("invalid approval identifier")
    if not isinstance(expires, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[-+]\d{2}:\d{2})",
        expires,
    ):
        raise ValueError("expected an expiration with a time zone")
    if not isinstance(allowlist, list) or not allowlist:
        raise ValueError("expected a signer allowlist")
    signers = set()
    for value in allowlist:
        signer = normalize_thumbprint(value)
        if signer is None:
            raise ValueError("invalid signer thumbprint")
        signers.add(signer)
    return _Approval(identifier, datetime.fromisoformat(expires), frozenset(signers)), digest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_artifacts(root: Path, artifacts: tuple[_Artifact, ...]) -> list[BundleFailure]:
    failures: list[BundleFailure] = []
    resolved: set[str] = set()
    for artifact in artifacts:
        name = "/".join(artifact.parts)
        try:
            path = _bundle_file(root, artifact.parts)
            path_key = path.relative_to(root).as_posix().casefold()
            if path_key in resolved:
                failures.append(BundleFailure(_PATH_FAILURE, "manifest.json", "duplicate artifact path"))
                continue
            resolved.add(path_key)
            actual = _sha256(path)
        except (OSError, RuntimeError, ValueError):
            failures.append(BundleFailure(_PATH_FAILURE, name, "missing or unsafe artifact"))
            continue

        if actual != artifact.sha256:
            failures.append(BundleFailure(_HASH_FAILURE, name, "hash mismatch"))
        elif not artifact.license_approved:
            failures.append(BundleFailure(_LICENSE_FAILURE, name, "license not approved"))
    return failures


def _valid_catalog(
    catalog_path: Path,
    verifier: CatalogVerifier,
    signers: frozenset[str],
    manifest_digest: str,
    approval_digest: str,
) -> bool:
    try:
        decision = verifier.verify(catalog_path)
    except Exception:
        # OS or verifier errors can contain the operator's absolute path.
        return False
    if not isinstance(decision, CatalogDecision) or decision.valid is not True:
        return False
    if normalize_thumbprint(decision.signer) not in signers:
        return False
    attested = decision.manifest_sha256, decision.approval_sha256
    return attested == (None, None) or attested == (manifest_digest, approval_digest)


def verify_bundle(root: Path, verifier: CatalogVerifier) -> BundleReport:
    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("bundle root is not a directory")
    except (OSError, RuntimeError, ValueError):
        return _block(_PATH_FAILURE, ".", "missing or invalid bundle directory")

    try:
        artifacts, manifest_digest = _read_manifest(root)
    except (OSError, RuntimeError, ValueError):
        return _block(_PATH_FAILURE, "manifest.json", "missing or invalid manifest")
    try:
        approval, approval_digest = _read_approval(root)
    except (OSError, RuntimeError, ValueError):
        return _block(_APPROVAL_FAILURE, "approval.json", "missing or invalid approval")
    try:
        catalog_path = _bundle_file(root, ("bundle.cat",))
    except (OSError, RuntimeError, ValueError):
        return _block(_CATALOG_FAILURE, "bundle.cat", "missing or unsafe catalog")
    try:
        unsafe_member = _unsafe_tree_member(root)
    except (OSError, RuntimeError, ValueError):
        return _block(_PATH_FAILURE, "manifest.json", "bundle contains an unreadable or unsafe path")
    if unsafe_member is not None:
        return _block(_PATH_FAILURE, unsafe_member, "bundle member is a link or special file")

    failures = _check_artifacts(root, artifacts)
    catalog_valid = _valid_catalog(
        catalog_path, verifier, approval.signers, manifest_digest, approval_digest,
    )
    if not catalog_valid:
        failures.append(
            BundleFailure(_CATALOG_FAILURE, "bundle.cat", "catalog or signer verification failed")
        )
    if approval.expires_at <= datetime.now(timezone.utc):
        failures.insert(0, BundleFailure(_APPROVAL_FAILURE, "approval.json", "approval expired"))
    return BundleReport(
        "BLOCK" if failures else "PASS",
        approval.identifier if catalog_valid else None,
        tuple(failures),
    )
