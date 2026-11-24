from types import MappingProxyType

from raglab.offline.jsonio import write_json_atomic


def test_atomic_json_is_canonical_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "report.json"
    write_json_atomic(target, {"z": 1, "a": "한글"})
    assert target.read_bytes() == '{\n  "a": "한글",\n  "z": 1\n}\n'.encode("utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


def test_read_only_mapping_is_supported(tmp_path):
    target = tmp_path / "report.json"
    write_json_atomic(target, MappingProxyType({"a": 1}))
    assert target.read_bytes() == b'{\n  "a": 1\n}\n'


def test_non_finite_number_preserves_existing_report_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "report.json"
    target.write_bytes(b'{"previous": true}\n')

    try:
        write_json_atomic(target, {"a": float("nan")})
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite values must not be written as JSON")

    assert target.read_bytes() == b'{"previous": true}\n'
    assert list(tmp_path.glob("*.tmp")) == []
