"""Inspect a PDF for structure and visible defects, and render its pages to PNG."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .common import (
    CommandError,
    describe_pages,
    existing_pdf,
    human_size,
    open_reader,
    paper_name,
    parse_pages,
    require_modules,
    tool_command,
)

STANDARD_FONTS = {
    "Courier",
    "Courier-Bold",
    "Courier-BoldOblique",
    "Courier-Oblique",
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-BoldOblique",
    "Helvetica-Oblique",
    "Symbol",
    "Times-Bold",
    "Times-BoldItalic",
    "Times-Italic",
    "Times-Roman",
    "ZapfDingbats",
}
TEXT_OPERATORS = (b"Tj", b"TJ", b"'", b'"')
MAX_GLYPH_CHECK_PAGES = 300
DEFAULT_LONG_EDGE = 1600
SHEET_COLUMNS = 4
SHEET_ROWS = 3
PLACEHOLDER = re.compile(r"\{\{[^{}\n]{1,80}\}\}")


class Report:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.page_count = 0
        self.page_sizes: list[str] = []
        self.fonts: list[str] = []
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.missing_glyph_pages: dict[int, int] = {}
        self.textless_pages: list[int] = []
        self.blank_pages: list[int] = []
        self.placeholders: dict[int, list[str]] = {}
        self.title = ""
        self.language = ""

    def size_summary(self) -> str:
        if not self.page_sizes:
            return "no pages"
        groups: dict[str, list[int]] = {}
        for index, size in enumerate(self.page_sizes):
            groups.setdefault(size, []).append(index)
        if len(groups) == 1:
            return self.page_sizes[0]
        return "; ".join(
            f"{size} (page{'s' if len(pages) != 1 else ''} {describe_pages(pages)})"
            for size, pages in groups.items()
        )


def analyse(path: Path, *, check_blank: bool, password: str | None = None) -> Report:
    reader = open_reader(path, password)
    report = Report(path)
    pages = reader.pages
    report.page_count = len(pages)
    if not report.page_count:
        report.problems.append("The PDF has no pages.")
        return report
    fonts: dict[str, bool] = {}
    for index, page in enumerate(pages):
        box = page.cropbox
        width, height = float(box.width), float(box.height)
        if int(page.get("/Rotate", 0) or 0) % 180:
            width, height = height, width
        report.page_sizes.append(paper_name(width, height))
        scanner = _PageScanner(reader, fonts, check_glyphs=index < MAX_GLYPH_CHECK_PAGES)
        scanner.scan(page)
        if scanner.missing_glyphs:
            report.missing_glyph_pages[index] = scanner.missing_glyphs
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if not text.strip() and scanner.has_images:
            report.textless_pages.append(index)
        found = PLACEHOLDER.findall(text)
        if found:
            report.placeholders[index] = sorted(set(found))
    if check_blank:
        report.blank_pages = _blank_pages(path, password)
    report.fonts = sorted(fonts)
    _collect_notes(report, reader)
    _collect_problems(report, fonts)
    return report


def _collect_problems(report: Report, fonts: dict[str, bool]) -> None:
    for index, count in sorted(report.missing_glyph_pages.items()):
        report.problems.append(
            f"Page {index + 1}: {count} character{'s' if count != 1 else ''} print as empty "
            "boxes because no font used there has a glyph for them."
        )
    if report.page_count > MAX_GLYPH_CHECK_PAGES:
        report.notes.append(f"Missing-glyph check covered pages 1-{MAX_GLYPH_CHECK_PAGES}.")
    for index, found in sorted(report.placeholders.items()):
        shown = ", ".join(found[:6]) + (f" and {len(found) - 6} more" if len(found) > 6 else "")
        report.problems.append(f"Page {index + 1}: unfilled template placeholders {shown}.")
    metadata = [value for value in (report.title, report.language) if PLACEHOLDER.search(value)]
    if metadata:
        report.problems.append(
            f"Unfilled placeholders in the document title or language: {', '.join(metadata)}."
        )
    if report.blank_pages:
        report.problems.append(f"Blank pages: {describe_pages(report.blank_pages)}.")
    textless = [page for page in report.textless_pages if page not in report.blank_pages]
    if textless:
        report.problems.append(
            f"No text layer on pages {describe_pages(textless)} (scanned or image-only): text "
            "search and copying do not work there. To read them, render these pages and read "
            "the images."
        )
    unembedded = sorted(
        name for name, embedded in fonts.items() if not embedded and name not in STANDARD_FONTS
    )
    if unembedded:
        report.problems.append(
            f"Fonts not embedded: {', '.join(unembedded)}. Other computers show a substitute font."
        )


def _collect_notes(report: Report, reader: Any) -> None:
    root = reader.trailer["/Root"]
    metadata = reader.metadata
    report.title = str(metadata.title) if metadata and metadata.title else ""
    report.language = str(root.get("/Lang", "") or "")
    title = report.title or "none"
    author = str(metadata.author) if metadata and metadata.author else "none"
    language = report.language or "none"
    bookmarks = _count_outline(reader.outline)
    links = 0
    for page in reader.pages:
        for annotation in page.get("/Annots", []) or []:
            try:
                if annotation.get_object().get("/Subtype") == "/Link":
                    links += 1
            except Exception:
                continue
    report.notes.insert(
        0,
        f"Title: {title} | Author: {author} | Language: {language} | Bookmarks: {bookmarks} "
        f"| Links: {links}",
    )
    fields = reader.get_fields() or {}
    acroform = root.get("/AcroForm")
    xfa = bool(acroform and "/XFA" in acroform.get_object())
    signed = [
        name for name, field in fields.items() if field.get("/FT") == "/Sig" and field.get("/V")
    ]
    if fields or xfa:
        form = f"Form: {len(fields)} fields" + (" with an XFA layer" if xfa else "")
        if signed:
            form += f" | Signed: {len(signed)} signature{'s' if len(signed) != 1 else ''}"
        report.notes.append(form)
    try:
        attachments = len(reader.attachments)
    except Exception:
        attachments = 0
    if attachments:
        report.notes.append(f"Attached files: {attachments}")


def _count_outline(outline: Any) -> int:
    count = 0
    for item in outline or []:
        count += _count_outline(item) if isinstance(item, list) else 1
    return count


class _PageScanner:
    """Walk a page's content streams for fonts, images and missing glyphs."""

    def __init__(self, reader: Any, fonts: dict[str, bool], *, check_glyphs: bool) -> None:
        self.reader = reader
        self.fonts = fonts
        self.check_glyphs = check_glyphs
        self.missing_glyphs = 0
        self.has_images = False
        self._seen: set[int] = set()

    def scan(self, page: Any) -> None:
        resources = _resolve(page.get("/Resources")) or {}
        contents = page.get_contents()
        if contents is not None:
            self._scan_stream(contents, resources, depth=0)
        else:
            self._register_fonts(resources)

    def _scan_stream(self, stream: Any, resources: Any, depth: int) -> None:
        from pypdf.generic import ContentStream

        font_map = self._register_fonts(resources)
        xobjects = _resolve(resources.get("/XObject")) or {}
        for xobject in xobjects.values():
            target = _resolve(xobject)
            if target is not None and target.get("/Subtype") == "/Image":
                self.has_images = True
        if not self.check_glyphs and depth == 0 and not xobjects:
            return
        try:
            content = (
                stream if isinstance(stream, ContentStream) else ContentStream(stream, self.reader)
            )
            operations = content.operations
        except Exception:
            return
        current: tuple[bool, bytes | None] | None = None
        for operands, operator in operations:
            if operator == b"Tf" and operands:
                current = font_map.get(str(operands[0]))
            elif operator in TEXT_OPERATORS and self.check_glyphs and current and current[0]:
                self.missing_glyphs += _count_notdef(operands, operator, current[1])
            elif operator == b"Do" and operands and depth < 8:
                self._scan_form(xobjects.get(str(operands[0])), resources, depth)

    def _scan_form(self, reference: Any, parent_resources: Any, depth: int) -> None:
        target = _resolve(reference)
        if target is None or target.get("/Subtype") != "/Form":
            return
        key = id(target)
        if key in self._seen:
            return
        self._seen.add(key)
        resources = _resolve(target.get("/Resources")) or parent_resources
        self._scan_stream(target, resources, depth + 1)

    def _register_fonts(self, resources: Any) -> dict[str, tuple[bool, bytes | None]]:
        """Map resource names to (checkable two-byte font, CID-to-GID map bytes)."""
        result: dict[str, tuple[bool, bytes | None]] = {}
        fonts = _resolve(resources.get("/Font")) or {}
        for name, reference in fonts.items():
            font = _resolve(reference)
            if font is None:
                continue
            base = str(font.get("/BaseFont", "")).lstrip("/")
            display = base.split("+", 1)[1] if "+" in base[:7] else base
            embedded = _embedded(font)
            if display:  # Type3 fonts often have no name; they are drawn shapes and always embedded
                self.fonts[display] = self.fonts.get(display, False) or embedded
            checkable = False
            gid_map: bytes | None = None
            if font.get("/Subtype") == "/Type0" and str(font.get("/Encoding")) in (
                "/Identity-H",
                "/Identity-V",
            ):
                descendant = _resolve((font.get("/DescendantFonts") or [None])[0])
                if descendant is not None and embedded:
                    checkable = True
                    mapping = _resolve(descendant.get("/CIDToGIDMap"))
                    if mapping is not None and hasattr(mapping, "get_data"):
                        try:
                            gid_map = mapping.get_data()
                        except Exception:
                            checkable = False
            result[str(name)] = (checkable, gid_map)
        return result


