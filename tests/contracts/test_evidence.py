import json
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import raglab.contracts as contracts
from raglab.contracts.errors import ErrorCode, RaglabError, RetryAdvice
from raglab.contracts.evidence import (
    BBox,
    EvidenceRegion,
    EvidenceRegionV1,
    EvidenceRelation,
    EvidenceRole,
    ExtractionMethod,
    NativeLocator,
    Point,
    validate_artifact_match,
)


_FIXTURE = Path(__file__).parents[1] / "fixtures" / "evidence" / "label_target.json"


def valid_evidence() -> EvidenceRegionV1:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    target = fixture["target"]
    return EvidenceRegionV1(
        evidence_id="ev_01",
        source_hash=fixture["source_hash"],
        document_version="doc-v1",
        generation_id="gen_01",
        native_locator=NativeLocator.page(1),
        role=EvidenceRole.TARGET,
        text="A-12",
        extraction_method=ExtractionMethod.SYNTHETIC,
        confidence=1.0,
        acl_snapshot_id="acl-01",
        metadata={},
        regions=(
            EvidenceRegion(
                region_id="ev_01:target",
                artifact_id="artifact_01",
                artifact_hash=fixture["artifact_hash"],
                artifact_mime_type="image/png",
                renderer_name="synthetic",
                renderer_version="1.0",
                render_profile_hash="c" * 64,
                canvas_width_px=1000,
                canvas_height_px=800,
                dpi=144,
                page_rotation=0,
                page_box="crop",
                polygon=tuple(Point(*point) for point in target["polygon"]),
                bbox=BBox(0.4, 0.2, 0.3, 0.4),
                transform_chain=((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),),
                role=EvidenceRole(target["role"]),
            ),
        ),
        relations=(EvidenceRelation(EvidenceRole.TARGET, "ev_01:target", None),),
    )


def test_valid_evidence_round_trips_to_json_safe_dict() -> None:
    data = valid_evidence().to_dict()
    assert json.loads(json.dumps(data, allow_nan=False)) == data
    assert data["schema_version"] == "evidence-region.v1"
    assert data["regions"][0]["coordinate_space"] == "rendered_top_left_normalized"
    assert data["native_locator"] == {"kind": "page", "page_number": 1}


def test_native_evidence_may_omit_rendered_regions() -> None:
    item = EvidenceRegionV1(
        evidence_id="cell-01",
        source_hash="b" * 64,
        document_version="doc-v1",
        generation_id="gen_01",
        native_locator=NativeLocator.sheet_range("sheet-1", "B2"),
        role=EvidenceRole.VALUE,
        text="12.5",
        regions=(),
        relations=(),
        extraction_method=ExtractionMethod.NATIVE,
        confidence=1.0,
        acl_snapshot_id="acl-01",
        metadata={"unit": "MPa"},
    )
    assert item.regions == ()


@pytest.mark.parametrize("value", [-0.01, 1.01, math.inf, math.nan])
def test_point_rejects_out_of_range_or_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError):
        Point(value, 0.5)


def test_bbox_rejects_zero_area() -> None:
    with pytest.raises(ValueError):
        BBox(0.1, 0.2, 0.0, 0.4)


def test_highlight_rejects_wrong_artifact_hash() -> None:
    with pytest.raises(RaglabError) as raised:
        validate_artifact_match(valid_evidence(), "ev_01:target", "d" * 64, "diag_hash")
    assert raised.value.code is ErrorCode.ARTIFACT_HASH_MISMATCH
    assert raised.value.retry is RetryAdvice.NEVER
    assert raised.value.expected == "a" * 64


