from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ErrorCode(StrEnum):
    AUTHORIZATION_CHANGED = "AUTHORIZATION_CHANGED"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    POLICY_DENIED = "POLICY_DENIED"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    GENERATION_CONFLICT = "GENERATION_CONFLICT"
    DELETION_VERIFICATION_FAILED = "DELETION_VERIFICATION_FAILED"
    DEGRADED_RESOURCE_MODE = "DEGRADED_RESOURCE_MODE"


class RetryAdvice(StrEnum):
    NEVER = "NEVER"
    SAFE = "SAFE"
    ONCE_FROM_FRESH_SNAPSHOT = "ONCE_FROM_FRESH_SNAPSHOT"


@dataclass(frozen=True, slots=True)
class RaglabError(Exception):
    code: ErrorCode
    stage: str
    observed: str
    expected: str
    protected_state: str
    retry: RetryAdvice
    next_action: str
    diagnostic_id: str
    sensitive_context: Mapping[str, str] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        # Exception normally retains positional arguments for its string form.
        Exception.__init__(self, self.code.value)

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "stage": self.stage,
            "observed": self.observed,
            "expected": self.expected,
            "protected_state": self.protected_state,
            "retry": self.retry.value,
            "next_action": self.next_action,
            "diagnostic_id": self.diagnostic_id,
        }

    @classmethod
    def policy_denied(cls, stage: str, diagnostic_id: str) -> "RaglabError":
        return cls(
            code=ErrorCode.POLICY_DENIED,
            stage=stage,
            observed="policy did not grant access",
            expected="explicit policy authorization",
            protected_state="no source or result released",
            retry=RetryAdvice.NEVER,
            next_action="verify access with the policy owner",
            diagnostic_id=diagnostic_id,
        )
