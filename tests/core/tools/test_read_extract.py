"""Tests for document text extraction used by the read and web_fetch tools."""

from __future__ import annotations

import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from core.tools.read_extract import (
    ExtractionError,
    detect_extractable_document,
    document_label,
    extract_document_text,
)

_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_DOCX_DOCUMENT_XML = (
    '<?xml version="1.0"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
    "<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t> World</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Line</w:t><w:tab/><w:t>Two</w:t></w:r></w:p>"
    "</w:body></w:document>"
)

_SHARED_STRINGS_XML = (
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<si><t>Name</t></si><si><t>Age</t></si></sst>"
)
_SHEET_XML = (
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<sheetData>"
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
    '<row r="2"><c r="A2" t="str"><v>Bob</v></c><c r="B2"><v>42</v></c></row>'
    "</sheetData></worksheet>"
)
_WORKBOOK_XML = (
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="People" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS_XML = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
)


def _docx_bytes() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", _DOCX_DOCUMENT_XML)
    return buffer.getvalue()


def _xlsx_bytes(*, with_workbook: bool = True) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", _SHARED_STRINGS_XML)
        archive.writestr("xl/worksheets/sheet1.xml", _SHEET_XML)
        if with_workbook:
            archive.writestr("xl/workbook.xml", _WORKBOOK_XML)
            archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
    return buffer.getvalue()


def _ipynb_bytes() -> bytes:
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Title\n", "intro"]},
            {"cell_type": "code", "source": "print('hi')"},
        ]
    }
    return json.dumps(notebook).encode("utf-8")


def _pdf_bytes(lines: list[str]) -> bytes:
    """Build a minimal single-page PDF whose content stream draws ``lines``.

    Cross-reference offsets are computed from the real byte layout so pypdf reads
    the file directly; an empty ``lines`` yields a page with no text-showing
    operator, standing in for a scanned PDF that has no text layer.
    """
    operators = b"BT /F1 24 Tf 72 720 Td "
    for line in lines:
        operators += b"(" + line.encode("latin-1") + b") Tj 0 -28 Td "
    operators += b"ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(operators), operators),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % index + body + b"\nendobj\n"

    xref_position = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    pdf += b"startxref\n%d\n%%%%EOF" % xref_position
    return bytes(pdf)


def test_detect_extractable_document_recognizes_kinds_by_extension() -> None:
    assert detect_extractable_document("notes.ipynb", "text/plain") == "ipynb"
    assert detect_extractable_document("report.DOCX", "application/octet-stream") == "docx"
    assert detect_extractable_document("sheet.xlsx", "application/octet-stream") == "xlsx"
    assert detect_extractable_document("paper.pdf", "application/octet-stream") == "pdf"
    assert detect_extractable_document("plain.txt", "text/plain") is None
    assert detect_extractable_document("archive.zip", "application/zip") is None


def test_detect_extractable_document_recognizes_kinds_by_media_type() -> None:
    # A fetched URL often has no usable extension, so the sniffed type must win.
    assert detect_extractable_document("download", "application/pdf") == "pdf"
    assert detect_extractable_document("download", _DOCX_MEDIA_TYPE) == "docx"
    assert detect_extractable_document("download", _XLSX_MEDIA_TYPE) == "xlsx"


def test_document_label_names_each_kind() -> None:
    assert document_label("ipynb") == "Jupyter notebook"
    assert document_label("docx") == "Word document"
    assert document_label("xlsx") == "Excel spreadsheet"
    assert document_label("pdf") == "PDF document"


def test_extract_ipynb_renders_cells_with_headers() -> None:
    text = extract_document_text(_ipynb_bytes(), "ipynb")

    assert text == "# Cell 1 [markdown]\n# Title\nintro\n\n# Cell 2 [code]\nprint('hi')"


def test_extract_docx_joins_paragraphs_with_tabs_preserved() -> None:
    text = extract_document_text(_docx_bytes(), "docx")

    assert text == "Hello World\nLine\tTwo"


def test_extract_xlsx_renders_tab_separated_rows_with_sheet_name() -> None:
    text = extract_document_text(_xlsx_bytes(), "xlsx")

    assert text == "# Sheet: People\nName\tAge\nBob\t42"


def test_extract_xlsx_falls_back_to_worksheet_files_without_workbook() -> None:
    text = extract_document_text(_xlsx_bytes(with_workbook=False), "xlsx")

    assert text == "# Sheet: sheet1\nName\tAge\nBob\t42"


def test_extract_pdf_renders_pages_with_headers() -> None:
    text = extract_document_text(_pdf_bytes(["Hello PDF", "Second line"]), "pdf")

    assert text.startswith("# Page 1\n")
    assert "Hello PDF" in text
    assert "Second line" in text


def test_extract_pdf_without_text_layer_returns_empty() -> None:
    # A scanned PDF has no text layer; extraction yields empty, and the read /
    # web_fetch caller turns that into an explicit "no extractable text" note.
    assert extract_document_text(_pdf_bytes([]), "pdf") == ""


def test_extract_malformed_pdf_raises_extraction_error() -> None:
    with pytest.raises(ExtractionError):
        extract_document_text(b"not a pdf at all", "pdf")


def test_extract_malformed_docx_raises_extraction_error() -> None:
    with pytest.raises(ExtractionError):
        extract_document_text(b"not a zip archive at all", "docx")


def test_extract_malformed_ipynb_raises_extraction_error() -> None:
    with pytest.raises(ExtractionError):
        extract_document_text(b"{not valid json", "ipynb")


def test_extract_unknown_kind_raises_extraction_error() -> None:
    with pytest.raises(ExtractionError):
        extract_document_text(b"anything", "png")
