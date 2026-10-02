"""Page operations, stamps, compression and passwords for existing PDFs."""

from __future__ import annotations

import contextlib
import io
import math
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .common import (
    AtomicOutput,
    CommandError,
    describe_pages,
    existing_pdf,
    human_size,
    open_reader,
    output_path,
    parse_pages,
    require_modules,
)

MM = 72 / 25.4


def _writer_from(reader: Any) -> Any:
    from pypdf import PdfWriter

    return PdfWriter(clone_from=reader)


def _save(writer: Any, target: Path) -> None:
    with AtomicOutput(target) as output, open(output.path, "wb") as handle:
        writer.write(handle)


def _written(target: Path, page_count: int, what: str) -> list[str]:
    return [
        f"{what} -> {target.name}: {page_count} page{'s' if page_count != 1 else ''}, "
        f"{human_size(target.stat().st_size)}.",
        f"File: {target}",
    ]


def merge(inputs: Sequence[str], target_value: str | None, bookmarks: bool) -> list[str]:
    require_modules("pypdf")
    from pypdf import PdfWriter

    values = list(inputs)
    if target_value is None:
        if len(values) < 3 or Path(values[-1]).exists():
            raise CommandError(
                "Name the output with -o, for example: merge a.pdf b.pdf -o combined.pdf"
            )
        target_value = values.pop()
    sources = [existing_pdf(value) for value in values]
    if len(sources) < 2:
        raise CommandError("merge needs at least two input PDFs.")
    target = output_path(target_value, inputs=sources)
    writer = PdfWriter()
    for source in sources:
        reader = open_reader(source)
        writer.append(reader, outline_item=source.stem if bookmarks else None)
    _save(writer, target)
    return _written(target, len(writer.pages), f"Merged {len(sources)} files")


def select(
    value: str,
    target_value: str,
    pages: str | None,
    rotations: Sequence[str],
    password: str | None,
) -> list[str]:
    require_modules("pypdf")
    from pypdf import PdfWriter

    source = existing_pdf(value)
    target = output_path(target_value, inputs=[source])
    reader = open_reader(source, password)
    indexes = parse_pages(pages, len(reader.pages))
    turns = _rotations(rotations, len(reader.pages))
    writer = PdfWriter(clone_from=reader)
    keep = set(indexes)
    order = list(indexes)
    for index, angle in turns.items():
        writer.pages[index].rotate(angle)
    if order != list(range(len(reader.pages))):
        # Rebuild in the requested order; duplicates of a page are allowed.
        reordered = PdfWriter()
        for index in order:
            reordered.add_page(writer.pages[index])
        _copy_document_info(reader, reordered)
        writer = reordered
    _save(writer, target)
    removed = len(reader.pages) - len(keep)
    detail = f"Kept pages {describe_pages(order)}"
    if removed:
        detail += f", removed {removed}"
    rotated = [position for position, index in enumerate(order) if index in turns]
    if rotated:
        detail += f", rotated output pages {describe_pages(rotated)}"
    return _written(target, len(order), detail)


def _rotations(values: Sequence[str], page_count: int) -> dict[int, int]:
    result: dict[int, int] = {}
    for value in values:
        pages_part, _, angle_part = value.rpartition(":")
        if not pages_part:
            pages_part, angle_part = "all", value
        try:
            angle = int(angle_part)
        except ValueError:
            angle = 1
        if angle % 90:
            raise CommandError(
                f"Invalid --rotate '{value}'. Use PAGES:DEGREES with a multiple of 90, "
                "for example --rotate 2:90 or --rotate 3-5:180; a bare angle rotates all pages."
            )
        for index in parse_pages(pages_part, page_count):
            result[index] = (result.get(index, 0) + angle) % 360
    return {index: angle for index, angle in result.items() if angle}


def _copy_document_info(reader: Any, writer: Any) -> None:
    if reader.metadata:
        writer.add_metadata({key: str(value) for key, value in reader.metadata.items()})


