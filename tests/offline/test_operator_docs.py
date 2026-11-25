from pathlib import Path


DOCS = Path("docs/operator")


def test_golden_path():
    text = (DOCS / "START-HERE.md").read_text(encoding="utf-8")
    for command in ("verify-bundle", "doctor --profile local16 --offline", "demo --profile local16"):
        assert command in text
    assert "30분" in text


def test_operator_handoff():
    for name in ("OFFLINE-INSTALL", "CONFIGURATION", "REPRODUCIBILITY", "SECURITY-REVIEW"):
        assert (DOCS / f"{name}.md").is_file()
    text = (DOCS / "ERROR-CATALOG.md").read_text(encoding="utf-8")
    for category in ("BUNDLE", "CONFIG", "DOCTOR", "MEM", "REPRO", "INSTALL", "UNINSTALL"):
        assert f"RAGLAB-{category}-" in text
