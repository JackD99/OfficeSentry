"""A small Word (.docx) writer for text documents: headings, paragraphs, bullet lists and a label-value table.

A .docx is a zip of XML parts. This writes the few parts Word, LibreOffice and Google Docs need to open a
plain document with built-in heading styles, so an engineer can paste from it or send it on. No images, no
dependency; ``reports/export`` keeps PDFs, ``reports/workbook`` spreadsheets.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

from .cells import printable

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '<Relationship Id="rId2" Target="docProps/core.xml" '
    'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"/>'
    "</Relationships>"
)
DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    "</Relationships>"
)


def _style(style_id: str, name: str, size_half_points: int, bold: bool = False, color: str = "") -> str:
    run = (
        f'<w:sz w:val="{size_half_points}"/>'
        + ("<w:b/>" if bold else "")
        + (f'<w:color w:val="{color}"/>' if color else "")
    )
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/><w:basedOn w:val="Normal"/>'
        f'<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/></w:pPr><w:rPr>{run}</w:rPr></w:style>'
    )


STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{W}">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" '
    'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    + _style("Title", "Title", 44, True)
    + _style("Heading1", "heading 1", 32, True)
    + _style("Heading2", "heading 2", 26, True)
    + _style("Heading3", "heading 3", 23, True)
    + '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:ind w:left="360" w:hanging="360"/><w:spacing w:after="60"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Muted"><w:name w:val="Muted"/><w:basedOn w:val="Normal"/>'
    '<w:rPr><w:color w:val="666666"/><w:sz w:val="19"/></w:rPr></w:style>'
    "</w:styles>"
)


def _run(text: str, bold: bool = False) -> str:
    props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    out = []
    for i, line in enumerate(str(text).split("\n")):
        if i:
            out.append("<w:br/>")
        out.append(f'<w:t xml:space="preserve">{escape(printable(line))}</w:t>')
    return f"<w:r>{props}{''.join(out)}</w:r>"


class Document:
    """Build a document top to bottom, then ``save()`` it as bytes."""

    def __init__(self, title: str = "", author: str = ""):
        self.title, self.author = title, author
        self._body: list[str] = []

    def _para(self, runs: str, style: str = "") -> None:
        props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        self._body.append(f"<w:p>{props}{runs}</w:p>")

    def heading(self, text: str, level: int = 1) -> None:
        self._para(_run(text), {0: "Title", 1: "Heading1", 2: "Heading2"}.get(level, "Heading3"))

    def paragraph(self, text: str, lead: str = "", muted: bool = False) -> None:
        """``lead`` is a bold label before the text ("Answer: ")."""
        runs = (_run(lead, bold=True) if lead else "") + _run(text)
        self._para(runs, "Muted" if muted else "")

    def bullets(self, items) -> None:
        for item in items:
            self._para(_run("• ") + _run(str(item)), "ListBullet")

    def table(self, rows: list[tuple[str, str]]) -> None:
        """Two columns: a label and its value, with light borders."""
        borders = "".join(
            f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BBBBBB"/>'
            for side in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        cells = []
        for label, value in rows:
            cells.append(
                "<w:tr>"
                f'<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr><w:p>{_run(label, bold=True)}</w:p></w:tc>'
                f'<w:tc><w:tcPr><w:tcW w:w="6000" w:type="dxa"/></w:tcPr><w:p>{_run(value)}</w:p></w:tc>'
                "</w:tr>"
            )
        self._body.append(
            f'<w:tbl><w:tblPr><w:tblW w:w="9000" w:type="dxa"/><w:tblBorders>{borders}</w:tblBorders>'
            f'<w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar>'
            f"</w:tblPr>{''.join(cells)}</w:tbl><w:p/>"
        )

    def save(self) -> bytes:
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W}"><w:body>{"".join(self._body)}'
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" '
            'w:left="1134" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>'
        )
        core = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(printable(self.title))}</dc:title>"
            f"<dc:creator>{escape(printable(self.author))}</dc:creator>"
            "</cp:coreProperties>"
        )
        out = BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", CONTENT_TYPES)
            z.writestr("_rels/.rels", RELS)
            z.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
            z.writestr("word/document.xml", document)
            z.writestr("word/styles.xml", STYLES)
            z.writestr("docProps/core.xml", core)
        return out.getvalue()


MIMETYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
