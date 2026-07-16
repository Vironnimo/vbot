"""Text extraction for documents read by the ``read`` and ``web_fetch`` tools.

Renders PDF / ``.docx`` / ``.xlsx`` / ``.ipynb`` bytes as readable plain text so
the read tool shows their content instead of a binary notice (or raw JSON for a
notebook) and web_fetch returns text instead of a binary notice. Office and
notebook rendering uses only the standard library (``zipfile`` + ``xml.etree`` +
``json``); PDF text extraction uses ``pypdf`` (pure Python, no native code).

Both callers hand in the bytes they already hold, so this module never touches
disk — detection keys off the sniffed media type (reliable for a fetched URL
with no usable extension) with the filename extension as a fallback.
"""

from __future__ import annotations

import json
import zlib
from io import BytesIO
from pathlib import Path
from threading import RLock
from xml.etree import ElementTree

# Human label per document kind, used in the extraction header the callers build.
_DOCUMENT_LABELS = {
    "pdf": "PDF document",
    "docx": "Word document",
    "xlsx": "Excel spreadsheet",
    "ipynb": "Jupyter notebook",
}

# Sniffed media types that unambiguously identify a kind from magic bytes alone,
# so a fetched URL without a usable extension is still recognized. ``.ipynb`` has
# no distinct sniffed type (it decodes as text/JSON), so it is extension-only.
_MEDIA_TYPE_KINDS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

_EXTENSION_KINDS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".ipynb": "ipynb",
}

# Caps keep a pathological spreadsheet from exhausting memory or the context
# budget before the read tool's own line/byte truncation even runs.
_MAX_ROWS_PER_SHEET = 5000
_MAX_COLUMNS = 256
_MAX_DOCUMENT_EXTRACTED_BYTES = 128 * 1024 * 1024
_MAX_DOCUMENT_EXTRACTED_SIZE_LABEL = "128 MB"
_PDF_DECOMPRESSION_LOCK = RLock()

_WORDPROCESSING_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class ExtractionError(Exception):
    """Raised when an extractable document is malformed and cannot be rendered."""


class ExtractionLimitExceededError(ExtractionError):
    """Raised when document processing would exceed its decompression budget."""


class _ExtractionBudget:
    """Track the total uncompressed document data admitted to an extractor."""

    def __init__(self) -> None:
        self.remaining = _MAX_DOCUMENT_EXTRACTED_BYTES

    def consume(self, size: int) -> None:
        if size > self.remaining:
            raise ExtractionLimitExceededError(
                f"document exceeds the {_MAX_DOCUMENT_EXTRACTED_SIZE_LABEL} extraction limit"
            )
        self.remaining -= size


def detect_extractable_document(filename: str, media_type: str) -> str | None:
    """Return the document kind for a file, or ``None`` when it is not extractable.

    A kind is recognized from the sniffed ``media_type`` first (reliable even for
    a fetched URL whose path has no usable extension) and from the filename
    extension second (covers ``.ipynb``, which has no distinct sniffed type, and
    any file whose bytes were not sniffed into a known document type).
    """
    kind = _MEDIA_TYPE_KINDS.get(media_type)
    if kind is not None:
        return kind
    return _EXTENSION_KINDS.get(Path(filename).suffix.lower())


def document_label(kind: str) -> str:
    """Return a human label for a document kind (for the extraction header)."""
    return _DOCUMENT_LABELS.get(kind, "document")


def ensure_document_input_size(size_bytes: int) -> int:
    """Reject a document whose compressed/raw input already exceeds the budget."""
    if size_bytes > _MAX_DOCUMENT_EXTRACTED_BYTES:
        raise ExtractionLimitExceededError(
            f"document exceeds the {_MAX_DOCUMENT_EXTRACTED_SIZE_LABEL} extraction limit"
        )
    return _MAX_DOCUMENT_EXTRACTED_BYTES


def extract_document_text(data: bytes, kind: str) -> str:
    """Render document bytes of the given kind as plain text."""
    budget = _ExtractionBudget()
    if kind == "pdf":
        budget.consume(len(data))
        return _extract_pdf(data, budget)
    if kind == "ipynb":
        budget.consume(len(data))
        return _extract_ipynb(data)
    if kind == "docx":
        return _extract_docx(data, budget)
    if kind == "xlsx":
        return _extract_xlsx(data, budget)
    raise ExtractionError(f"unknown document kind: {kind}")


def _extract_pdf(data: bytes, budget: _ExtractionBudget) -> str:
    """Render a PDF's text layer, page-delimited by ``# Page N`` headers.

    A scanned (image-only) PDF has no text layer, so every page extracts empty;
    that returns ``""`` and the caller surfaces an explicit "no extractable text"
    note rather than a wall of bare page headers. A malformed or password-locked
    PDF raises ``ExtractionError`` so the caller falls back to a binary notice.
    The broad ``except`` is deliberate: pypdf raises a wide, undocumented range on
    a malformed file (and page access is lazy), and a bad document must never take
    the run down — it is converted to a domain error, never swallowed.
    """
    import pypdf.filters as pypdf_filters
    from pypdf import PdfReader

    # pypdf's Flate decoder normally materializes a whole stream at once. Replace
    # it only while this synchronous extraction runs, guarded process-wide because
    # the decoder is a dependency-module global. The lock keeps concurrent callers
    # from observing a partially restored decoder.
    with _PDF_DECOMPRESSION_LOCK:
        original_decompress = pypdf_filters.decompress
        pypdf_filters.decompress = lambda data: _bounded_pdf_decompress(data, budget)
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                # A PDF is often encrypted with an empty owner password purely to set
                # permissions; that still opens for reading. A real password fails.
                reader.decrypt("")
            page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
        except ExtractionError:
            raise
        except Exception as error:
            raise ExtractionError(f"cannot read PDF: {error}") from error
        finally:
            pypdf_filters.decompress = original_decompress

    if not any(page_texts):
        return ""
    return "\n\n".join(f"# Page {index}\n{text}" for index, text in enumerate(page_texts, start=1))