def split(
    value: str, folder_value: str, ranges: str | None, every: int, password: str | None
) -> list[str]:
    require_modules("pypdf")
    from pypdf import PdfWriter

    source = existing_pdf(value)
    folder = Path(folder_value).expanduser().resolve()
    if folder.suffix.lower() == ".pdf":
        raise CommandError("split writes several files: give a folder, not a .pdf file.")
    folder.mkdir(parents=True, exist_ok=True)
    reader = open_reader(source, password)
    count = len(reader.pages)
    groups: list[list[int]] = []
    if ranges:
        for part in ranges.split(","):
            groups.append(parse_pages(part, count))
    else:
        size = max(1, every)
        groups = [list(range(start, min(start + size, count))) for start in range(0, count, size)]
    width = len(str(count))
    written: list[str] = []
    for group in groups:
        writer = PdfWriter()
        for index in group:
            writer.add_page(reader.pages[index])
        _copy_document_info(reader, writer)
        first, last = group[0] + 1, group[-1] + 1
        label = f"{first:0{width}d}" if first == last else f"{first:0{width}d}-{last:0{width}d}"
        target = folder / f"{source.stem}-p{label}.pdf"
        _save(writer, target)
        written.append(target.name)
    return [
        f"Split {source.name} into {len(written)} files in {folder.as_posix()}.",
        "Files: " + ", ".join(written),
    ]


# Stamps


def stamp(
    value: str,
    target_value: str,
    text: str | None,
    overlay: str | None,
    pages: str | None,
    position: str,
    size: float,
    angle: float | None,
    opacity: float,
    color: str,
    under: bool,
    password: str | None,
) -> list[str]:
    require_modules("pypdf")
    if bool(text) == bool(overlay):
        raise CommandError("Give exactly one of --text or --overlay.")
    source = existing_pdf(value)
    target = output_path(target_value, inputs=[source])
    reader = open_reader(source, password)
    writer = _writer_from(reader)
    indexes = parse_pages(pages, len(writer.pages))
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise CommandError(f"--color must be a hex color such as #C00000, not '{color}'.")
    if not 0 < opacity <= 1:
        raise CommandError("--opacity must be between 0.05 and 1, for example 0.15.")
    overlay_pages: list[Any] = []
    if overlay:
        overlay_path = existing_pdf(overlay)
        overlay_pages = list(open_reader(overlay_path).pages)
        if len(overlay_pages) not in (1, len(writer.pages)):
            raise CommandError(
                f"{overlay_path.name} has {len(overlay_pages)} pages: give an overlay with one "
                "page "
                f"for all stamped pages, or with exactly {len(writer.pages)} pages, one per page."
            )
    for index in indexes:
        page = writer.pages[index]
        if int(page.get("/Rotate", 0) or 0) % 360:
            page.transfer_rotation_to_content()
        box = page.mediabox
        width, height = float(box.width), float(box.height)
        if text:
            layer = _text_layer(text, width, height, position, size, angle, opacity, color)
        else:
            layer = (
                overlay_pages[index]
                if len(overlay_pages) == len(writer.pages)
                else overlay_pages[0]
            )
        _merge_layer(page, layer, width, height, float(box.left), float(box.bottom), under)
    _save(writer, target)
    what = f"Stamped '{text}'" if text else f"Overlaid {Path(overlay or '').name}"
    return _written(target, len(writer.pages), f"{what} on pages {describe_pages(indexes)}")


def _merge_layer(
    page: Any, layer: Any, width: float, height: float, left: float, bottom: float, under: bool
) -> None:
    from pypdf import Transformation

    layer_box = layer.mediabox
    layer_width, layer_height = float(layer_box.width), float(layer_box.height)
    scale = min(width / layer_width, height / layer_height)
    offset_x = left + (width - layer_width * scale) / 2 - float(layer_box.left) * scale
    offset_y = bottom + (height - layer_height * scale) / 2 - float(layer_box.bottom) * scale
    transformation = Transformation().scale(scale, scale).translate(offset_x, offset_y)
    page.merge_transformed_page(layer, transformation, over=not under)


