"""Build small synthetic workbook packages without launching Office."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

CONTENT_TYPES = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
WORKBOOK = b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'


def write_xlsx(path: Path, extra_parts: dict[str, bytes] | None = None) -> Path:
    parts = {"[Content_Types].xml": CONTENT_TYPES, "xl/workbook.xml": WORKBOOK}
    parts.update(extra_parts or {})
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return path