def _bounded_pdf_decompress(data: bytes, budget: _ExtractionBudget) -> bytes:
    """Decode one Flate stream without allowing pypdf to inflate beyond the budget."""
    try:
        decoder = zlib.decompressobj()
        expanded = decoder.decompress(data, budget.remaining + 1)
        if len(expanded) > budget.remaining or decoder.unconsumed_tail:
            raise ExtractionLimitExceededError(
                f"document exceeds the {_MAX_DOCUMENT_EXTRACTED_SIZE_LABEL} extraction limit"
            )
        tail = decoder.flush(budget.remaining - len(expanded) + 1)
    except zlib.error as error:
        raise ExtractionError(f"cannot decompress PDF stream: {error}") from error

    if len(tail) > budget.remaining - len(expanded):
        raise ExtractionLimitExceededError(
            f"document exceeds the {_MAX_DOCUMENT_EXTRACTED_SIZE_LABEL} extraction limit"
        )
    content = expanded + tail
    budget.consume(len(content))
    return content


def _local_name(tag: str) -> str:
    """Strip an ElementTree ``{namespace}local`` tag down to its local name."""
    return tag.rsplit("}", 1)[-1]


def _extract_ipynb(data: bytes) -> str:
    """Render a notebook as ``# Cell N [type]`` blocks joined by blank lines."""
    try:
        document = json.loads(data)
    except ValueError as error:
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


def _read_zip_member(data: bytes, member: str, budget: _ExtractionBudget) -> bytes | None:
    """Read one archive member without exceeding the document's expansion budget."""
    from zipfile import BadZipFile, ZipFile

    try:
        with ZipFile(BytesIO(data)) as archive:
            try:
                info = archive.getinfo(member)
            except KeyError:
                return None
            budget.consume(info.file_size)
            with archive.open(info) as handle:
                content = handle.read(info.file_size + 1)
            # ``ZipInfo.file_size`` is normally authoritative, but check the
            # actual result as well so malformed metadata never bypasses the cap.
            if len(content) > info.file_size:
                raise ExtractionLimitExceededError(
                    f"document exceeds the {_MAX_DOCUMENT_EXTRACTED_SIZE_LABEL} extraction limit"
                )
            return content
    except (BadZipFile, OSError) as error:
        raise ExtractionError(f"cannot open archive: {error}") from error


def _parse_xml(data: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise ExtractionError(f"malformed XML: {error}") from error


def _extract_docx(data: bytes, budget: _ExtractionBudget) -> str:
    """Collect paragraph text from ``word/document.xml``.

    Paragraphs (``w:p``) become lines; within a paragraph ``w:t`` runs are text,
    ``w:tab`` is a tab, and ``w:br``/``w:cr`` are newlines. Table cells contain
    their own paragraphs, so their text is picked up in document order too.
    """
    document_xml = _read_zip_member(data, "word/document.xml", budget)
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


def _extract_xlsx(data: bytes, budget: _ExtractionBudget) -> str:
    """Render each worksheet as tab-separated rows, sheets separated by headers."""
    shared_strings = _load_shared_strings(data, budget)
    sheets = _resolve_worksheet_targets(data, budget)

    rendered_sheets: list[str] = []
    for sheet_name, member in sheets:
        sheet_xml = _read_zip_member(data, member, budget)
        if sheet_xml is None:
            continue
        rows = _render_worksheet_rows(_parse_xml(sheet_xml), shared_strings)
        body = "\n".join(rows)
        rendered_sheets.append(f"# Sheet: {sheet_name}\n{body}")

    return "\n\n".join(rendered_sheets)


def _load_shared_strings(data: bytes, budget: _ExtractionBudget) -> list[str]:
    """Read the workbook's shared-string table (cell text is stored once there)."""
    member = _read_zip_member(data, "xl/sharedStrings.xml", budget)
    if member is None:
        return []

    root = _parse_xml(member)
    strings: list[str] = []
    for item in root:
        if _local_name(item.tag) != "si":
            continue
        # An <si> is either a single <t> or rich-text runs each holding a <t>.
        strings.append(
            "".join(node.text or "" for node in item.iter() if _local_name(node.tag) == "t")
        )
    return strings


def _resolve_worksheet_targets(data: bytes, budget: _ExtractionBudget) -> list[tuple[str, str]]:
    """Map sheet display names to their archive members in workbook order.

    Falls back to the raw ``xl/worksheets/sheet*.xml`` members (sorted) when the
    workbook relationship metadata is missing or unreadable.
    """
    workbook = _read_zip_member(data, "xl/workbook.xml", budget)
    relationships = _read_zip_member(data, "xl/_rels/workbook.xml.rels", budget)
    if workbook is None or relationships is None:
        return _fallback_worksheet_targets(data)

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

    return sheets or _fallback_worksheet_targets(data)


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


def _fallback_worksheet_targets(data: bytes) -> list[tuple[str, str]]:
    from zipfile import BadZipFile, ZipFile

    try:
        with ZipFile(BytesIO(data)) as archive:
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
    "ExtractionError",
    "ExtractionLimitExceededError",
    "detect_extractable_document",
    "document_label",
    "extract_document_text",
]
