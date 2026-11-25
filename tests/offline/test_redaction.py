import pytest

from raglab.offline.progress import Redactor


def test_redactor_removes_forbidden_payload_fields_recursively():
    value = {
        "document_name": "secret.pdf",
        "ocr_text": "secret",
        "events": [{"documentPath": "private.pdf", "ToKeN": {"value": "xyz"}}],
        "run_id": "run-1",
    }
    assert Redactor().redact(value) == {
        "document_name": "[REDACTED]",
        "ocr_text": "[REDACTED]",
        "events": [{"documentPath": "[REDACTED]", "ToKeN": "[REDACTED]"}],
        "run_id": "run-1",
    }


@pytest.mark.parametrize("message", [
    r'could not read "C:\Users\Alice Doe\file.pdf"',
    "could not read /home/alice/private.pdf",
    r"could not read \\server\share\Alice\private.pdf",
    "could not read C:/Users/Alice/private.pdf",
])
def test_redactor_removes_absolute_paths(message):
    assert Redactor().redact(message) == "[REDACTED]"