def _embedded(font: Any) -> bool:
    if font.get("/Subtype") == "/Type3":
        return True
    candidates = [font]
    descendants = font.get("/DescendantFonts")
    if descendants:
        candidates.append(_resolve(descendants[0]))
    for candidate in candidates:
        descriptor = _resolve(candidate.get("/FontDescriptor")) if candidate is not None else None
        if descriptor is not None and any(
            key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")
        ):
            return True
    return False


def _count_notdef(operands: Any, operator: bytes, gid_map: bytes | None) -> int:
    items = operands[0] if operator == b"TJ" else operands[-1:]
    count = 0
    for item in items:
        data = getattr(item, "original_bytes", None)
        if data is None and isinstance(item, bytes):
            data = bytes(item)
        if not data:
            continue
        for offset in range(0, len(data) - 1, 2):
            cid = (data[offset] << 8) | data[offset + 1]
            if gid_map is not None:
                position = cid * 2
                gid = (
                    (gid_map[position] << 8) | gid_map[position + 1]
                    if position + 1 < len(gid_map)
                    else 0
                )
            else:
                gid = cid
            if gid == 0:
                count += 1
    return count


def _resolve(value: Any) -> Any:
    try:
        return value.get_object() if value is not None and hasattr(value, "get_object") else value
    except Exception:
        return None


