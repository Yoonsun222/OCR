import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CatalogDecision:
    valid: bool
    signer: str | None
    reason: str | None
    manifest_sha256: str | None = None
    approval_sha256: str | None = None


class CatalogVerifier(Protocol):
    def verify(self, catalog_path: Path) -> CatalogDecision:
        """Authenticate the catalog and its bundle membership.

        The production verifier attests the exact bytes of both metadata files.
        Injected verifiers may attest membership directly without returning digests.
        """
        ...


def normalize_thumbprint(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.upper()
    return None
