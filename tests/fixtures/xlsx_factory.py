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


def write_native_workbook(path: Path, extra_parts: dict[str, bytes] | None = None) -> Path:
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <workbookPr date1904="true"/>
      <sheets><sheet name="Data" sheetId="1" r:id="rId1"/>
      <sheet name="Summary" sheetId="9" state="veryHidden" r:id="rId2"/></sheets></workbook>"""
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
       Target="worksheets/data.xml"/>
      <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
       Target="/xl/worksheets/report.xml"/>
      <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
       Target="strings.xml"/></Relationships>"""
    shared = """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <si>
        <r><t>매</t></r>
        <r><t>출</t></r>
        <rPh sb="0" eb="2"><t>phonetic</t></rPh>
      </si></sst>"""
    sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c></row>
        <row r="2">
          <c r="B2"><v>12.5</v></c><c r="C2"><f t="shared" si="0" ref="C2:C3">SUM(B2:B3)</f><v>20</v></c>
          <c r="D2"><f>B2*2</f></c>
        </row>
        <row r="3">
          <c r="B3"><v>7.5</v></c><c r="C3"><f t="shared" si="0"/><v>0</v></c>
          <c r="D3" t="str"><f>""</f><v/></c>
        </row>
      </sheetData></worksheet>"""
    report = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="inlineStr"><is>
          <r><t xml:space="preserve"> Total </t></r>
          <r><t xml:space="preserve">Sales </t></r>
        </is></c></row>
        <row r="2">
          <c r="A2" t="str"><f>TEXT(45,"0")</f><v>45</v></c>
          <c r="B2" t="b"><v>1</v></c><c r="C2" t="e"><v>#VALUE!</v></c>
          <c r="D2" s="2"><v>1E2</v></c>
        </row>
      </sheetData></worksheet>"""
    parts = {
        "xl/workbook.xml": workbook.encode(),
        "xl/_rels/workbook.xml.rels": relationships.encode(),
        "xl/strings.xml": shared.encode(),
        "xl/worksheets/data.xml": sheet.encode(),
        "xl/worksheets/report.xml": report.encode(),
    }
    parts.update(extra_parts or {})
    return write_xlsx(path, parts)

def write_layout_workbook(path: Path, extra_parts: dict[str, bytes] | None = None) -> Path:
    sheet = b"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <cols><col min="4" max="5" hidden="true"/></cols>
      <sheetData><row r="3"/><row hidden="1"/></sheetData>
      <autoFilter ref="A1:C9"/>
      <mergeCells><mergeCell ref="A1:B1"/></mergeCells>
      <tableParts count="1"><tablePart r:id="rIdTable"/></tableParts>
      </worksheet>"""
    relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdTable" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
       Target="../tables/table1.xml"/></Relationships>"""
    table = b"""<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      id="1" name="Sales" displayName="Sales_Display" ref="A1:C9" totalsRowCount="1"/>"""
    parts = {
        "xl/worksheets/data.xml": sheet,
        "xl/worksheets/_rels/data.xml.rels": relationships,
        "xl/tables/table1.xml": table,
    }
    parts.update(extra_parts or {})
    return write_native_workbook(path, parts)


def write_chart_workbook(path: Path, extra_parts: dict[str, bytes] | None = None) -> Path:
    workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>""".encode()
    workbook_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
       Target="worksheets/sheet1.xml"/></Relationships>""".encode()
    sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheetData><row r="2"><c r="B2"><v>1</v></c></row></sheetData><drawing r:id="rIdDraw"/></worksheet>""".encode()
    sheet_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdDraw" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
       Target="../drawings/drawing1.xml"/></Relationships>""".encode()
    drawing = """<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
      xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <xdr:twoCellAnchor><xdr:graphicFrame>
        <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
            <c:chart r:id="rIdChart"/>
          </a:graphicData>
        </a:graphic>
      </xdr:graphicFrame></xdr:twoCellAnchor></xdr:wsDr>""".encode()
    drawing_rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdChart" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
       Target="../charts/chart1.xml"/></Relationships>""".encode()
    chart = """<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart>
      <c:title><c:tx><c:rich><a:p><a:r><a:t>분기 매출</a:t></a:r></a:p></c:rich></c:tx></c:title>
      <c:plotArea><c:barChart>
      <c:ser><c:idx val="0"/><c:order val="1"/>
      <c:tx><c:v>제품 B</c:v></c:tx>
      <c:cat><c:numRef><c:f>'Data'!$A$2:$A$5</c:f></c:numRef></c:cat>
      <c:val><c:numRef><c:f>'Data'!$C$2:$C$5</c:f></c:numRef></c:val>
      </c:ser>
      <c:ser><c:idx val="1"/><c:order val="0"/>
      <c:tx><c:strRef><c:f>'Data'!$B$1</c:f><c:strCache><c:pt idx="0"><c:v>제품 A</c:v></c:pt></c:strCache></c:strRef></c:tx>
      <c:cat><c:strRef><c:f>'Data'!$A$2:$A$5</c:f></c:strRef></c:cat>
      <c:val><c:numRef><c:f>'Data'!$B$2:$B$5</c:f></c:numRef></c:val>
      </c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>""".encode("utf-8")
    parts = {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/worksheets/sheet1.xml": sheet,
        "xl/worksheets/_rels/sheet1.xml.rels": sheet_rels,
        "xl/drawings/drawing1.xml": drawing,
        "xl/drawings/_rels/drawing1.xml.rels": drawing_rels,
        "xl/charts/chart1.xml": chart,
    }
    parts.update(extra_parts or {})
    return write_xlsx(path, parts)