def _blank_pages(path: Path, password: str | None) -> list[int]:
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
    except ImportError:
        return []
    blank: list[int] = []
    try:
        document = pdfium.PdfDocument(str(path), password=password)
        document.init_forms()
    except Exception:
        return []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=0.12, grayscale=True, may_draw_forms=True)
                low, _ = bitmap.to_pil().convert("L").getextrema()
                if low >= 248:
                    blank.append(index)
            except Exception:
                pass
            finally:
                page.close()
    finally:
        document.close()
    return blank


def inspect(value: str, password: str | None) -> list[str]:
    path = existing_pdf(value)
    require_modules("pypdf")
    report = analyse(path, check_blank=True, password=password)
    lines = [
        f"{path.name}: {report.page_count} page{'s' if report.page_count != 1 else ''}, "
        f"{report.size_summary()}, {human_size(path.stat().st_size)}.",
        *report.notes,
    ]
    if report.fonts:
        lines.append("Fonts: " + ", ".join(report.fonts))
    if report.problems:
        lines.append("Problems:")
        lines.extend(f"- {problem}" for problem in report.problems)
    else:
        lines.append("No structural problems found.")
    return lines


# Rendering


def render(
    value: str,
    folder_value: str,
    pages_spec: str | None,
    long_edge: int,
    dpi: int | None,
    overview: bool,
    password: str | None,
    grid: bool = False,
    area_spec: str | None = None,
) -> list[str]:
    path = existing_pdf(value)
    area = _parse_area(area_spec) if area_spec else None
    require_modules("pypdfium2", "PIL")
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    folder = Path(folder_value).expanduser().resolve()
    if folder.suffix.lower() == ".pdf":
        raise CommandError("The second argument is the folder for the PNG files, not a PDF file.")
    folder.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    for old in list(folder.glob(f"{stem}-page-*.png")) + list(
        folder.glob(f"{stem}-overview-*.png")
    ):
        old.unlink()
    try:
        document = pdfium.PdfDocument(str(path), password=password)
        document.init_forms()  # without this, form field values are not drawn
    except Exception as error:
        raise CommandError(f"Cannot open {path.name} for rendering: {error}") from error
    rendered: list[tuple[int, Any]] = []
    try:
        indexes = parse_pages(pages_spec, len(document))
        for index in indexes:
            page = document[index]
            try:
                width, height = page.get_size()
                left, top, part_width, part_height = _area_points(area, width, height, index)
                crop = (left, height - top - part_height, width - left - part_width, top)
                scale = dpi / 72.0 if dpi else long_edge / max(part_width, part_height)
                bitmap = page.render(scale=scale, crop=crop, may_draw_forms=True)
                image = bitmap.to_pil().convert("RGB")
            finally:
                page.close()
            if grid:
                image = _draw_grid(
                    image, part_width * MM_PER_POINT, left * MM_PER_POINT, top * MM_PER_POINT
                )
            output = folder / f"{stem}-page-{index + 1:03d}.png"
            image.save(output, format="PNG", optimize=False)
            rendered.append((index, image))
    finally:
        document.close()
    pixel_size = rendered[0][1].size if rendered else (0, 0)
    numbers = [index for index, _ in rendered]
    first = f"{folder.as_posix()}/{stem}-page-{numbers[0] + 1:03d}.png"
    lines = [
        f"Rendered {len(rendered)} page{'s' if len(rendered) != 1 else ''} of {path.name} "
        f"({pixel_size[0]} x {pixel_size[1]} px for the first).",
        f"Page image: {first}"
        if len(numbers) == 1
        else f"Page images: {first} to {stem}-page-{numbers[-1] + 1:03d}.png, one file per page "
        f"({describe_pages(numbers)}).",
    ]
    if area:
        x, y, w, h = area
        lines.append(
            f"Each image shows the area from {_number(x)},{_number(y)} mm to {_number(x + w)},"
            f"{_number(y + h)} mm of its page ({_number(w)} x {_number(h)} mm)."
        )
    if grid:
        lines.append(
            "Grid lines every 5 mm, labelled every 10 mm, measured from the top-left corner "
            "of the page."
        )
    if overview and len(rendered) > 1:
        sheets = _overview_sheets(rendered, folder, stem)
        lines.append("Overview sheets with all rendered pages as thumbnails:")
        lines.extend(f"- {sheet.as_posix()}" for sheet in sheets)
    return lines


