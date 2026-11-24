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
