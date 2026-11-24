import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from raglab.offline.windows_paths import PinnedPath


@dataclass(frozen=True)
class VerificationLimits:
    metadata_bytes: int = 4 * 1024 * 1024
    catalog_bytes: int = 32 * 1024 * 1024
    artifact_count: int = 4096
    inventory_entries: int = 8192
    inventory_depth: int = 32
    payload_bytes: int = 128 * 1024**3
    seconds: float = 240


class VerificationDeadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = monotonic):
        self._clock = clock
        self._expires = clock() + seconds

    def check(self) -> None:
        if self._clock() >= self._expires:
            raise TimeoutError("verification deadline exceeded")

    def call(self, operation, *args):
        self.check()
        result = operation(*args)
        self.check()
        return result


@dataclass(frozen=True)
class CatalogDecision:
    valid: bool
    signer: str | None
    reason: str | None
    members: Mapping[str, str] | None = None


class CatalogVerifier(Protocol):
    def verify(
        self,
        catalog: "PinnedPath",
        files: Mapping[str, "PinnedPath"],
        deadline: VerificationDeadline,
    ) -> CatalogDecision:
        """Authenticate the catalog and every held file, returning signed SHA-256 members."""
        ...


class PublisherAuthority(Protocol):
    def approved_thumbprints(self) -> frozenset[str]:
        """Return publishers approved by authority independent from the bundle."""
        ...


def normalize_thumbprint(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.upper()
    return None
