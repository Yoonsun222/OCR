from pathlib import Path
from zipfile import ZipFile

import pytest

from raglab.offline.windows_paths import WindowsLocalPaths
from raglab.track_b.errors import (
    ForbiddenPart,
    InvalidPackage,
    PackageLimitExceeded,
    UnsafeRelationship,
)
from raglab.track_b.opc import OpcLimits, OpcPackage, Relationship
from tests.fixtures.xlsx_factory import write_xlsx


REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _relationships(target: str, mode: str = "") -> bytes:
    return (
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="urn:test" Target="{target}" {mode}/>'
        "</Relationships>"
    ).encode()


def test_reads_xml_and_resolves_parent_relative_relationship(tmp_path: Path) -> None:
    path = write_xlsx(
        tmp_path / "sample.xlsx",
        {
            "xl/drawings/drawing1.xml": b"<drawing/>",
            "xl/drawings/_rels/drawing1.xml.rels": _relationships("../charts/chart1.xml"),
            "xl/charts/chart1.xml": b"<chart/>",
        },
    )

    package = OpcPackage.open(path)

    assert package.path == path
    assert {"xl/workbook.xml", "xl/charts/chart1.xml"} <= package.names
    assert package.read_xml("xl/workbook.xml").tag.endswith("}workbook")
    assert package.relationships("xl/workbook.xml") == ()
    assert package.relationships("xl/drawings/drawing1.xml") == (
        Relationship("rId1", "urn:test", "xl/charts/chart1.xml"),
    )


@pytest.mark.parametrize(
    "part",
    ("../private.xml", "/private.xml", "C:/private.xml", "xl/./private.xml"),
)
def test_rejects_unsafe_zip_part_names(tmp_path: Path, part: str) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx", {part: b"x"})

    with pytest.raises(InvalidPackage, match="unsafe part name") as error:
        OpcPackage.open(path)

    assert "private" not in str(error.value)
    assert error.value.code == "TRACK_B_INVALID_PACKAGE"


@pytest.mark.parametrize(
    "part",
    (
        "xl/vbaProject.bin",
        "xl/embeddings/object1.bin",
        "xl/externalLinks/externalLink1.xml",
        "xl/connections.xml",
        "xl/queryTables/internal.xml",
        "xl/macrosheets/sheet1.xml",
        "xl/activeX/control1.xml",
    ),
)
def test_rejects_active_parts(tmp_path: Path, part: str) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx", {part: b"x"})

    with pytest.raises(ForbiddenPart, match="forbidden workbook part") as error:
        OpcPackage.open(path)

    assert part not in str(error.value)
    assert error.value.code == "TRACK_B_FORBIDDEN_PART"


def test_rejects_macro_enabled_content_type(tmp_path: Path) -> None:
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/></Types>'
    )
    path = write_xlsx(tmp_path / "sample.xlsx", {"[Content_Types].xml": content_types})

    with pytest.raises(ForbiddenPart):
        OpcPackage.open(path)


@pytest.mark.parametrize(
    ("target", "mode"),
    (
        ("https://example.invalid/private", 'TargetMode="External"'),
        ("https://example.invalid/private", ""),
        ("../../private.xml", ""),
        ("/private.xml", ""),
        ("//xl/workbook.xml", ""),
        ("/xl/../xl/workbook.xml", ""),
        ("%2e%2e/private.xml", ""),
        ("missing-private.xml", ""),
        ("workbook.xml", 'TargetMode="unexpected"'),
    ),
)
def test_rejects_unsafe_relationships(tmp_path: Path, target: str, mode: str) -> None:
    path = write_xlsx(
        tmp_path / "sample.xlsx",
        {"xl/_rels/workbook.xml.rels": _relationships(target, mode)},
    )
    package = OpcPackage.open(path)

    with pytest.raises(UnsafeRelationship) as error:
        package.relationships("xl/workbook.xml")

    assert target not in str(error.value)
    assert error.value.code == "TRACK_B_UNSAFE_RELATIONSHIP"


@pytest.mark.parametrize(
    ("limits", "message"),
    (
        (OpcLimits(max_entries=2), "entry count"),
        (OpcLimits(max_entry_bytes=127), "entry exceeds"),
        (OpcLimits(max_entry_bytes=256, max_total_bytes=64), "total uncompressed"),
        (OpcLimits(max_ratio=2), "compression ratio"),
    ),
)
def test_rejects_zip_limits(tmp_path: Path, limits: OpcLimits, message: str) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx", {"xl/media/blob.bin": b"a" * 128})

    with pytest.raises(PackageLimitExceeded, match=message) as error:
        OpcPackage.open(path, limits)

    assert "blob.bin" not in str(error.value)
    assert error.value.code == "TRACK_B_PACKAGE_LIMIT"


