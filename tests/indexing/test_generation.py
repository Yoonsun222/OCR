import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Barrier

import pytest

from raglab.contracts.errors import ErrorCode, RaglabError
from raglab.indexing.generation import (
    GenerationState,
    IndexGeneration,
    IndexManifest,
    JsonGenerationCatalog,
)


def candidate(generation_id: str) -> IndexGeneration:
    manifests = tuple(
        IndexManifest(kind=kind, sha256=digit * 64, row_count=3)
        for kind, digit in (("text", "a"), ("structure", "b"), ("visual", "c"))
    )
    return IndexGeneration(
        generation_id, "doc-v1", "d" * 64, "evidence-region.v1", 4,
        manifests, GenerationState.VALIDATED,
    )


def test_promote_persists_an_active_copy(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    generation = candidate("gen_01")

    assert catalog.read_active() is None
    active = catalog.promote(generation, expected_active_id=None)

    assert active == replace(generation, state=GenerationState.ACTIVE)
    assert generation.state is GenerationState.VALIDATED
    assert JsonGenerationCatalog(tmp_path / "catalog.json").read_active() == active
    assert not catalog.temp_path.exists()


def test_update_requires_the_current_active_id(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    catalog.promote(candidate("gen_01"), expected_active_id=None)

    active = catalog.promote(candidate("gen_02"), expected_active_id="gen_01")

    assert active.generation_id == "gen_02"
    assert catalog.read_active() == active


def test_stale_compare_and_swap_preserves_current_active(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    catalog = JsonGenerationCatalog(path)
    active = catalog.promote(candidate("gen_01"), expected_active_id=None)
    original_catalog = path.read_bytes()
    next_generation = candidate("gen_02")

    with pytest.raises(RaglabError) as raised:
        catalog.promote(next_generation, expected_active_id=None)

    assert raised.value.code is ErrorCode.GENERATION_CONFLICT
    assert path.read_bytes() == original_catalog
    assert catalog.read_active() == active
    assert next_generation.state is GenerationState.VALIDATED
    assert not catalog.temp_path.exists()


def test_concurrent_promotions_have_only_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    catalog = JsonGenerationCatalog(path)
    catalog.promote(candidate("gen_01"), expected_active_id=None)
    ready = Barrier(2, timeout=5)

    def promote(generation_id: str) -> IndexGeneration | ErrorCode:
        writer = JsonGenerationCatalog(path)
        ready.wait()
        try:
            return writer.promote(candidate(generation_id), expected_active_id="gen_01")
        except RaglabError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(promote, ("gen_02", "gen_03")))

    assert results.count(ErrorCode.GENERATION_CONFLICT) == 1
    assert catalog.read_active() in results


def test_recover_removes_only_the_known_temp_catalog(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    active = catalog.promote(candidate("gen_01"), expected_active_id=None)
    catalog.temp_path.write_text("partial", encoding="utf-8")
    other_temp = tmp_path / "other.tmp"
    other_temp.write_text("opaque", encoding="utf-8")

    catalog.recover()
    catalog.recover()

    assert not catalog.temp_path.exists()
    assert catalog.read_active() == active
    assert other_temp.read_text(encoding="utf-8") == "opaque"


def test_recover_does_not_activate_an_orphaned_temp_catalog(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    catalog.temp_path.write_text("partial", encoding="utf-8")

    catalog.recover()

    assert catalog.read_active() is None
    assert not catalog.temp_path.exists()


def test_interrupted_replace_leaves_the_previous_generation_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    catalog = JsonGenerationCatalog(path)
    original = catalog.promote(candidate("gen_01"), expected_active_id=None)

    def interrupted_replace(source: Path, destination: Path) -> None:
        assert source == catalog.temp_path
        assert destination == path
        assert json.loads(source.read_text(encoding="utf-8"))["generation_id"] == "gen_02"
        raise OSError("interrupted replace")

    monkeypatch.setattr("raglab.indexing.generation.os.replace", interrupted_replace)

    with pytest.raises(OSError, match="interrupted replace"):
        catalog.promote(candidate("gen_02"), expected_active_id="gen_01")

    assert catalog.read_active() == original
    catalog.recover()
    assert not catalog.temp_path.exists()


@pytest.mark.parametrize(
    "indexes", [(0, 1), (0, 0, 2), (0, 1, 2, 2)],
    ids=["missing", "duplicate", "extra"],
)
def test_generation_requires_one_manifest_of_each_kind(indexes: tuple[int, ...]) -> None:
    generation = candidate("gen_01")

    with pytest.raises(ValueError, match="exactly one"):
        replace(generation, manifests=tuple(generation.manifests[index] for index in indexes))


@pytest.mark.parametrize("checksum", ["A" * 64, "g" * 64, "a" * 63, "a" * 64 + "\n"])
def test_generation_and_manifest_require_lowercase_hex_sha256(checksum: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        IndexManifest("text", checksum, 0)
    with pytest.raises(ValueError, match="SHA-256"):
        replace(candidate("gen_01"), source_hash=checksum)


@pytest.mark.parametrize("row_count", [-1, 1.5, True])
def test_manifest_requires_a_non_negative_integer_row_count(row_count: object) -> None:
    with pytest.raises(ValueError, match="row_count"):
        IndexManifest("text", "a" * 64, row_count)


def test_manifests_and_generation_are_immutable() -> None:
    generation = candidate("gen_01")
    manifests = list(generation.manifests)
    copied_generation = replace(generation, manifests=manifests)

    manifests.clear()

    assert copied_generation.manifests == generation.manifests
    with pytest.raises(FrozenInstanceError):
        copied_generation.generation_id = "gen_02"
    with pytest.raises(FrozenInstanceError):
        copied_generation.manifests[0].row_count = 4


def test_only_validated_generation_may_be_promoted(tmp_path: Path) -> None:
    catalog = JsonGenerationCatalog(tmp_path / "catalog.json")
    active = replace(candidate("gen_01"), state=GenerationState.ACTIVE)

    with pytest.raises(ValueError, match="VALIDATED"):
        catalog.promote(active, expected_active_id=None)

    assert catalog.read_active() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation_id", ""),
        ("document_version", []),
        ("schema_version", {}),
        ("acl_projection_version", True),
        ("state", "ACTIVE"),
    ],
)
def test_generation_rejects_invalid_metadata(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        replace(candidate("gen_01"), **{field: value})


def test_a_corrupt_active_id_cannot_be_used_as_an_empty_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    catalog = JsonGenerationCatalog(path)
    catalog.promote(candidate("gen_01"), expected_active_id=None)
    catalog_data = json.loads(path.read_text(encoding="utf-8"))
    catalog_data["generation_id"] = None
    corrupt_data = json.dumps(catalog_data)
    path.write_text(corrupt_data, encoding="utf-8")

    with pytest.raises(ValueError, match="generation_id"):
        catalog.promote(candidate("gen_02"), expected_active_id=None)

    assert path.read_text(encoding="utf-8") == corrupt_data