def _text_layer(
    text: str,
    width: float,
    height: float,
    position: str,
    size: float,
    angle: float | None,
    opacity: float,
    color: str,
) -> Any:
    require_modules("reportlab")
    from pypdf import PdfReader
    from reportlab.lib.colors import HexColor  # type: ignore[import-untyped]
    from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]
    from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

    from .fonts import register_family

    family = register_family()
    font = family.bold
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(width, height))
    canvas.setFillColor(HexColor(color), alpha=opacity)
    canvas.setFont(font, size)
    text_width = stringWidth(text, font, size)
    margin = 14 * MM
    if position == "center":
        # Without an explicit angle the text runs along the page diagonal.
        turn = math.degrees(math.atan2(height, width)) if angle is None else angle
        canvas.translate(width / 2, height / 2)
        canvas.rotate(turn)
        canvas.drawCentredString(0, -size * 0.35, text)
    else:
        vertical = height - margin - size * 0.8 if position.startswith("top") else margin
        if position.endswith("left"):
            canvas.drawString(margin, vertical, text)
        elif position.endswith("right"):
            canvas.drawString(width - margin - text_width, vertical, text)
        else:
            canvas.drawCentredString(width / 2, vertical, text)
    canvas.save()
    family.report_missing(text)
    return PdfReader(io.BytesIO(buffer.getvalue())).pages[0]


# Compression


def compress(
    value: str, target_value: str, quality: int, max_pixels: int, password: str | None
) -> list[str]:
    require_modules("pypdf", "PIL")
    source = existing_pdf(value)
    target = output_path(target_value, inputs=[source])
    reader = open_reader(source, password)
    writer = _writer_from(reader)
    recompressed = 0
    for page in writer.pages:
        for image in _images(page):
            if _recompress(image, quality, max_pixels):
                recompressed += 1
        with contextlib.suppress(Exception):
            page.compress_content_streams()
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    _save(writer, target)
    before, after = source.stat().st_size, target.stat().st_size
    if after >= before:
        shutil.copyfile(source, target)
        return [
            f"No reduction possible: {source.name} ({human_size(before)}) is already compact; "
            f"{target.name} is an unchanged copy. Lower --quality or --max-pixels to shrink "
            "images further."
        ]
    saved = 100 - after * 100 // before
    return [
        f"Compressed {source.name}: {human_size(before)} -> {human_size(after)} ({saved}% smaller, "
        f"{recompressed} image{'s' if recompressed != 1 else ''} re-encoded at JPEG quality "
        f"{quality}).",
        f"File: {target}",
    ]


def _images(page: Any) -> list[Any]:
    try:
        return list(page.images)
    except Exception:
        return []


def _recompress(image_file: Any, quality: int, max_pixels: int) -> bool:
    try:
        stream = image_file.indirect_reference.get_object()
        if "/SMask" in stream or "/Mask" in stream or "/ImageMask" in stream:
            return False
        picture = image_file.image
        if picture is None or picture.mode not in ("RGB", "L", "CMYK"):
            return False
        # Cloned streams carry no /Length; pypdf keeps the encoded bytes in _data.
        encoded = getattr(stream, "_data", None)
        original = len(encoded) if isinstance(encoded, bytes) else len(stream.get_data())
        if max(picture.size) > max_pixels:
            picture = picture.copy()
            picture.thumbnail((max_pixels, max_pixels))
        if picture.mode == "CMYK":
            picture = picture.convert("RGB")
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() >= original * 0.9:
            return False
        image_file.replace(picture, quality=quality)
        return True
    except Exception:
        return False


# Passwords


def encrypt(value: str, target_value: str, password: str, owner_password: str | None) -> list[str]:
    require_modules("pypdf", "cryptography")
    source = existing_pdf(value)
    target = output_path(target_value, inputs=[source])
    reader = open_reader(source)
    writer = _writer_from(reader)
    writer.encrypt(password, owner_password or password, algorithm="AES-256")
    _save(writer, target)
    return _written(target, len(writer.pages), "Encrypted with AES-256")


def decrypt(value: str, target_value: str, password: str) -> list[str]:
    require_modules("pypdf", "cryptography")
    source = existing_pdf(value)
    target = output_path(target_value, inputs=[source])
    reader = open_reader(source, password)
    writer = _writer_from(reader)
    _save(writer, target)
    return _written(target, len(writer.pages), "Removed the password")
