import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_MANIFEST_KINDS = frozenset({"text", "structure", "visual"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class GenerationState(StrEnum):
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True, slots=True)
class IndexManifest:
    kind: str
    sha256: str
    row_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _MANIFEST_KINDS:
            raise ValueError("manifest kind must be text, structure, or visual")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("manifest SHA-256 must contain 64 lowercase hex digits")
        if type(self.row_count) is not int or self.row_count < 0:
            raise ValueError("manifest row_count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class IndexGeneration:
    generation_id: str
    document_version: str
    source_hash: str
    schema_version: str
    acl_projection_version: int
    manifests: tuple[IndexManifest, ...]
    state: GenerationState

    def __post_init__(self) -> None:
        for field in ("generation_id", "document_version", "schema_version"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
        if not isinstance(self.source_hash, str) or _SHA256.fullmatch(self.source_hash) is None:
            raise ValueError("source SHA-256 must contain 64 lowercase hex digits")
        if type(self.acl_projection_version) is not int:
            raise ValueError("acl_projection_version must be an integer")
        if not isinstance(self.state, GenerationState):
            raise ValueError("state must be a GenerationState")
        manifests = tuple(self.manifests)
        if (
            len(manifests) != len(_MANIFEST_KINDS)
            or any(not isinstance(manifest, IndexManifest) for manifest in manifests)
            or {manifest.kind for manifest in manifests} != _MANIFEST_KINDS
        ):
            raise ValueError("generation must contain exactly one text, structure, and visual manifest")
        object.__setattr__(self, "manifests", manifests)


class JsonGenerationCatalog:
    """Store one ACTIVE generation in an atomically replaced local JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path.absolute()
        self._lock_path = self._path.with_name(f"{self._path.name}.lock")

    @property
    def temp_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}.tmp")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # Keep the lock file separate from the replaced catalog and temporary file.
        # Closing the handle releases the OS lock, including after process exit.
        with self._lock_path.open("a+b", buffering=0) as lock_file:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield

    def _read_active(self) -> IndexGeneration | None:
        try:
            with self._path.open(encoding="utf-8") as catalog_file:
                data = json.load(catalog_file)
        except FileNotFoundError:
            return None

        state = GenerationState(data["state"])
        if state is not GenerationState.ACTIVE:
            raise ValueError("catalog must contain an ACTIVE generation")
        data["state"] = state
        data["manifests"] = tuple(IndexManifest(**manifest) for manifest in data["manifests"])
        return IndexGeneration(**data)

    def read_active(self) -> IndexGeneration | None:
        if not self._path.exists():
            return None
        with self._locked():
            return self._read_active()

    def promote(
        self, candidate: IndexGeneration, expected_active_id: str | None
    ) -> IndexGeneration:
        if candidate.state is not GenerationState.VALIDATED:
            raise ValueError("only a VALIDATED generation may be promoted")

        with self._locked():
            current = self._read_active()
            current_id = current.generation_id if current is not None else None
            if current_id != expected_active_id:
                raise RaglabError(
                    code=ErrorCode.GENERATION_CONFLICT,
                    stage="promote",
                    observed="active generation changed",
                    expected="active generation matches the caller's snapshot",
                    protected_state="active catalog unchanged",
                    retry=RetryAdvice.ONCE_FROM_FRESH_SNAPSHOT,
                    next_action="read the active generation before retrying promotion",
                    diagnostic_id=uuid4().hex,
                )

            active = replace(candidate, state=GenerationState.ACTIVE)
            self.temp_path.unlink(missing_ok=True)
            with self.temp_path.open("x", encoding="utf-8") as temp_file:
                json.dump(asdict(active), temp_file)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(self.temp_path, self._path)
        return active

    def recover(self) -> None:
        if self._path.parent.exists():
            with self._locked():
                self.temp_path.unlink(missing_ok=True)
