import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError

import pytest

from raglab.contracts import ErrorCode, RaglabError, RetryAdvice


def test_safe_error_output_excludes_sensitive_context():
    error = RaglabError(
        ErrorCode.INVALID_EVIDENCE,
        "verify",
        "evidence did not validate",
        "validated evidence",
        "no result released",
        RetryAdvice.NEVER,
        "review the evidence",
        "diag-001",
        {"source": "secret.pdf", "question": "part A?"},
    )

    safe = error.to_safe_dict()
    assert safe == {
        "code": "INVALID_EVIDENCE",
        "stage": "verify",
        "observed": "evidence did not validate",
        "expected": "validated evidence",
        "protected_state": "no result released",
        "retry": "NEVER",
        "next_action": "review the evidence",
        "diagnostic_id": "diag-001",
    }
    for output in (json.dumps(safe), repr(error), str(error)):
        assert "secret.pdf" not in output
        assert "part A?" not in output


def test_policy_denied_fails_closed():
    @contextmanager
    def operation():
        yield

    error = RaglabError.policy_denied(stage="retrieve", diagnostic_id="diag-002")

    assert isinstance(error, Exception)
    assert error.code is ErrorCode.POLICY_DENIED
    assert error.stage == "retrieve"
    assert error.protected_state == "no source or result released"
    assert error.retry is RetryAdvice.NEVER
    assert error.diagnostic_id == "diag-002"

    with pytest.raises(RaglabError) as caught:
        with operation():
            raise error

    assert caught.value is error
    error.add_note("retry disabled")
    assert error.__notes__ == ["retry disabled"]

    with pytest.raises(FrozenInstanceError):
        error.stage = "changed"
    with pytest.raises(FrozenInstanceError):
        del error.stage
