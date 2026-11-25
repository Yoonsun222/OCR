import pytest

from raglab.offline.config import (
    LOCKED_SECURITY_FIELDS,
    ConfigLayer,
    load_effective_config,
    load_profile_config,
    validate_config,
)


@pytest.mark.parametrize("profile,working_set,concurrency,rerank,visual", [
    ("local16", 12, 1, 20, 3),
    ("local32", 26, 2, 30, 5),
])
def test_shipped_profiles_are_valid_in_any_working_directory(
    tmp_path, monkeypatch, profile, working_set, concurrency, rerank, visual,
):
    monkeypatch.chdir(tmp_path)
    result = load_profile_config(profile)
    assert result.status == "PASS"
    assert result.issues == ()
    assert result.values == {
        "network_mode": "deny",
        "diagnostic_content": False,
        "profile": profile,
        "max_working_set_gib": working_set,
        "interactive_concurrency": concurrency,
        "rerank_candidates": rerank,
        "visual_candidates": visual,
        "temp_root": "tmp",
        "cache_root": "cache",
        "output_root": "runs",
    }
    assert result.provenance["max_working_set_gib"] == profile
    assert result.locked == LOCKED_SECURITY_FIELDS | {"profile"}


@pytest.mark.parametrize("name,forbidden,baseline", [
    ("network_mode", "allow", "deny"),
    ("diagnostic_content", True, False),
])
def test_run_layer_cannot_weaken_security_without_an_explicit_declaration(name, forbidden, baseline):
    result = load_effective_config([ConfigLayer("run", {name: forbidden})])
    assert result.status == "BLOCK"
    assert result.values[name] == baseline
    assert result.issues[0].code == "RAGLAB-CONFIG-003"
    assert result.issues[0].field == name


def test_locked_restatement_does_not_replace_authority_and_overrides_track_source():
    result = load_effective_config([
        ConfigLayer("security", {"network_mode": "deny", "rerank_candidates": 20}),
        ConfigLayer("profile", {"rerank_candidates": 18}),
        ConfigLayer("run", {"network_mode": "deny", "rerank_candidates": 15}),
    ])
    assert result.status == "PASS"
    assert result.provenance["network_mode"] == "security"
    assert result.provenance["rerank_candidates"] == "run"
    assert result.values["rerank_candidates"] == 15


def test_any_declared_lock_persists_through_later_layers():
    config = load_effective_config([
        ConfigLayer("site", {"max_working_set_gib": 10}, frozenset({"max_working_set_gib"})),
        ConfigLayer("profile", {"max_working_set_gib": 12}),
        ConfigLayer("run", {"max_working_set_gib": 8}),
    ])
    assert config.status == "BLOCK"
    assert config.values["max_working_set_gib"] == 10
    assert config.provenance["max_working_set_gib"] == "site"
    assert [issue.code for issue in config.issues] == ["RAGLAB-CONFIG-003"] * 2


@pytest.mark.parametrize("locked", [{"missing"}, {"temp_root"}])
def test_layer_can_lock_only_valid_supplied_keys(locked):
    result = load_effective_config([ConfigLayer("run", {}, frozenset(locked))])
    assert result.status == "BLOCK"
    assert result.issues[0].code == "RAGLAB-CONFIG-001"


def test_effective_security_values_cannot_be_mutated():
    source = {"network_mode": "deny", "rerank_candidates": 8}
    result = load_effective_config([ConfigLayer("site", source)])
    source["network_mode"] = "allow"
    assert result.values["network_mode"] == "deny"
    with pytest.raises(TypeError):
        result.values["network_mode"] = "allow"
    with pytest.raises(TypeError):
        result.provenance["network_mode"] = "run"


@pytest.mark.parametrize("value,field,code", [
    ({"private-key-name": "secret"}, None, "001"),
    ({"interactive_concurrency": True}, "interactive_concurrency", "002"),
    ({"diagnostic_content": 0}, "diagnostic_content", "002"),
    ({"rerank_candidates": 2.0}, "rerank_candidates", "002"),
    ({"visual_candidates": -1}, "visual_candidates", "002"),
    ({"rerank_candidates": 31}, "rerank_candidates", "002"),
    ({"max_working_set_gib": 0}, "max_working_set_gib", "002"),
    ({"max_working_set_gib": 27}, "max_working_set_gib", "002"),
    ({"interactive_concurrency": 3}, "interactive_concurrency", "002"),
    ({"profile": "unknown"}, "profile", "002"),
    ({"network_mode": "allow"}, "network_mode", "003"),
    ({"diagnostic_content": True}, "diagnostic_content", "003"),
])
def test_exact_field_schema(value, field, code):
    (issue,) = validate_config(value)
    assert issue.code == f"RAGLAB-CONFIG-{code}"
    assert issue.field == field
    assert "private-key-name" not in repr(issue)
    assert "secret" not in repr(issue)


@pytest.mark.parametrize("path", [
    r"..\cache", "temp/../cache", r"C:\Users\Synthetic\..\cache", r"C:cache",
    r"\\network.invalid\share\cache", r"\\?\C:\work\cache", "/rooted",
    "tmp/file:stream", r"tmp\LPT1.txt", "tmp/unsafe.", "tmp//bad", "tmp/\u0000",
    "tmp/\ud800", "",
])
def test_windows_unsafe_config_paths_block_without_echo(path):
    (issue,) = validate_config({"temp_root": path})
    assert issue.field == "temp_root"
    assert issue.code == "RAGLAB-CONFIG-002"
    if path:
        assert path not in issue.reason


@pytest.mark.parametrize("path", [r"C:\Users\Synthetic\cache", "work/한글", r"work\cache", "./runs"])
def test_safe_config_paths_are_validated_without_io(path):
    assert validate_config({"output_root": path}) == ()


@pytest.mark.parametrize("profile,limits", [
    ("local16", {
        "max_working_set_gib": 12, "interactive_concurrency": 1,
        "rerank_candidates": 20, "visual_candidates": 3,
    }),
    ("local32", {
        "max_working_set_gib": 26, "interactive_concurrency": 2,
        "rerank_candidates": 30, "visual_candidates": 5,
    }),
])
def test_validate_profile_enforces_every_resource_ceiling(profile, limits):
    assert validate_config({"profile": profile, **limits}) == ()
    excessive = {key: value + 1 for key, value in limits.items()}
    issues = validate_config({"profile": profile, **excessive})
    assert {issue.field for issue in issues} == limits.keys()
    assert len(issues) == len(limits)


def test_local16_run_cannot_exceed_any_resource_ceiling(tmp_path):
    run = tmp_path / "experiment.json"
    run.write_text(
        '{"max_working_set_gib":13,"interactive_concurrency":2,"rerank_candidates":21,"visual_candidates":4}',
        encoding="utf-8",
    )
    result = load_profile_config("local16", run)
    assert result.status == "BLOCK"
    assert {issue.field for issue in result.issues} == {
        "max_working_set_gib", "interactive_concurrency", "rerank_candidates", "visual_candidates",
    }


def test_run_cannot_switch_selected_profile(tmp_path):
    run = tmp_path / "experiment.json"
    run.write_text('{"profile":"local32"}', encoding="utf-8")
    result = load_profile_config("local16", run)
    assert result.status == "BLOCK"
    assert result.values["profile"] == "local16"
    assert result.issues[0].code == "RAGLAB-CONFIG-003"