MM_PER_POINT = 25.4 / 72


def _parse_area(spec: str) -> tuple[float, float, float, float]:
    try:
        values = [float(part) for part in spec.replace(" ", "").split(",")]
    except ValueError:
        values = []
    if len(values) != 4 or values[0] < 0 or values[1] < 0 or values[2] <= 0 or values[3] <= 0:
        raise CommandError(
            f"--area {spec} is not LEFT,TOP,WIDTH,HEIGHT in millimetres, e.g. --area 20,80,60,30."
        )
    return values[0], values[1], values[2], values[3]


def _area_points(
    area: tuple[float, float, float, float] | None, width: float, height: float, index: int
) -> tuple[float, float, float, float]:
    """The rendered part of the page in points: left, top, width, height."""
    if area is None:
        return 0.0, 0.0, width, height
    left, top, part_width, part_height = (value / MM_PER_POINT for value in area)
    if left >= width or top >= height:
        raise CommandError(
            f"--area starts outside page {index + 1}, which is {_number(width * MM_PER_POINT)} x "
            f"{_number(height * MM_PER_POINT)} mm."
        )
    return left, top, min(part_width, width - left), min(part_height, height - top)


def _number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _draw_grid(image: Any, width_mm: float, left_mm: float = 0.0, top_mm: float = 0.0) -> Any:
    """Overlay a millimetre grid so positions on the page can be read off the image.

    The image shows the page from left_mm, top_mm on; lines and labels use page coordinates.
    """
    from PIL import Image, ImageDraw, ImageFont

    per_mm = image.size[0] / width_mm
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    try:
        font: Any = ImageFont.load_default(size=max(10, min(int(per_mm * 2.2), 28)))
    except TypeError:
        font = ImageFont.load_default()
    right_mm = left_mm + image.size[0] / per_mm
    bottom_mm = top_mm + image.size[1] / per_mm

    def multiples(start: float, end: float) -> range:
        return range(int(start // 5 + 1) * 5, int(end) + 1, 5)

    for mm in multiples(left_mm, right_mm):
        strong = mm % 10 == 0
        x = round((mm - left_mm) * per_mm)
        draw.line([(x, 0), (x, image.size[1])], fill=_grid_colour(strong), width=1)
        if strong:
            for y in (2, image.size[1] // 2):
                draw.text((x + 2, y), str(mm), fill=(185, 28, 28, 255), font=font)
    for mm in multiples(top_mm, bottom_mm):
        strong = mm % 10 == 0
        y = round((mm - top_mm) * per_mm)
        draw.line([(0, y), (image.size[0], y)], fill=_grid_colour(strong), width=1)
        if strong:
            for x in (2, image.size[0] // 2):
                draw.text((x, y + 1), str(mm), fill=(185, 28, 28, 255), font=font)
    return Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")


def _grid_colour(strong: bool) -> tuple[int, int, int, int]:
    return (220, 38, 38, 150) if strong else (220, 38, 38, 60)


def _overview_sheets(rendered: list[tuple[int, Any]], folder: Path, stem: str) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    cell_width = 400
    gap = 16
    label_height = 34
    tallest = max(image.size[1] / image.size[0] for _, image in rendered)
    cell_height = int(cell_width * tallest)
    try:
        font: Any = ImageFont.load_default(size=22)
    except TypeError:
        font = ImageFont.load_default()
    sheets: list[Path] = []
    for sheet_number, start in enumerate(range(0, len(rendered), per_sheet), start=1):
        chunk = rendered[start : start + per_sheet]
        rows = math.ceil(len(chunk) / SHEET_COLUMNS)
        columns = min(SHEET_COLUMNS, len(chunk))
        sheet = Image.new(
            "RGB",
            (columns * (cell_width + gap) + gap, rows * (cell_height + label_height + gap) + gap),
            (128, 128, 128),
        )
        draw = ImageDraw.Draw(sheet)
        for position, (index, image) in enumerate(chunk):
            column, row = position % SHEET_COLUMNS, position // SHEET_COLUMNS
            thumbnail = image.copy()
            thumbnail.thumbnail((cell_width, cell_height))
            x = gap + column * (cell_width + gap)
            y = gap + row * (cell_height + label_height + gap)
            draw.text((x, y + 4), f"Page {index + 1}", fill=(255, 255, 255), font=font)
            sheet.paste(thumbnail, (x, y + label_height))
        output = folder / f"{stem}-overview-{sheet_number}.png"
        sheet.save(output, format="PNG")
        sheets.append(output)
    return sheets


def next_render_hint(path: Path) -> str:
    return tool_command(f"render {path.as_posix()} <folder>")
