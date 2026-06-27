"""Text extraction for Office and notebook documents read by the ``read`` tool.

Renders ``.ipynb``/``.docx``/``.xlsx`` as readable plain text using only the
standard library (``zipfile`` + ``xml.etree`` + ``json``) so the read tool can
show their content instead of a binary notice (docx/xlsx) or raw JSON (ipynb).
No third-party document libraries are pulled in.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

# The three formats this module renders as text. ``.ipynb`` is valid UTF-8 JSON
# and sniffs as text, so detection is by extension (not media type) to cover all
# three uniformly — see the read tool's routing.
EXTRACTABLE_EXTENSIONS = frozenset({".ipynb", ".docx", ".xlsx"})

_DOCUMENT_LABELS = {
    ".ipynb": "Jupyter notebook",
    ".docx": "Word document",
    ".xlsx": "Excel spreadsheet",
}

# Caps keep a pathological spreadsheet from exhausting memory or the context
# budget before the read tool's own line/byte truncation even runs.
_MAX_ROWS_PER_SHEET = 5000
_MAX_COLUMNS = 256

_WORDPROCESSING_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class ExtractionError(Exception):
    """Raised when an extractable document is malformed and cannot be rendered."""


def is_extractable_document(name: str) -> bool:
    """Return whether a filename's extension is one this module can render."""
    return Path(name).suffix.lower() in EXTRACTABLE_EXTENSIONS


def document_label(name: str) -> str:
    """Return a human label for the document type (for the extraction header)."""
    return _DOCUMENT_LABELS.get(Path(name).suffix.lower(), "document")


