"""Create a polished PDF from a compact JSON document specification."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from xml.sax.saxutils import escape

try:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.enums import TA_CENTER, TA_LEFT  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import A4, LETTER, landscape  # type: ignore[import-untyped]
    from reportlab.lib.styles import (  # type: ignore[import-untyped]
        ParagraphStyle,
        getSampleStyleSheet,
    )
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        Image,
        ListFlowable,
        ListItem,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except ImportError as error:
    raise SystemExit(
        "ReportLab is required to create PDFs. Ask the user before installing it with "
        "'python -m pip install reportlab'."
    ) from error


DEFAULT_ACCENT = "#2563EB"
DEFAULT_TEXT = "#172033"
DEFAULT_MUTED = "#5F6B7A"
DEFAULT_SURFACE = "#F3F6FA"
DEFAULT_MARGIN_MM = 18.0
DEFAULT_IMAGE_HEIGHT_MM = 80.0
MIN_MARGIN_MM = 10.0
MAX_MARGIN_MM = 40.0
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
SUPPORTED_BLOCK_TYPES = {
    "paragraph",
    "heading",
    "bullets",
    "table",
    "callout",
    "image",
    "spacer",
    "page_break",
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="UTF-8 JSON document specification")
    parser.add_argument("output", type=Path, help="Output PDF path")
    parser.add_argument("--font-regular", type=Path, help="Optional TrueType regular font")
    parser.add_argument("--font-bold", type=Path, help="Optional TrueType bold font")
    return parser.parse_args()


def _load_spec(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Specification not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read specification: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("Specification root must be a JSON object")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Specification requires a non-empty title")
    sections = payload.get("sections", [])
    if not isinstance(sections, list):
        raise ValueError("sections must be an array")
    return payload


def _clean_text(value: Any, *, field: str, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} must not be empty")
    return escape(text).replace("\n", "<br/>")


def _color(value: Any, fallback: str, *, field: str) -> colors.Color:
    candidate = fallback if value is None else value
    if not isinstance(candidate, str) or not HEX_COLOR_PATTERN.fullmatch(candidate):
        raise ValueError(f"{field} must be a six-digit hex color such as #2563EB")
    return colors.HexColor(candidate)


def _font_candidates() -> list[tuple[Path, Path]]:
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return [
        (windows_fonts / "segoeui.ttf", windows_fonts / "segoeuib.ttf"),
        (windows_fonts / "arial.ttf", windows_fonts / "arialbd.ttf"),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
        (
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        ),
        (
            Path("/Library/Fonts/Arial Unicode.ttf"),
            Path("/Library/Fonts/Arial Bold.ttf"),
        ),
    ]


def _register_fonts(regular: Path | None, bold: Path | None) -> tuple[str, str, bool]:
    pairs = [(regular, bold)] if regular or bold else _font_candidates()
    for regular_path, bold_path in pairs:
        if regular_path is None or bold_path is None:
            continue
        if not regular_path.is_file() or not bold_path.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("VBotPDFRegular", str(regular_path)))
            pdfmetrics.registerFont(TTFont("VBotPDFBold", str(bold_path)))
        except (OSError, ValueError):
            continue
        return "VBotPDFRegular", "VBotPDFBold", True
    return "Helvetica", "Helvetica-Bold", False


def _page_size(spec: dict[str, Any]) -> tuple[float, float]:
    page = spec.get("page", {})
    if page is None:
        page = {}
    if not isinstance(page, dict):
        raise ValueError("page must be an object")
    size_name = str(page.get("size", "A4")).upper()
    if size_name == "A4":
        size = A4
    elif size_name == "LETTER":
        size = LETTER
    else:
        raise ValueError("page.size must be A4 or LETTER")
    orientation = str(page.get("orientation", "portrait")).lower()
    if orientation == "landscape":
        return cast(tuple[float, float], landscape(size))
    if orientation != "portrait":
        raise ValueError("page.orientation must be portrait or landscape")
    return cast(tuple[float, float], size)


def _margin(spec: dict[str, Any]) -> float:
    page = spec.get("page", {}) or {}
    raw = page.get("margin_mm", DEFAULT_MARGIN_MM)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError("page.margin_mm must be numeric")
    margin = float(raw)
    if not MIN_MARGIN_MM <= margin <= MAX_MARGIN_MM:
        raise ValueError(f"page.margin_mm must be between {MIN_MARGIN_MM:g} and {MAX_MARGIN_MM:g}")
    return cast(float, margin * mm)


def _styles(
    regular_font: str,
    bold_font: str,
    accent: colors.Color,
    text: colors.Color,
    muted: colors.Color,
) -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "PDFTitle",
            parent=sample["Title"],
            fontName=bold_font,
            fontSize=25,
            leading=30,
            textColor=text,
            spaceAfter=8 * mm,
        ),
        "subtitle": ParagraphStyle(
            "PDFSubtitle",
            parent=sample["Normal"],
            fontName=regular_font,
            fontSize=12,
            leading=17,
            textColor=muted,
            spaceAfter=5 * mm,
        ),
        "summary": ParagraphStyle(
            "PDFSummary",
            parent=sample["Normal"],
            fontName=regular_font,
            fontSize=10.5,
            leading=15,
            textColor=text,
            borderColor=accent,
            borderWidth=0,
            borderPadding=(3 * mm, 4 * mm, 3 * mm, 4 * mm),
            backColor=colors.HexColor(DEFAULT_SURFACE),
            spaceAfter=8 * mm,
        ),
        "h2": ParagraphStyle(
            "PDFHeading2",
            parent=sample["Heading2"],
            fontName=bold_font,
            fontSize=15,
            leading=19,
            textColor=accent,
            spaceBefore=5 * mm,
            spaceAfter=3 * mm,
            keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "PDFHeading3",
            parent=sample["Heading3"],
            fontName=bold_font,
            fontSize=11.5,
            leading=15,
            textColor=text,
            spaceBefore=3 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "PDFBody",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=9.5,
            leading=13.5,
            textColor=text,
            alignment=TA_LEFT,
            spaceAfter=3 * mm,
        ),
        "bullet": ParagraphStyle(
            "PDFBullet",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=9.5,
            leading=13.5,
            textColor=text,
            leftIndent=2 * mm,
        ),
        "table_header": ParagraphStyle(
            "PDFTableHeader",
            parent=sample["BodyText"],
            fontName=bold_font,
            fontSize=8.5,
            leading=11,
            textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "PDFTableCell",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=8.2,
            leading=10.5,
            textColor=text,
        ),
        "callout_title": ParagraphStyle(
            "PDFCalloutTitle",
            parent=sample["BodyText"],
            fontName=bold_font,
            fontSize=9.5,
            leading=12,
            textColor=accent,
            spaceAfter=1 * mm,
        ),
        "caption": ParagraphStyle(
            "PDFCaption",
            parent=sample["BodyText"],
            fontName=regular_font,
            fontSize=8,
            leading=10,
            textColor=muted,
            alignment=TA_CENTER,
            spaceBefore=1.5 * mm,
            spaceAfter=3 * mm,
        ),
    }


def _table(
    block: dict[str, Any], styles: dict[str, ParagraphStyle], width: float, accent: colors.Color
) -> Table:
    headers = block.get("headers")
    rows = block.get("rows")
    if not isinstance(headers, list) or not headers:
        raise ValueError("table.headers must be a non-empty array")
    if not isinstance(rows, list):
        raise ValueError("table.rows must be an array")
    column_count = len(headers)
    data: list[list[Paragraph]] = [
        [
            Paragraph(_clean_text(value, field="table header"), styles["table_header"])
            for value in headers
        ]
    ]
    for row_index, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) != column_count:
            raise ValueError(f"table row {row_index} must contain {column_count} cells")
        data.append(
            [
                Paragraph(_clean_text(value, field="table cell"), styles["table_cell"])
                for value in row
            ]
        )
    table = Table(data, colWidths=[width / column_count] * column_count, repeatRows=1, splitByRow=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), accent),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F9FC")]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7DDE6")),
            ]
        )
    )
    return table


def _image(block: dict[str, Any], styles: dict[str, ParagraphStyle], width: float) -> list[Any]:
    raw_path = block.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("image.path must be a non-empty string")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Image not found: {path}")
    image = Image(str(path))
    max_height = float(block.get("max_height_mm", DEFAULT_IMAGE_HEIGHT_MM)) * mm
    scale = min(width / image.imageWidth, max_height / image.imageHeight, 1.0)
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale
    flowables: list[Any] = [image]
    caption = _clean_text(block.get("caption"), field="image.caption")
    if caption:
        flowables.append(Paragraph(caption, styles["caption"]))
    else:
        flowables.append(Spacer(1, 3 * mm))
    return flowables


def _block_flowables(
    block: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    width: float,
    accent: colors.Color,
) -> list[Any]:
    block_type = block.get("type")
    if block_type not in SUPPORTED_BLOCK_TYPES:
        raise ValueError(f"Unsupported block type: {block_type!r}")
    if block_type == "paragraph":
        return [
            Paragraph(
                _clean_text(block.get("text"), field="paragraph.text", required=True),
                styles["body"],
            )
        ]
    if block_type == "heading":
        level = block.get("level", 3)
        style = styles["h2"] if level == 2 else styles["h3"]
        return [
            Paragraph(_clean_text(block.get("text"), field="heading.text", required=True), style)
        ]
    if block_type == "bullets":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("bullets.items must be a non-empty array")
        bullet_items = [
            ListItem(
                Paragraph(_clean_text(item, field="bullet item", required=True), styles["bullet"])
            )
            for item in items
        ]
        return [
            ListFlowable(bullet_items, bulletType="bullet", start="circle", leftIndent=5 * mm),
            Spacer(1, 2 * mm),
        ]
    if block_type == "table":
        return [_table(block, styles, width, accent), Spacer(1, 4 * mm)]
    if block_type == "callout":
        title = _clean_text(block.get("title"), field="callout.title")
        text = _clean_text(block.get("text"), field="callout.text", required=True)
        content: list[Any] = []
        if title:
            content.append(Paragraph(title, styles["callout_title"]))
        content.append(Paragraph(text, styles["body"]))
        table = Table([[content]], colWidths=[width])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(DEFAULT_SURFACE)),
                    ("BOX", (0, 0), (-1, -1), 0.8, accent),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1 * mm),
                ]
            )
        )
        return [table, Spacer(1, 4 * mm)]
    if block_type == "image":
        return _image(block, styles, width)
    if block_type == "spacer":
        raw_height = block.get("height_mm", 4)
        if not isinstance(raw_height, (int, float)) or isinstance(raw_height, bool):
            raise ValueError("spacer.height_mm must be numeric")
        return [Spacer(1, max(0.0, min(float(raw_height), 40.0)) * mm)]
    if block_type == "page_break":
        return [PageBreak()]
    raise AssertionError("unreachable block type")


def _page_decorator(title: str, regular_font: str, muted: colors.Color, accent: colors.Color):
    def draw(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(accent)
        canvas.setLineWidth(0.7)
        top = document.pagesize[1] - 11 * mm
        canvas.line(document.leftMargin, top, document.pagesize[0] - document.rightMargin, top)
        canvas.setFont(regular_font, 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(document.leftMargin, 7.5 * mm, title[:90])
        canvas.drawRightString(
            document.pagesize[0] - document.rightMargin,
            7.5 * mm,
            f"Page {canvas.getPageNumber()}",
        )
        canvas.restoreState()

    return draw


def _build(
    spec: dict[str, Any], output: Path, regular: Path | None, bold: Path | None
) -> dict[str, Any]:
    theme = spec.get("theme", {}) or {}
    if not isinstance(theme, dict):
        raise ValueError("theme must be an object")
    accent = _color(theme.get("accent"), DEFAULT_ACCENT, field="theme.accent")
    text = _color(theme.get("text"), DEFAULT_TEXT, field="theme.text")
    muted = _color(theme.get("muted"), DEFAULT_MUTED, field="theme.muted")
    regular_font, bold_font, embedded_font = _register_fonts(regular, bold)
    page_size = _page_size(spec)
    margin = _margin(spec)
    output.parent.mkdir(parents=True, exist_ok=True)
    title_plain = str(spec["title"]).strip()
    styles = _styles(regular_font, bold_font, accent, text, muted)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".pdf", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        document = SimpleDocTemplate(
            str(temporary_path),
            pagesize=page_size,
            leftMargin=margin,
            rightMargin=margin,
            topMargin=18 * mm,
            bottomMargin=14 * mm,
            title=title_plain,
            author=str(spec.get("author", "")).strip(),
        )
        story: list[Any] = [
            Paragraph(_clean_text(spec["title"], field="title", required=True), styles["title"])
        ]
        subtitle = _clean_text(spec.get("subtitle"), field="subtitle")
        author = _clean_text(spec.get("author"), field="author")
        if subtitle:
            story.append(Paragraph(subtitle, styles["subtitle"]))
        if author:
            story.append(Paragraph(author, styles["subtitle"]))
        summary = _clean_text(spec.get("summary"), field="summary")
        if summary:
            story.append(Paragraph(summary, styles["summary"]))
        for section_index, section in enumerate(spec.get("sections", []), start=1):
            if not isinstance(section, dict):
                raise ValueError(f"section {section_index} must be an object")
            heading = _clean_text(
                section.get("heading"), field=f"section {section_index}.heading", required=True
            )
            story.append(Paragraph(heading, styles["h2"]))
            blocks = section.get("blocks", [])
            if not isinstance(blocks, list):
                raise ValueError(f"section {section_index}.blocks must be an array")
            for block_index, block in enumerate(blocks, start=1):
                if not isinstance(block, dict):
                    raise ValueError(
                        f"section {section_index} block {block_index} must be an object"
                    )
                story.extend(_block_flowables(block, styles, document.width, accent))
        document.build(
            story,
            onFirstPage=_page_decorator(title_plain, regular_font, muted, accent),
            onLaterPages=_page_decorator(title_plain, regular_font, muted, accent),
        )
        os.replace(temporary_path, output)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return {"output": str(output.resolve()), "font_embedded": embedded_font}


def main() -> int:
    arguments = _arguments()
    try:
        spec = _load_spec(arguments.spec)
        result = _build(
            spec,
            arguments.output.expanduser().resolve(),
            arguments.font_regular,
            arguments.font_bold,
        )
    except (OSError, ValueError) as error:
        print(f"PDF creation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