def test_highlight_accepts_only_an_existing_region_and_its_exact_hash() -> None:
    item = valid_evidence()
    validate_artifact_match(item, "ev_01:target", "a" * 64, "diag_hash")
    with pytest.raises(RaglabError) as raised:
        validate_artifact_match(item, "absent", "a" * 64, "diag_hash")
    assert raised.value.code is ErrorCode.ARTIFACT_HASH_MISMATCH
    assert raised.value.expected == "missing region"


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_hash": "A" * 64},
        {"render_profile_hash": "z" * 64},
        {"canvas_width_px": 0},
        {"canvas_height_px": -1},
        {"dpi": math.inf},
        {"page_rotation": 45},
        {"polygon": (Point(0.2, 0.3), Point(0.3, 0.4))},
        {"transform_chain": ()},
        {"transform_chain": ((1.0,) * 8,)},
        {"transform_chain": ((math.nan,) * 9,)},
    ],
)
def test_region_rejects_invalid_render_provenance_or_geometry(changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(valid_evidence().regions[0], **changes)


def test_bbox_rejects_overflow_and_invalid_origin() -> None:
    for arguments in ((0.9, 0.2, 0.2, 0.3), (-0.1, 0.2, 0.2, 0.3)):
        with pytest.raises(ValueError):
            BBox(*arguments)


def test_native_locator_factories_expose_only_the_tagged_fields() -> None:
    assert NativeLocator.slide(2).to_dict() == {"kind": "slide", "slide_number": 2}
    cell = NativeLocator.sheet_range("sheet-1", "B2")
    assert cell.to_dict() == {"kind": "sheet_range", "sheet_id": "sheet-1", "cell_range": "B2"}
    assert cell.cell_range == "B2"
    assert NativeLocator.sheet_object("sheet-1", "chart").object_id == "chart"
    assert NativeLocator.object("shape").to_dict() == {"kind": "object", "object_id": "shape"}
    with pytest.raises(ValueError):
        NativeLocator.page(0)
    with pytest.raises(TypeError):
        NativeLocator("page", page_number=1)


def test_native_relation_accepts_an_explicit_formula_or_range() -> None:
    item = replace(
        valid_evidence(),
        native_locator=NativeLocator.sheet_object("sheet-1", "chart"),
        extraction_method=ExtractionMethod.NATIVE,
        regions=(),
        relations=(
            EvidenceRelation(EvidenceRole.SERIES, "'Data'!$A$2:$A$5", "category"),
            EvidenceRelation(EvidenceRole.VALUE, "=SUM(B2:B4)", None),
        ),
    )
    assert item.relations[0].target == "'Data'!$A$2:$A$5"


def test_rendered_relation_requires_both_region_ids_to_exist() -> None:
    item = valid_evidence()
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    label = fixture["label"]
    label_region = replace(
        item.regions[0],
        region_id="ev_01:label",
        bbox=BBox(*label["bbox"]),
        polygon=(
            Point(0.1, 0.2), Point(0.3, 0.2), Point(0.3, 0.3), Point(0.1, 0.3),
        ),
        role=EvidenceRole(label["role"]),
    )
    item = replace(
        item,
        regions=(label_region, *item.regions),
        relations=(EvidenceRelation(EvidenceRole.LABEL, "ev_01:label", "ev_01:target"),),
    )
    assert item.relations[0].target_region_id == "ev_01:target"
    with pytest.raises(ValueError):
        replace(
            item,
            relations=(EvidenceRelation(EvidenceRole.LABEL, "ev_01:label", "missing"),),
        )


def test_envelope_rejects_unknown_relation_targets() -> None:
    with pytest.raises(ValueError):
        replace(
            valid_evidence(),
            relations=(EvidenceRelation(EvidenceRole.TARGET, "missing", None),),
        )


def test_only_native_evidence_may_omit_rendered_regions() -> None:
    with pytest.raises(ValueError):
        replace(valid_evidence(), regions=(), relations=())


def test_rendered_regions_have_unambiguous_ids_for_highlighting() -> None:
    item = valid_evidence()
    duplicate = replace(item.regions[0], artifact_hash="d" * 64)
    with pytest.raises(ValueError):
        replace(item, regions=(*item.regions, duplicate))


def test_evidence_types_are_exported_from_the_contract_package() -> None:
    for public in (
        BBox, EvidenceRegion, EvidenceRegionV1, EvidenceRelation, EvidenceRole,
        ExtractionMethod, NativeLocator, Point, validate_artifact_match,
    ):
        assert public.__name__ in contracts.__all__
        assert getattr(contracts, public.__name__) is public


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.nan])
def test_envelope_rejects_invalid_confidence(confidence: float) -> None:
    with pytest.raises(ValueError):
        replace(valid_evidence(), confidence=confidence)


def test_envelope_rejects_non_json_metadata() -> None:
    with pytest.raises(ValueError):
        replace(valid_evidence(), metadata={"invalid": [math.nan]})


def test_evidence_copies_and_freezes_nested_metadata() -> None:
    metadata = {"units": [{"symbol": "MPa"}]}
    item = replace(valid_evidence(), metadata=metadata)
    metadata["units"][0]["symbol"] = "kPa"
    assert item.to_dict()["metadata"] == {"units": [{"symbol": "MPa"}]}
    with pytest.raises(TypeError):
        item.metadata["units"][0]["symbol"] = "Pa"
    with pytest.raises(FrozenInstanceError):
        item.schema_version = "other"