def extract_document_text(path: Path) -> str:
    """Render an extractable document as plain text. Dispatch is by extension."""
    suffix = path.suffix.lower()
    if suffix == ".ipynb":
        return _extract_ipynb(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".xlsx":
        return _extract_xlsx(path)
    raise ExtractionError(f"not an extractable document: {path.name}")


def _local_name(tag: str) -> str:
    """Strip an ElementTree ``{namespace}local`` tag down to its local name."""
    return tag.rsplit("}", 1)[-1]


def _extract_ipynb(path: Path) -> str:
    """Render a notebook as ``# Cell N [type]`` blocks joined by blank lines."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ExtractionError(f"cannot parse notebook: {error}") from error

    if not isinstance(document, dict):
        raise ExtractionError("notebook root is not an object")

    cells = document.get("cells")
    if not isinstance(cells, list):
        raise ExtractionError("notebook has no cells array")

    blocks: list[str] = []
    for index, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        cell_type = cell.get("cell_type", "unknown")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(part for part in source if isinstance(part, str))
        elif not isinstance(source, str):
            source = ""
        blocks.append(f"# Cell {index} [{cell_type}]\n{source}")

    return "\n\n".join(blocks)


def _read_zip_member(path: Path, member: str) -> bytes | None:
    """Read one archive member, returning ``None`` if it is absent."""
    from zipfile import BadZipFile, ZipFile

    try:
        with ZipFile(path) as archive:
            try:
                return archive.read(member)
            except KeyError:
                return None
    except (BadZipFile, OSError) as error:
        raise ExtractionError(f"cannot open archive: {error}") from error


def _parse_xml(data: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise ExtractionError(f"malformed XML: {error}") from error


def _extract_docx(path: Path) -> str:
    """Collect paragraph text from ``word/document.xml``.

    Paragraphs (``w:p``) become lines; within a paragraph ``w:t`` runs are text,
    ``w:tab`` is a tab, and ``w:br``/``w:cr`` are newlines. Table cells contain
    their own paragraphs, so their text is picked up in document order too.
    """
    document_xml = _read_zip_member(path, "word/document.xml")
    if document_xml is None:
        raise ExtractionError("docx has no word/document.xml")

    root = _parse_xml(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{{{_WORDPROCESSING_NS}}}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            tag = _local_name(node.tag)
            if tag == "t":
                pieces.append(node.text or "")
            elif tag == "tab":
                pieces.append("\t")
            elif tag in ("br", "cr"):
                pieces.append("\n")
        paragraphs.append("".join(pieces))

    return "\n".join(paragraphs)


def _extract_xlsx(path: Path) -> str:
    """Render each worksheet as tab-separated rows, sheets separated by headers."""
    shared_strings = _load_shared_strings(path)
    sheets = _resolve_worksheet_targets(path)

    rendered_sheets: list[str] = []
    for sheet_name, member in sheets:
        sheet_xml = _read_zip_member(path, member)
        if sheet_xml is None:
            continue
        rows = _render_worksheet_rows(_parse_xml(sheet_xml), shared_strings)
        body = "\n".join(rows)
        rendered_sheets.append(f"# Sheet: {sheet_name}\n{body}")

    return "\n\n".join(rendered_sheets)


def _load_shared_strings(path: Path) -> list[str]:
    """Read the workbook's shared-string table (cell text is stored once there)."""
    data = _read_zip_member(path, "xl/sharedStrings.xml")
    if data is None:
        return []

    root = _parse_xml(data)
    strings: list[str] = []
    for item in root:
        if _local_name(item.tag) != "si":
            continue
        # An <si> is either a single <t> or rich-text runs each holding a <t>.
        strings.append(
            "".join(node.text or "" for node in item.iter() if _local_name(node.tag) == "t")
        )
    return strings


def _resolve_worksheet_targets(path: Path) -> list[tuple[str, str]]:
    """Map sheet display names to their archive members in workbook order.

    Falls back to the raw ``xl/worksheets/sheet*.xml`` members (sorted) when the
    workbook relationship metadata is missing or unreadable.
    """
    workbook = _read_zip_member(path, "xl/workbook.xml")
    relationships = _read_zip_member(path, "xl/_rels/workbook.xml.rels")
    if workbook is None or relationships is None:
        return _fallback_worksheet_targets(path)

    relationship_targets = _relationship_targets(_parse_xml(relationships))
    sheets: list[tuple[str, str]] = []
    for sheet in _parse_xml(workbook).iter():
        if _local_name(sheet.tag) != "sheet":
            continue
        name = sheet.attrib.get("name", "Sheet")
        relationship_id = _sheet_relationship_id(sheet.attrib)
        target = relationship_targets.get(relationship_id) if relationship_id else None
        if target is None:
            continue
        sheets.append((name, f"xl/{target.lstrip('/')}"))

    return sheets or _fallback_worksheet_targets(path)


def _sheet_relationship_id(attributes: dict[str, str]) -> str | None:
    for key, value in attributes.items():
        if _local_name(key) == "id":
            return value
    return None


def _relationship_targets(root: ElementTree.Element) -> dict[str, str]:
    targets: dict[str, str] = {}
    for relationship in root.iter(f"{{{_RELATIONSHIPS_NS}}}Relationship"):
        relationship_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        if relationship_id and target:
            targets[relationship_id] = target
    return targets


def _fallback_worksheet_targets(path: Path) -> list[tuple[str, str]]:
    from zipfile import BadZipFile, ZipFile

    try:
        with ZipFile(path) as archive:
            members = [
                name
                for name in archive.namelist()
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            ]
    except (BadZipFile, OSError) as error:
        raise ExtractionError(f"cannot open archive: {error}") from error

    return [(Path(member).stem, member) for member in sorted(members)]


def _render_worksheet_rows(root: ElementTree.Element, shared_strings: list[str]) -> list[str]:
    rows: list[str] = []
    for row in root.iter():
        if _local_name(row.tag) != "row":
            continue
        if len(rows) >= _MAX_ROWS_PER_SHEET:
            rows.append("… [truncated]")
            break
        rows.append(_render_row_cells(row, shared_strings))
    return rows


def _render_row_cells(row: ElementTree.Element, shared_strings: list[str]) -> str:
    cells_by_column: dict[int, str] = {}
    max_column = 0
    for cell in row:
        if _local_name(cell.tag) != "c":
            continue
        column = _column_index(cell.attrib.get("r", ""))
        if column == 0 or column > _MAX_COLUMNS:
            continue
        cells_by_column[column] = _cell_text(cell, shared_strings)
        max_column = max(max_column, column)

    return "\t".join(cells_by_column.get(column, "") for column in range(1, max_column + 1))


def _cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = ""
    inline = ""
    for node in cell.iter():
        tag = _local_name(node.tag)
        if tag == "v":
            value = node.text or ""
        elif tag == "t":
            inline += node.text or ""

    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "inlineStr":
        return inline
    return value


def _column_index(cell_reference: str) -> int:
    """Convert a cell ref like ``B12`` to a 1-based column index (``2``)."""
    index = 0
    for character in cell_reference:
        if not character.isalpha():
            break
        index = index * 26 + (ord(character.upper()) - ord("A") + 1)
    return index


__all__ = [
    "EXTRACTABLE_EXTENSIONS",
    "ExtractionError",
    "document_label",
    "extract_document_text",
    "is_extractable_document",
]
