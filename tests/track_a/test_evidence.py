from dataclasses import replace

from raglab.contracts.evidence import EvidenceRole
from raglab.track_a.evidence import candidate_to_evidence
from raglab.track_a.models import ArtifactProvenance, GroundingCandidate, OcrToken, TargetRegion
from raglab.contracts.evidence import NativeLocator


def _inputs() -> tuple[GroundingCandidate, ArtifactProvenance]:
    candidate = GroundingCandidate(
        OcrToken("A-12", "en", (.1, .2, .1, .05), .96),
        TargetRegion("part-a", ((.2, .2), (.4, .2), (.4, .5), (.2, .5)), "part"),
        .88,
    )
    provenance = ArtifactProvenance(
        "b" * 64, "doc-v1", "gen-01", "artifact-01", "a" * 64, "image/png",
        "synthetic", "1.0", "c" * 64, 1000, 500, 144, 0, "crop",
        ((1., 0., 0., 0., 1., 0., 0., 0., 1.),), NativeLocator.page(1), "acl-01",
    )
    return candidate, provenance


def test_paired_regions_and_relation() -> None:
    candidate, provenance = _inputs()
    evidence = candidate_to_evidence(candidate, provenance)
    assert [region.role for region in evidence.regions] == [EvidenceRole.LABEL, EvidenceRole.TARGET]
    assert evidence.relations[0].target == evidence.regions[0].region_id
    assert evidence.relations[0].related == evidence.regions[1].region_id
    assert evidence.relations[0].target_region_id == evidence.regions[1].region_id


def test_repeated_label_occurrences_have_distinct_structured_ids() -> None:
    candidate, provenance = _inputs()
    second = replace(candidate, token=replace(candidate.token, box=(.6, .2, .1, .05)))
    first_evidence = candidate_to_evidence(candidate, provenance)
    second_evidence = candidate_to_evidence(second, provenance)
    assert first_evidence.evidence_id != second_evidence.evidence_id
    assert first_evidence.regions[0].region_id != second_evidence.regions[0].region_id


def test_same_structured_candidate_is_deterministic() -> None:
    candidate, provenance = _inputs()
    assert candidate_to_evidence(candidate, provenance).evidence_id == candidate_to_evidence(candidate, provenance).evidence_id


def test_conversion_errors_do_not_echo_ocr_content() -> None:
    candidate, provenance = _inputs()
    object.__setattr__(candidate.token, "box", (.99, .2, .1, .05))
    try:
        candidate_to_evidence(candidate, provenance)
    except ValueError as error:
        assert str(error) == "candidate evidence conversion failed"
        assert candidate.token.text not in str(error)
    else:
        raise AssertionError("invalid geometry should not convert")
