from pathlib import Path

import pytest

from raglab.track_a.svg_regions import read_svg_regions
from raglab.track_a.tesseract_tsv import parse_tsv

FIXTURES = Path(__file__).parent / "fixtures"
SVG_RECT = '<rect data-region-id="target" x="10" y="20" width="10" height="20"/>'
SVG_TEMPLATE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{}</svg>'


def test_synthetic_inputs_normalize_coordinates() -> None:
    tokens = parse_tsv((FIXTURES / "mixed_labels.tsv").read_text(encoding="utf-8"), 1000, 500)
    regions = read_svg_regions(str(FIXTURES / "parts_ko_en_ja.svg"))
    assert [item.text for item in tokens] == ["A-12", "부품B", "部品C"]
    assert tokens[0].box == (0.1, 0.2, 0.08, 0.04)
    assert [item.region_id for item in regions] == ["part-a", "part-b", "part-c"]
    assert regions[0].polygon[0] == (0.2, 0.2)


@pytest.mark.parametrize("value", ["synthetic-malformed", "nan", "99999"])
def test_tsv_rejects_invalid_word_coordinates_without_echoing_input(value) -> None:
    text = (FIXTURES / "mixed_labels.tsv").read_text(encoding="utf-8")
    text = text.replace("\t100\t100\t80\t20\t", f"\t{value}\t100\t80\t20\t", 1)
    with pytest.raises(ValueError) as error:
        parse_tsv(text, 1000, 500)
    assert value not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("svg", [
    '<!DOCTYPE svg [<!ENTITY text "synthetic">]>' + SVG_TEMPLATE.format(SVG_RECT),
    SVG_TEMPLATE.format('<g transform="translate(10 10)">' + SVG_RECT + '</g>'),
    SVG_TEMPLATE.format(SVG_RECT.replace('x="10"', 'x="synthetic-malformed"')),
    SVG_TEMPLATE.format('<image href="https://example.invalid/test.png"/>'),
    SVG_TEMPLATE.format(SVG_RECT * 2),
    SVG_TEMPLATE.format(" " * (1024 * 1024)),
], ids=["dtd", "transform", "coordinate", "reference", "duplicate", "size"])
def test_svg_rejects_unsupported_input_without_echoing_payload(tmp_path, svg) -> None:
    path = tmp_path / "input.svg"
    path.write_text(svg, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        read_svg_regions(str(path))
    assert "synthetic-malformed" not in str(error.value)
    assert "example.invalid" not in str(error.value)


@pytest.mark.parametrize("path", [r"\\example.invalid\share\input.svg", "https://example.invalid/input.svg"])
def test_svg_rejects_remote_path(path) -> None:
    with pytest.raises(ValueError, match="local SVG"):
        read_svg_regions(path)


def test_svg_rejects_symbolic_link(tmp_path) -> None:
    source = tmp_path / "source.svg"
    source.write_text(SVG_TEMPLATE.format(SVG_RECT), encoding="utf-8")
    link = tmp_path / "link.svg"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symbolic links unavailable")
    with pytest.raises(ValueError, match="local SVG"):
        read_svg_regions(str(link))
