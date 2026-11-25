from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock

import pytest

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.contracts.evidence import NativeLocator
from raglab.security.authorization import AuthorizationGuard, MemoryPolicyStore
from raglab.track_a.models import ArtifactProvenance, GroundingRequest, OcrToken, TargetRegion
from raglab.track_a.service import ApprovedArtifact, GroundingService


@pytest.fixture
def authorized_artifact():
    store = MemoryPolicyStore()
    store.allow("user", "doc", group_hash="g" * 64)
    guard = AuthorizationGuard.policy_only(store)
    pin = guard.pin("user", "g" * 64, "doc", "doc-v1", "gen-01")
    data = b"approved synthetic fixture"
    provenance = ArtifactProvenance(
        "b" * 64, "doc-v1", "gen-01", "artifact-1", sha256(data).hexdigest(),
        "image/svg+xml", "synthetic", "1", "c" * 64, 1000, 500, 144, 0, "crop",
        ((1., 0., 0., 0., 1., 0., 0., 0., 1.),), NativeLocator.page(1),
        pin.query_snapshot_id,
    )
    approved = ApprovedArtifact("doc", "fixture.svg", provenance, data)
    request = GroundingRequest("Where is A-12?", approved.artifact_path, provenance, pin)
    return store, guard, approved, request


def target(name="part-a", left=.2):
    return TargetRegion(name, ((left, .2), (left + .2, .2), (left + .2, .5), (left, .5)), "part")


def providers(tokens=None, regions=None):
    ocr, geometry = Mock(), Mock()
    ocr.read.return_value = (OcrToken("A-12", "en", (.1, .2, .1, .05), 1.),) if tokens is None else tokens
    geometry.read.return_value = (target(),) if regions is None else regions
    return ocr, geometry


def test_verified_uses_the_resolved_bytes_and_provenance(authorized_artifact):
    _, guard, approved, request = authorized_artifact
    resolver = Mock(return_value=approved)
    ocr, geometry = providers()

    response = GroundingService(guard, ocr, geometry, resolver=resolver).query(request)

    resolver.assert_called_once_with(request.pin, approved.provenance.artifact_id)
    ocr.read.assert_called_once_with(approved.data)
    geometry.read.assert_called_once_with(approved.data)
    assert response.state == "VERIFIED"
    (item,) = response.evidence
    assert item.text == "A-12"
    assert item.source_hash == approved.provenance.source_hash
    assert item.acl_snapshot_id == request.pin.query_snapshot_id
    assert item.regions[0].artifact_hash == approved.provenance.artifact_hash


@pytest.mark.parametrize(
    "tokens", [(), (OcrToken("A-120", "en", (.1, .2, .1, .05), 1.),)],
)
def test_absent_label_skips_geometry(authorized_artifact, tokens):
    _, guard, approved, request = authorized_artifact
    ocr, geometry = providers(tokens=tokens)
    response = GroundingService(guard, ocr, geometry, resolver=Mock(return_value=approved)).query(request)
    assert response.state == "PART_ABSENT"
    assert response.evidence == ()
    geometry.read.assert_not_called()


@pytest.mark.parametrize(
    "regions", [
        (), (target(left=.75),), (target(left=.53),),
        (target(), target("alternative", .21)), (target(), target("tie")),
    ],
)
def test_missing_distant_or_ambiguous_targets_abstain(authorized_artifact, regions):
    _, guard, approved, request = authorized_artifact
    ocr, geometry = providers(regions=regions)
    response = GroundingService(guard, ocr, geometry, resolver=Mock(return_value=approved)).query(request)
    assert response.state == "ABSTAIN"
    assert response.evidence == ()


def test_repeated_visible_label_abstains(authorized_artifact):
    _, guard, approved, request = authorized_artifact
    ocr, geometry = providers(tokens=(
        OcrToken("A-12", "en", (.1, .2, .1, .05), 1.),
        OcrToken("A-12", "en", (.6, .2, .1, .05), .9),
    ))
    response = GroundingService(guard, ocr, geometry, resolver=Mock(return_value=approved)).query(request)
    assert response.state == "ABSTAIN"
    assert response.evidence == ()
    geometry.read.assert_not_called()


