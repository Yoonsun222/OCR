import raglab.contracts as contracts


def test_common_contract_exports_are_frozen() -> None:
    assert set(contracts.__all__) == {
        "BBox", "ErrorCode", "EvidenceRegion", "EvidenceRegionV1",
        "EvidenceRelation", "EvidenceRole", "ExtractionMethod", "NativeLocator",
        "Point", "RaglabError", "RetryAdvice", "validate_artifact_match",
    }