def test_rejects_backslash_zip_part_name(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx", {"xl/private.xml": b"x"})
    path.write_bytes(path.read_bytes().replace(b"xl/private.xml", b"xl\\private.xml"))

    with pytest.raises(InvalidPackage, match="unsafe part name"):
        OpcPackage.open(path)


def test_rejects_case_colliding_zip_entries(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx", {"XL/WORKBOOK.XML": b"x"})

    with pytest.raises(InvalidPackage, match="duplicate part"):
        OpcPackage.open(path)


@pytest.mark.parametrize(
    "payload",
    (
        b"<private",
        b'<!DOCTYPE d [<!ENTITY x "private">]><d>&x;</d>',
        '<!DOCTYPE d SYSTEM "file:///private"><d/>'.encode("utf-16"),
    ),
)
def test_rejects_invalid_xml_and_doctype(tmp_path: Path, payload: bytes) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx", {"xl/workbook.xml": payload})
    package = OpcPackage.open(path)

    with pytest.raises(InvalidPackage, match="invalid XML") as error:
        package.read_xml("xl/workbook.xml")

    assert "private" not in str(error.value)


def test_rejects_malformed_zip_without_disclosing_path(tmp_path: Path) -> None:
    path = tmp_path / "private.xlsx"
    path.write_bytes(b"invalid zip")

    with pytest.raises(InvalidPackage) as error:
        OpcPackage.open(path)

    assert "private" not in str(error.value)


def test_holds_original_file_until_package_closes(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx")
    replacement = write_xlsx(tmp_path / "replacement.xlsx", {"xl/workbook.xml": b"<changed/>"})
    with OpcPackage.open(path) as package:
        with pytest.raises(PermissionError):
            replacement.replace(path)
        assert package.read_xml("xl/workbook.xml").tag.endswith("}workbook")

    replacement.replace(path)
    with pytest.raises(InvalidPackage, match="package is closed"):
        package.read_xml("xl/workbook.xml")
    with pytest.raises(InvalidPackage, match="package is closed"):
        package.relationships("xl/workbook.xml")


def test_rejects_unsupported_zip_compression(tmp_path: Path) -> None:
    from zipfile import ZIP_LZMA

    path = write_xlsx(tmp_path / "sample.xlsx")
    with ZipFile(path, "a", ZIP_LZMA) as archive:
        archive.writestr("xl/worksheet.xml", b"<worksheet/>")

    with pytest.raises(InvalidPackage, match="unsupported ZIP compression"):
        OpcPackage.open(path)

def test_rejects_untruthful_central_directory_count(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx")
    archive = bytearray(path.read_bytes())
    end_record = archive.rindex(b"PK\x05\x06")
    archive[end_record + 8:end_record + 12] = b"\x01\x00\x01\x00"
    path.write_bytes(archive)

    with pytest.raises(InvalidPackage, match="central directory count"):
        OpcPackage.open(path)


def test_rejects_oversize_compressed_entry(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx")
    archive = bytearray(path.read_bytes())
    header = archive.index(b"PK\x01\x02")
    archive[header + 20:header + 24] = (1024 * 1024).to_bytes(4, "little")
    path.write_bytes(archive)

    with pytest.raises(PackageLimitExceeded, match="compressed entry size"):
        OpcPackage.open(path, OpcLimits(max_entry_bytes=512))


def test_rejects_network_paths_before_opening(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_open(*args: object, **kwargs: object) -> None:
        pytest.fail("network path must be rejected before file IO")

    monkeypatch.setattr(Path, "open", unexpected_open)
    with pytest.raises(InvalidPackage, match="local file"):
        OpcPackage.open(Path("//example.invalid/share/sample.xlsx"))


@pytest.mark.parametrize("target", (r"\Device\Mup\share", r"\??\C:\private"))
def test_rejects_redirected_drives_before_any_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx")
    monkeypatch.setattr(WindowsLocalPaths, "volume_target", lambda self, drive: target)

    def unexpected_pin(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid volume must be rejected before pinning")

    monkeypatch.setattr(WindowsLocalPaths, "pin", unexpected_pin)
    with pytest.raises(InvalidPackage, match="local file") as error:
        OpcPackage.open(path)
    assert "private" not in str(error.value)


def test_does_not_open_descendant_of_rejected_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "redirected"
    parent.mkdir()
    path = write_xlsx(parent / "sample.xlsx")
    pin_entry = WindowsLocalPaths.pin_entry

    def reject_redirected(self: WindowsLocalPaths, entry: Path, **options: bool):
        if entry == parent:
            raise ValueError("synthetic private reparse point")
        if entry == path:
            pytest.fail("descendant must not be opened")
        return pin_entry(self, entry, **options)

    monkeypatch.setattr(WindowsLocalPaths, "pin_entry", reject_redirected)
    with pytest.raises(InvalidPackage, match="local file") as error:
        OpcPackage.open(path)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    ("payload", "options", "message"),
    (
        (b"<a><b><c/></b></a>", {"max_xml_depth": 2}, "depth"),
        (b"<a><b/><c/></a>", {"max_xml_elements": 2}, "element count"),
        (b'<a private="1" second="2"/>', {"max_xml_attributes": 1}, "attribute count"),
        (b'<a xmlns:b="one" xmlns:c="two"/>', {"max_xml_attributes": 1}, "attribute count"),
        (b'<a private="' + b"x" * 60 + b'"/>', {"max_xml_attribute_chars": 60}, "attribute text"),
        (b"<a>private<b/>private</a>", {"max_xml_text_chars": 10}, "text"),
    ),
)
def test_rejects_xml_structure_over_limit(
    tmp_path: Path, payload: bytes, options: dict[str, int], message: str,
) -> None:
    path = write_xlsx(tmp_path / "sample.xlsx", {"xl/workbook.xml": payload})
    with OpcPackage.open(path, OpcLimits(**options)) as package:
        with pytest.raises(PackageLimitExceeded, match=message) as error:
            package.read_xml("xl/workbook.xml")
    assert "private" not in str(error.value)


def test_resolves_package_absolute_workbook_relationship(tmp_path: Path) -> None:
    path = write_xlsx(
        tmp_path / "sample.xlsx",
        {
            "xl/_rels/workbook.xml.rels": _relationships("/xl/worksheets/sheet1.xml"),
            "xl/worksheets/sheet1.xml": b"<worksheet/>",
        },
    )
    with OpcPackage.open(path) as package:
        assert package.relationships("xl/workbook.xml") == (
            Relationship("rId1", "urn:test", "xl/worksheets/sheet1.xml"),
        )
