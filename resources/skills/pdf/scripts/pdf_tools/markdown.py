"""Build a PDF from Markdown with ReportLab, for hosts without a browser."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .common import AtomicOutput, CommandError
from .fonts import Family, register_family

PAGE_LABELS = {
    "de": "Seite {page} von {total}",
    "en": "Page {page} of {total}",
    "fr": "Page {page} sur {total}",
    "es": "Página {page} de {total}",
    "it": "Pagina {page} di {total}",
    "nl": "Pagina {page} van {total}",
    "pt": "Página {page} de {total}",
    "pl": "Strona {page} z {total}",
    "sv": "Sida {page} av {total}",
    "da": "Side {page} af {total}",
    "no": "Side {page} av {total}",
}
CONTENTS_LABELS = {"de": "Inhalt", "fr": "Sommaire", "es": "Índice", "it": "Indice", "nl": "Inhoud"}
CALLOUTS = {
    "NOTE": "#2563EB",
    "TIP": "#15803D",
    "IMPORTANT": "#7C3AED",
    "WARNING": "#B45309",
    "CAUTION": "#B91C1C",
}
TEXT = "#1D2433"
MUTED = "#5B6472"
RULE = "#D5DAE1"
SURFACE = "#F4F6F8"

Block = tuple[str, Any]
# Paragraph styles by name, heading styles by level (1-4).
Styles = dict[Any, Any]


# Parsing


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].strip() in ("---", "..."):
            meta: dict[str, str] = {}
            for line in lines[1:end]:
                key, separator, value = line.partition(":")
                if separator and key.strip():
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            return meta, "\n".join(lines[end + 1 :])
    return {}, text


LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
IMAGE_LINE = re.compile(r'^!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)\s*$')


def parse_blocks(text: str) -> list[Block]:
    lines = text.replace("\t", "    ").splitlines()
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(fence):
                body.append(lines[index])
                index += 1
            blocks.append(("code", "\n".join(body)))
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", stripped)
        if heading:
            blocks.append(("heading", (min(len(heading.group(1)), 4), heading.group(2))))
            index += 1
            continue
        if stripped in ("\\pagebreak", "<!-- pagebreak -->", "\\newpage"):
            blocks.append(("pagebreak", None))
            index += 1
            continue
        if re.fullmatch(r"(-\s*){3,}|(\*\s*){3,}|(_\s*){3,}", stripped):
            blocks.append(("rule", None))
            index += 1
            continue
        image = IMAGE_LINE.match(stripped)
        if image:
            blocks.append(("image", (image.group(2), image.group(1) or image.group(3) or "")))
            index += 1
            continue
        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and TABLE_SEPARATOR.match(lines[index + 1])
        ):
            index = _parse_table(lines, index, blocks)
            continue
        if stripped.startswith(">"):
            quoted: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            blocks.append(_callout(quoted))
            continue
        if LIST_ITEM.match(line):
            index = _parse_list(lines, index, blocks)
            continue
        paragraph: list[str] = []
        while index < len(lines) and lines[index].strip() and not _starts_block(lines, index):
            paragraph.append(lines[index])
            index += 1
        if not paragraph:
            paragraph.append(lines[index])
            index += 1
        blocks.append(("paragraph", paragraph))
    return blocks


def _starts_block(lines: list[str], index: int) -> bool:
    stripped = lines[index].strip()
    if stripped.startswith(("```", "~~~", "#", ">")) or LIST_ITEM.match(lines[index]):
        return True
    return (
        stripped.startswith("|")
        and index + 1 < len(lines)
        and bool(TABLE_SEPARATOR.match(lines[index + 1]))
    )


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", stripped)]


def _parse_table(lines: list[str], index: int, blocks: list[Block]) -> int:
    header = _cells(lines[index])
    alignments = []
    for cell in _cells(lines[index + 1]):
        if cell.startswith(":") and cell.endswith(":"):
            alignments.append("CENTER")
        elif cell.endswith(":"):
            alignments.append("RIGHT")
        else:
            alignments.append("LEFT")
    rows: list[list[str]] = []
    index += 2
    while index < len(lines) and lines[index].strip().startswith("|"):
        row = _cells(lines[index])
        row = (row + [""] * len(header))[: len(header)]
        rows.append(row)
        index += 1
    alignments = (alignments + ["LEFT"] * len(header))[: len(header)]
    blocks.append(("table", (header, alignments, rows)))
    return index


def _callout(lines: list[str]) -> Block:
    first = lines[0].strip() if lines else ""
    alert = re.match(r"^\[!(\w+)\]\s*(.*)$", first)
    if alert and alert.group(1).upper() in CALLOUTS:
        kind = alert.group(1).upper()
        return ("callout", (kind, alert.group(2), lines[1:]))
    return ("callout", ("QUOTE", "", lines))


def _parse_list(lines: list[str], index: int, blocks: list[Block]) -> int:
    items: list[tuple[int, bool, str]] = []
    while index < len(lines):
        line = lines[index]
        match = LIST_ITEM.match(line)
        if match:
            indent = len(match.group(1))
            ordered = match.group(2)[0].isdigit()
            if items and indent <= items[0][0] and ordered != items[0][1]:
                break  # a different list type at the outer level starts a new list
            items.append((indent, ordered, match.group(3)))
            index += 1
            continue
        if line.strip() and line.startswith("  ") and items:
            indent, ordered, content = items[-1]
            items[-1] = (indent, ordered, content + " " + line.strip())
            index += 1
            continue
        if not line.strip() and index + 1 < len(lines) and LIST_ITEM.match(lines[index + 1]):
            index += 1
            continue
        break
    blocks.append(("list", _nest(items)))
    return index


def _nest(items: list[tuple[int, bool, str]]) -> dict[str, Any]:
    """Turn indented items into nested lists; deeper indentation nests under the item above."""
    root: dict[str, Any] = {"ordered": items[0][1], "items": []}
    stack: list[tuple[int, dict[str, Any]]] = [(items[0][0], root)]
    for indent, ordered, content in items:
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
        current_indent, current = stack[-1]
        if indent > current_indent and current["items"]:
            child: dict[str, Any] = {"ordered": ordered, "items": []}
            current["items"][-1]["children"] = child
            stack.append((indent, child))
            current = child
        current["items"].append({"text": content})
    return root


# Inline markup


class Inline:
    """Markdown inline syntax to ReportLab paragraph markup, with font fallback."""

    PATTERN = re.compile(
        r"`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|<(https?://[^>\s]+)>"
    )

    def __init__(self, family: Family, accent: str) -> None:
        self.family = family
        self.accent = accent

    def text(self, value: str) -> str:
        parts: list[str] = []
        for character in value:
            if character.isspace() or self.family.covers(character):
                parts.append(escape(character))
                continue
            fallback = self.family.fallback_for(character)
            if fallback:
                parts.append(f'<font name="{fallback}">{escape(character)}</font>')
            else:
                self.family.report_missing(character)
                parts.append(escape(character))
        return "".join(parts)

    def emphasis(self, value: str) -> str:
        result = self.text(value)
        result = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", result)
        result = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", result)
        result = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", result)
        result = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"<i>\1</i>", result)
        result = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<strike>\1</strike>", result)
        return result

    def convert(self, value: str) -> str:
        output: list[str] = []
        position = 0
        for match in self.PATTERN.finditer(value):
            output.append(self.emphasis(value[position : match.start()]))
            code, label, target, autolink = match.groups()
            if code is not None:
                output.append(f'<font name="{self.family.mono}" size="-1">{escape(code)}</font>')
            elif label is not None:
                href = escape(target, {'"': "&quot;"})
                output.append(f'<a href="{href}" color="{self.accent}">{self.emphasis(label)}</a>')
            else:
                href = escape(autolink, {'"': "&quot;"})
                output.append(f'<a href="{href}" color="{self.accent}">{escape(autolink)}</a>')
            position = match.end()
        output.append(self.emphasis(value[position:]))
        return "".join(output)

    def lines(self, lines: list[str]) -> str:
        parts: list[str] = []
        for number, line in enumerate(lines):
            hard_break = line.endswith("  ") or line.rstrip().endswith("\\")
            content = line.strip().rstrip("\\").strip()
            parts.append(self.convert(content))
            if number < len(lines) - 1:
                parts.append("<br/>" if hard_break else " ")
        return "".join(parts)


# Building


def build_markdown(source: Path, target: Path) -> list[str]:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import (  # type: ignore[import-untyped]
        A4,
        A5,
        LEGAL,
        LETTER,
        landscape,
    )
    from reportlab.lib.units import mm  # type: ignore[import-untyped]

    meta, body = parse_front_matter(source.read_text(encoding="utf-8-sig"))
    blocks = parse_blocks(body)
    accent = meta.get("accent", "#1F4E79")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", accent):
        raise CommandError(f"accent must be a hex color such as #1F4E79, not '{accent}'.")
    sizes = {"A4": A4, "A5": A5, "LETTER": LETTER, "LEGAL": LEGAL}
    size_name = meta.get("page", "A4").upper()
    if size_name not in sizes:
        raise CommandError("page must be A4, A5, Letter or Legal.")
    page_size = sizes[size_name]
    if meta.get("orientation", "portrait").lower() == "landscape":
        page_size = landscape(page_size)
    language = meta.get("lang", "en").split("-")[0].lower()
    title = meta.get("title", "")
    if not title and blocks and blocks[0][0] == "heading" and blocks[0][1][0] == 1:
        title = blocks.pop(0)[1][1]
    family = register_family()
    inline = Inline(family, accent)
    styles = _styles(family, colors.HexColor(accent))
    story = _title_block(meta, title, inline, styles, colors.HexColor(accent))
    if meta.get("toc", "").lower() in ("true", "yes", "1"):
        story.extend(_contents(language, styles))
    width = page_size[0] - 44 * mm
    for block in blocks:
        story.extend(_flowables(block, inline, styles, width, accent, source.parent))
    levels = [block[1][0] for block in blocks if block[0] == "heading"]
    _write(target, story, page_size, meta, title, language, family, min(levels, default=1))
    problems: list[str] = []
    if family.missing:
        shown = ", ".join(f"{char} (U+{ord(char):04X})" for char in sorted(family.missing)[:12])
        problems.append(
            f"No available font has these characters, so they print as empty boxes: {shown}. "
            "Replace them with words, or install a font with TrueType outlines that covers them."
        )
    if family.regular == "Helvetica":
        problems.append(
            "No TrueType font was found; the built-in Helvetica covers Western European text only."
        )
    return problems


def _styles(family: Family, accent: Any) -> Styles:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]

    text, muted = colors.HexColor(TEXT), colors.HexColor(MUTED)
    body = ParagraphStyle(
        "Body",
        fontName=family.regular,
        fontSize=10,
        leading=14.6,
        textColor=text,
        spaceAfter=2.6 * mm,
    )
    return {
        "body": body,
        "title": ParagraphStyle(
            "Title", parent=body, fontName=family.bold, fontSize=26, leading=31, spaceAfter=2.5 * mm
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=body, fontSize=13, leading=18, textColor=muted, spaceAfter=1.5 * mm
        ),
        "meta": ParagraphStyle("Meta", parent=body, fontSize=9, leading=12, textColor=muted),
        1: ParagraphStyle(
            "H1",
            parent=body,
            fontName=family.bold,
            fontSize=17,
            leading=22,
            spaceBefore=7 * mm,
            spaceAfter=2.5 * mm,
            keepWithNext=True,
        ),
        2: ParagraphStyle(
            "H2",
            parent=body,
            fontName=family.bold,
            fontSize=13.5,
            leading=18,
            spaceBefore=5.5 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
        ),
        3: ParagraphStyle(
            "H3",
            parent=body,
            fontName=family.bold,
            fontSize=11,
            leading=15,
            spaceBefore=4 * mm,
            spaceAfter=1.5 * mm,
            keepWithNext=True,
        ),
        4: ParagraphStyle(
            "H4",
            parent=body,
            fontName=family.bold,
            fontSize=10,
            leading=14,
            textColor=muted,
            spaceBefore=3 * mm,
            spaceAfter=1 * mm,
            keepWithNext=True,
        ),
        "item": ParagraphStyle("Item", parent=body, spaceAfter=1.2 * mm),
        "cell": ParagraphStyle("Cell", parent=body, fontSize=9, leading=12.4, spaceAfter=0),
        "head": ParagraphStyle(
            "Head", parent=body, fontName=family.bold, fontSize=9, leading=12.4, spaceAfter=0
        ),
        "caption": ParagraphStyle(
            "Caption",
            parent=body,
            fontSize=8.5,
            leading=11.5,
            textColor=muted,
            spaceBefore=1.5 * mm,
            spaceAfter=4 * mm,
        ),
        "code": ParagraphStyle(
            "Code", parent=body, fontName=family.mono, fontSize=8.4, leading=11.4, spaceAfter=0
        ),
        "toc": ParagraphStyle("Toc", parent=body, spaceAfter=1 * mm),
        "accent": accent,
    }


def _title_block(
    meta: dict[str, str], title: str, inline: Inline, styles: Styles, accent: Any
) -> list[Any]:
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import Paragraph, Spacer  # type: ignore[import-untyped]
    from reportlab.platypus.flowables import HRFlowable  # type: ignore[import-untyped]

    story: list[Any] = []
    if not title:
        return story
    story.append(
        HRFlowable(width=18 * mm, thickness=2.2, color=accent, hAlign="LEFT", spaceAfter=5 * mm)
    )
    story.append(Paragraph(inline.convert(title), styles["title"]))
    if meta.get("subtitle"):
        story.append(Paragraph(inline.convert(meta["subtitle"]), styles["subtitle"]))
    details = [meta[key] for key in ("author", "date") if meta.get(key)]
    if details:
        story.append(Paragraph(inline.convert("  ·  ".join(details)), styles["meta"]))
    story.append(Spacer(1, 9 * mm))
    return story


def _contents(language: str, styles: Styles) -> list[Any]:
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import PageBreak, Paragraph  # type: ignore[import-untyped]
    from reportlab.platypus.tableofcontents import TableOfContents  # type: ignore[import-untyped]

    contents = TableOfContents()
    level_styles = []
    for level in range(2):
        style = styles["toc"].clone(f"Toc{level}")
        style.leftIndent = level * 6 * mm
        style.fontName = styles[1].fontName if level == 0 else styles["body"].fontName
        level_styles.append(style)
    contents.levelStyles = level_styles
    contents.dotsMinLevel = 0
    label = CONTENTS_LABELS.get(language, "Contents")
    return [Paragraph(label, styles[2]), contents, PageBreak()]


def _flowables(
    block: Block, inline: Inline, styles: Styles, width: float, accent: str, base: Path
) -> list[Any]:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        PageBreak,
        Paragraph,
        Preformatted,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.flowables import HRFlowable  # type: ignore[import-untyped]

    kind, data = block
    if kind == "heading":
        level, text = data
        paragraph = Paragraph(inline.convert(text), styles[level])
        paragraph._bookmark = (level, re.sub(r"[*_`]", "", text))
        return [paragraph]
    if kind == "paragraph":
        return [Paragraph(inline.lines(data), styles["body"])]
    if kind == "pagebreak":
        return [PageBreak()]
    if kind == "rule":
        return [
            HRFlowable(
                width="100%",
                thickness=0.6,
                color=colors.HexColor(RULE),
                spaceBefore=2 * mm,
                spaceAfter=4 * mm,
            )
        ]
    if kind == "list":
        return [_list(data, inline, styles, accent), Spacer(1, 1.6 * mm)]
    if kind == "code":
        characters = max(40, int(width / (styles["code"].fontSize * 0.6)) - 4)
        code = Preformatted(data, styles["code"], maxLineLength=characters, newLineChars="")
        box = Table([[code]], colWidths=[width])
        box.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), styles["body"].fontName),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SURFACE)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3.5 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3.5 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.6 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6 * mm),
                ]
            )
        )
        return [box, Spacer(1, 3.5 * mm)]
    if kind == "table":
        return [_table(data, inline, styles, width, accent), Spacer(1, 4 * mm)]
    if kind == "callout":
        return [_callout_box(data, inline, styles, width), Spacer(1, 3.5 * mm)]
    if kind == "image":
        return _image(data, inline, styles, width, base)
    raise AssertionError(kind)


def _list(data: dict[str, Any], inline: Inline, styles: Styles, accent: str, depth: int = 0) -> Any:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import ListFlowable, ListItem, Paragraph  # type: ignore[import-untyped]

    items = []
    for item in data["items"]:
        text = item["text"]
        task = re.match(r"^\[([ xX])\]\s+(.*)$", text)
        if task:
            text = ("☑ " if task.group(1).lower() == "x" else "☐ ") + task.group(2)
        content: list[Any] = [Paragraph(inline.convert(text), styles["item"])]
        if "children" in item:
            content.append(_list(item["children"], inline, styles, accent, depth + 1))
        items.append(ListItem(content))
    body = styles["body"]
    if data["ordered"]:
        return ListFlowable(
            items,
            bulletType="1",
            bulletFormat="%s.",
            leftIndent=6.5 * mm,
            bulletFontName=body.fontName,
            bulletFontSize=body.fontSize,
            bulletColor=colors.HexColor(MUTED),
        )
    tasks = all(re.match(r"^\[[ xX]\]\s", item["text"]) for item in data["items"])
    marks = ["•", "–", "·"]
    mark = " " if tasks else marks[depth % 3]  # task items show their own box
    return ListFlowable(
        items,
        bulletType="bullet",
        start=mark,
        leftIndent=0 if tasks else 5.5 * mm,
        bulletFontName=body.fontName,
        bulletFontSize=body.fontSize,
        bulletColor=colors.HexColor(accent),
        bulletOffsetY=0,
    )


def _table(
    data: tuple[list[str], list[str], list[list[str]]],
    inline: Inline,
    styles: Styles,
    width: float,
    accent: str,
) -> Any:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import Paragraph, Table, TableStyle  # type: ignore[import-untyped]

    header, alignments, rows = data
    align = {"LEFT": TA_LEFT, "RIGHT": TA_RIGHT, "CENTER": TA_CENTER}
    weights = []
    for column in range(len(header)):
        lengths = [len(header[column])] + [len(row[column]) for row in rows]
        weights.append(min(max(lengths), 60) + 4)
    total = float(sum(weights))
    widths = [max(width * weight / total, 14 * mm) for weight in weights]
    scale = width / sum(widths)
    widths = [value * scale for value in widths]

    def cell(text: str, column: int, style_name: str) -> Any:
        style = styles[style_name].clone(f"{style_name}{column}")
        style.alignment = align[alignments[column]]
        return Paragraph(inline.convert(text), style)

    table_data = [[cell(text, column, "head") for column, text in enumerate(header)]]
    table_data.extend(
        [[cell(text, column, "cell") for column, text in enumerate(row)] for row in rows]
    )
    table = Table(table_data, colWidths=widths, repeatRows=1, splitByRow=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                # The cell font otherwise defaults to Helvetica, listed among the fonts.
                ("FONTNAME", (0, 0), (-1, -1), styles["body"].fontName),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(SURFACE)),
                ("LINEABOVE", (0, 0), (-1, 0), 1.2, colors.HexColor(accent)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor(RULE)),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor(RULE)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.8 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8 * mm),
            ]
        )
    )
    return table


def _callout_box(
    data: tuple[str, str, list[str]], inline: Inline, styles: Styles, width: float
) -> Any:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import Paragraph, Table, TableStyle  # type: ignore[import-untyped]

    kind, title, lines = data
    color = CALLOUTS.get(kind, MUTED)
    content: list[Any] = []
    if title:
        style = styles["body"].clone("CalloutTitle")
        style.fontName, style.textColor, style.spaceAfter = (
            styles[3].fontName,
            colors.HexColor(color),
            1 * mm,
        )
        content.append(Paragraph(inline.convert(title), style))
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    last_style = styles["body"].clone("CalloutBody")
    last_style.spaceAfter = 1 * mm
    if kind == "QUOTE":
        last_style.textColor = colors.HexColor(MUTED)
    for paragraph in paragraphs:
        if paragraph:
            content.append(Paragraph(inline.lines(paragraph), last_style))
    box = Table([[content]], colWidths=[width])
    box.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), styles["body"].fontName),
                ("LINEBEFORE", (0, 0), (0, -1), 2.4, colors.HexColor(color)),
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, -1),
                    colors.HexColor(SURFACE) if kind != "QUOTE" else colors.white,
                ),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.6 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8 * mm),
            ]
        )
    )
    return box


def _image(
    data: tuple[str, str], inline: Inline, styles: Styles, width: float, base: Path
) -> list[Any]:
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import Image, KeepTogether, Paragraph  # type: ignore[import-untyped]

    reference, caption = data
    path = (base / reference).resolve() if not Path(reference).is_absolute() else Path(reference)
    if not path.is_file():
        raise CommandError(f"Image not found: {path} (paths are relative to the Markdown file).")
    image = Image(str(path))
    scale = min(width / image.imageWidth, 120 * mm / image.imageHeight, 1.0)
    image.drawWidth, image.drawHeight = image.imageWidth * scale, image.imageHeight * scale
    image.hAlign = "LEFT"
    parts: list[Any] = [image]
    if caption:
        parts.append(Paragraph(inline.convert(caption), styles["caption"]))
    return [KeepTogether(parts)]


def _write(
    target: Path,
    story: list[Any],
    page_size: Any,
    meta: dict[str, str],
    title: str,
    language: str,
    family: Family,
    top_level: int,
) -> None:
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
    from reportlab.platypus import Frame, PageTemplate  # type: ignore[import-untyped]
    from reportlab.platypus.doctemplate import BaseDocTemplate  # type: ignore[import-untyped]

    label = PAGE_LABELS.get(language, "{page} / {total}")
    running_title = re.sub(r"[*_`]", "", title)[:80]

    class NumberedCanvas(Canvas):  # type: ignore[misc]
        def __init__(self, *arguments: Any, **keywords: Any) -> None:
            Canvas.__init__(self, *arguments, **keywords)
            self._pages: list[dict[str, Any]] = []

        def showPage(self) -> None:  # noqa: N802 - ReportLab API
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._decorate(total)
                Canvas.showPage(self)
            Canvas.save(self)

        def _decorate(self, total: int) -> None:
            width, height = self._pagesize
            self.saveState()
            self.setFont(family.regular, 8)
            self.setFillColor(colors.HexColor(MUTED))
            page = self._pageNumber
            self.drawRightString(width - 22 * mm, 11 * mm, label.format(page=page, total=total))
            if page > 1 and running_title:
                self.drawString(22 * mm, height - 12 * mm, running_title)
                self.setStrokeColor(colors.HexColor(RULE))
                self.setLineWidth(0.4)
                self.line(22 * mm, height - 14 * mm, width - 22 * mm, height - 14 * mm)
            self.restoreState()

    class Document(BaseDocTemplate):  # type: ignore[misc]
        outline_level = -1

        def beforeDocument(self) -> None:  # noqa: N802 - ReportLab API
            self.outline_level = -1

        def afterFlowable(self, flowable: Any) -> None:  # noqa: N802 - ReportLab API
            mark = getattr(flowable, "_bookmark", None)
            if not mark:
                return
            heading, text = mark
            # Bookmark levels may not skip: a document starting at ### still nests correctly.
            level = max(0, min(heading - top_level, self.outline_level + 1))
            self.outline_level = level
            key = f"heading-{id(flowable)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            if level <= 1:
                self.notify("TOCEntry", (level, text, self.page, key))

    with AtomicOutput(target) as output:
        document = Document(
            str(output.path),
            pagesize=page_size,
            leftMargin=22 * mm,
            rightMargin=22 * mm,
            topMargin=22 * mm,
            bottomMargin=20 * mm,
            title=running_title,
            author=meta.get("author", ""),
            subject=meta.get("subtitle", ""),
            lang=meta.get("lang", ""),
            # ReportLab starts every page in Helvetica, which then shows among the fonts.
            initialFontName=family.regular,
        )
        frame = Frame(
            document.leftMargin, document.bottomMargin, document.width, document.height, id="body"
        )
        document.addPageTemplates([PageTemplate(id="page", frames=[frame])])
        document.multiBuild(story, canvasmaker=NumberedCanvas)
