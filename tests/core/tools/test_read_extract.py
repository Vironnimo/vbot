"""Tests for Office/notebook text extraction used by the read tool."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from core.tools.read_extract import (
    ExtractionError,
    document_label,
    extract_document_text,
    is_extractable_document,
)

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


def _write_docx(path: Path) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", _DOCX_DOCUMENT_XML)
    return path


def _write_xlsx(path: Path, *, with_workbook: bool = True) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", _SHARED_STRINGS_XML)
        archive.writestr("xl/worksheets/sheet1.xml", _SHEET_XML)
        if with_workbook:
            archive.writestr("xl/workbook.xml", _WORKBOOK_XML)
            archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
    return path


def _write_ipynb(path: Path) -> Path:
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Title\n", "intro"]},
            {"cell_type": "code", "source": "print('hi')"},
        ]
    }
    path.write_text(json.dumps(notebook), encoding="utf-8")
    return path


def test_is_extractable_document_matches_known_extensions() -> None:
    assert is_extractable_document("notes.ipynb") is True
    assert is_extractable_document("report.DOCX") is True
    assert is_extractable_document("sheet.xlsx") is True
    assert is_extractable_document("plain.txt") is False
    assert is_extractable_document("archive.zip") is False


def test_document_label_names_each_type() -> None:
    assert document_label("a.ipynb") == "Jupyter notebook"
    assert document_label("a.docx") == "Word document"
    assert document_label("a.xlsx") == "Excel spreadsheet"


def test_extract_ipynb_renders_cells_with_headers(tmp_path: Path) -> None:
    notebook = _write_ipynb(tmp_path / "nb.ipynb")

    text = extract_document_text(notebook)

    assert text == "# Cell 1 [markdown]\n# Title\nintro\n\n# Cell 2 [code]\nprint('hi')"


def test_extract_docx_joins_paragraphs_with_tabs_preserved(tmp_path: Path) -> None:
    document = _write_docx(tmp_path / "doc.docx")

    text = extract_document_text(document)

    assert text == "Hello World\nLine\tTwo"


def test_extract_xlsx_renders_tab_separated_rows_with_sheet_name(tmp_path: Path) -> None:
    spreadsheet = _write_xlsx(tmp_path / "book.xlsx")

    text = extract_document_text(spreadsheet)

    assert text == "# Sheet: People\nName\tAge\nBob\t42"


def test_extract_xlsx_falls_back_to_worksheet_files_without_workbook(tmp_path: Path) -> None:
    spreadsheet = _write_xlsx(tmp_path / "book.xlsx", with_workbook=False)

    text = extract_document_text(spreadsheet)

    assert text == "# Sheet: sheet1\nName\tAge\nBob\t42"


def test_extract_malformed_docx_raises_extraction_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip archive at all")

    with pytest.raises(ExtractionError):
        extract_document_text(broken)


def test_extract_malformed_ipynb_raises_extraction_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.ipynb"
    broken.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ExtractionError):
        extract_document_text(broken)
