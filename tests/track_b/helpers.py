from hashlib import sha256

from raglab.contracts import EvidenceRegionV1, EvidenceRole, ExtractionMethod, NativeLocator


def numeric_evidence(values: dict[str, str]) -> tuple[EvidenceRegionV1, ...]:
    return tuple(
        EvidenceRegionV1(
            evidence_id="ev-" + sha256(reference.encode()).hexdigest()[:24],
            source_hash="0" * 64,
            document_version="doc-test-v1",
            generation_id="gen-test-1",
            native_locator=NativeLocator.sheet_range("sheet-1", reference),
            role=EvidenceRole.VALUE,
            text=value,
            regions=(),
            relations=(),
            extraction_method=ExtractionMethod.NATIVE,
            confidence=1.0,
            acl_snapshot_id="acl-test",
            metadata={"formula": None, "formula_state": "literal"},
        )
        for reference, value in sorted(values.items())
    )
