"""Inspect PDF structure and report machine-checkable diagnostics as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from pypdf import PdfReader
except ImportError as error:
    raise SystemExit("pypdf is required for structural PDF inspection") from error


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Input PDF")
    return parser.parse_args()


def _number(value: Any) -> float:
    return round(float(value), 2)


def _inspect(path: Path) -> dict[str, Any]:
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise ValueError("PDF is encrypted; provide an authorized decrypted copy for inspection")
    pages: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        try:
            text = page.extract_text() or ""
            text_error = None
        except Exception as error:
            text = ""
            text_error = str(error)
        annotations = page.get("/Annots")
        annotation_count = len(annotations) if isinstance(annotations, list) else 0
        page_data: dict[str, Any] = {
            "page": index,
            "width_points": _number(box.width),
            "height_points": _number(box.height),
            "rotation": int(page.get("/Rotate", 0) or 0),
            "text_characters": len(text.strip()),
            "annotation_count": annotation_count,
        }
        if text_error:
            page_data["text_extraction_error"] = text_error
            warnings.append(f"Page {index}: text extraction failed")
        elif not text.strip():
            warnings.append(f"Page {index}: no extractable text; inspect the rendered page")
        pages.append(page_data)
    if not pages:
        raise ValueError("PDF contains no pages")
    fields = reader.get_fields() or {}
    metadata = reader.metadata
    return {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "page_count": len(pages),
        "pages": pages,
        "form_field_count": len(fields),
        "form_field_names": sorted(str(name) for name in fields),
        "metadata": {
            "title": str(metadata.title) if metadata and metadata.title else None,
            "author": str(metadata.author) if metadata and metadata.author else None,
        },
        "warnings": warnings,
    }


def main() -> int:
    arguments = _arguments()
    path = arguments.pdf.expanduser().resolve()
    if not path.is_file():
        print(f"PDF not found: {path}", file=sys.stderr)
        return 2
    try:
        result = _inspect(path)
    except Exception as error:
        print(f"PDF inspection failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