@pytest.mark.parametrize("field", ["document_version", "generation_id", "acl_snapshot_id"])
def test_pin_rejects_mismatched_request_provenance_before_resolver(authorized_artifact, field):
    _, guard, approved, request = authorized_artifact
    request = replace(request, provenance=replace(request.provenance, **{field: "forged"}))
    resolver = Mock(return_value=approved)
    ocr, geometry = providers()
    with pytest.raises(RaglabError) as raised:
        GroundingService(guard, ocr, geometry, resolver=resolver).query(request)
    assert raised.value.code is ErrorCode.POLICY_DENIED
    resolver.assert_not_called()
    ocr.read.assert_not_called()
    geometry.read.assert_not_called()


@pytest.mark.parametrize(
    "attack", ["unknown", "wrong-document", "forged-path", "forged-source", "forged-self-hash", "changed-bytes"],
)
def test_only_the_bound_artifact_reaches_the_providers(authorized_artifact, attack):
    _, guard, approved, request = authorized_artifact
    expected = ErrorCode.POLICY_DENIED
    if attack == "unknown":
        approved = None
    elif attack == "wrong-document":
        approved = replace(approved, document_id="different-document")
    elif attack == "forged-path":
        request = replace(request, artifact_path="unauthorized.svg")
    elif attack == "forged-source":
        request = replace(request, provenance=replace(request.provenance, source_hash="f" * 64))
    elif attack == "forged-self-hash":
        request = replace(
            request, artifact_path="unauthorized.svg",
            provenance=replace(request.provenance, artifact_hash=sha256(b"unauthorized").hexdigest()),
        )
    else:
        approved = replace(approved, data=b"tampered")
        expected = ErrorCode.ARTIFACT_HASH_MISMATCH
    ocr, geometry = providers()
    with pytest.raises(RaglabError) as raised:
        GroundingService(guard, ocr, geometry, resolver=Mock(return_value=approved)).query(request)
    assert raised.value.code is expected
    assert "unauthorized" not in repr(raised.value.to_safe_dict())
    ocr.read.assert_not_called()
    geometry.read.assert_not_called()


@pytest.mark.parametrize("stage", ["before-retrieval", "resolver", "ocr", "absent-ocr", "geometry", "highlight"])
def test_revocation_discards_results_and_stops_subsequent_reads(authorized_artifact, stage):
    store, guard, approved, request = authorized_artifact
    resolver = Mock(return_value=approved)
    ocr, geometry = providers()

    def revoke_and_return(value):
        store.revoke("doc")
        return value

    if stage == "before-retrieval":
        store.revoke("doc")
    elif stage == "resolver":
        resolver.side_effect = lambda *_: revoke_and_return(approved)
    elif stage in ("ocr", "absent-ocr"):
        tokens = ocr.read.return_value if stage == "ocr" else ()
        ocr.read.side_effect = lambda _: revoke_and_return(tokens)
    elif stage == "geometry":
        regions = geometry.read.return_value
        geometry.read.side_effect = lambda _: revoke_and_return(regions)
    else:
        check = guard.check

        def revoke_after_highlight(pin, stage, diagnostic_id=None):
            check(pin, stage, diagnostic_id)
            if stage == "highlight_open":
                store.revoke("doc")

        guard.check = revoke_after_highlight

    with pytest.raises(RaglabError) as raised:
        GroundingService(guard, ocr, geometry, resolver=resolver).query(request)
    assert raised.value.code is ErrorCode.AUTHORIZATION_CHANGED
    if stage == "before-retrieval":
        resolver.assert_not_called()
    if stage in ("before-retrieval", "resolver"):
        ocr.read.assert_not_called()
    if stage in ("before-retrieval", "resolver", "ocr", "absent-ocr"):
        geometry.read.assert_not_called()
    if stage == "highlight":
        assert raised.value.stage == "serialize"


@pytest.mark.parametrize("provider", ["ocr", "geometry"])
@pytest.mark.parametrize("revoke", [False, True])
def test_provider_errors_are_masked_and_authorization_takes_precedence(
    authorized_artifact, provider, revoke,
):
    store, guard, approved, request = authorized_artifact
    ocr, geometry = providers()

    def fail(_):
        if revoke:
            store.revoke("doc")
        raise OSError("private fixture full path")

    (ocr if provider == "ocr" else geometry).read.side_effect = fail
    with pytest.raises(RaglabError) as raised:
        GroundingService(guard, ocr, geometry, resolver=Mock(return_value=approved)).query(request)

    expected = ErrorCode.AUTHORIZATION_CHANGED if revoke else ErrorCode.INVALID_EVIDENCE
    assert raised.value.code is expected
    assert raised.value.stage == "source_load"
    assert raised.value.__context__ is None
    assert "private fixture" not in repr(raised.value.to_safe_dict())
    if provider == "ocr":
        geometry.read.assert_not_called()
