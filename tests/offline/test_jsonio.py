from raglab.offline.jsonio import write_json_atomic


def test_atomic_json_is_canonical_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "report.json"
    write_json_atomic(target, {"z": 1, "a": "한글"})
    assert target.read_text(encoding="utf-8") == '{\n  "a": "한글",\n  "z": 1\n}\n'
    assert list(tmp_path.glob("*.tmp")) == []
