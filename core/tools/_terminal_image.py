"""PNG images of a Terminal screen, drawn the way a terminal shows it.

A Terminal renderer worker draws them next to its ``TerminalEmulator``
(``_terminal_render_host.py``), so the module imports only the standard
library: Pillow cannot load in a subinterpreter. Characters come from a
bundled subset of GNU Unifont (``resources/licenses/unifont.txt``): its 8x16
glyphs fill whole cells, so box-drawing and block characters join across
cells as they do in a terminal. Colors follow the WebUI's dark terminal
theme (``webui/src/components/terminals/terminalRenderer.svelte.js``);
256-color and truecolor values are drawn exactly.
"""

from __future__ import annotations

import gzip
import struct
import unicodedata
import zlib
from collections.abc import Callable, Sequence
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

CELL_WIDTH = 8
CELL_HEIGHT = 16
_FONT_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "fonts" / "unifont-17.0.05-subset.hex.gz"
)

# The WebUI's dark terminal theme, by pyte's color names ("brown" is yellow).
_DEFAULT_FOREGROUND = 0xF1EDE6
_DEFAULT_BACKGROUND = 0x0E0D0B
_CURSOR = 0xE8870A
_NAMED_COLORS = {
    "black": 0x0E0D0B,
    "red": 0xF28B82,
    "green": 0x5FCF8F,
    "brown": 0xE5B53E,
    "blue": 0x74ADF2,
    "magenta": 0xD8A4E2,
    "cyan": 0x67D4C1,
    "white": 0xF1EDE6,
    "brightblack": 0x9D9387,
    "brightred": 0xFFA0A0,
    "brightgreen": 0x7AE7A4,
    "brightbrown": 0xFBC45B,
    "brightblue": 0x8EC0FF,
    "brightmagenta": 0xE7BDEE,
    "brightcyan": 0x8DE1D2,
    "brightwhite": 0xFFF9F0,
}
# Bold text in one of the eight basic colors is drawn in its bright variant,
# as the WebUI's terminal does.
_BOLD_BRIGHT = frozenset(("black", "red", "green", "brown", "blue", "magenta", "cyan", "white"))

# Pixel rows of a cell: the underline sits just below the baseline, the
# strikethrough across the middle of lowercase letters.
_UNDERLINE_ROW = 14
_STRIKETHROUGH_ROW = 8
# Italics slant a glyph by shifting its upper rows right.
_ITALIC_SHIFT = tuple((CELL_HEIGHT - 1 - row) // 6 for row in range(CELL_HEIGHT))
# Variation selectors choose a glyph's presentation; they draw nothing.
_VARIATION_SELECTORS = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
# Up to this many colors an image is stored with a palette.
_PALETTE_COLORS = 256
_COMPRESSION_LEVEL = 6


def paint_screen(
    rows: Sequence[Sequence[tuple[int, Any, str]]],
    *,
    columns: int,
    cursor: tuple[int, int] | None,
    reverse_screen: bool,
) -> bytes:
    """Draw a screen as a PNG image of ``CELL_WIDTH`` x ``CELL_HEIGHT`` pixels per cell.

    *rows* lists each screen row's visible cells as (column, cell, text) in
    column order from column 0, the second cell of a wide character left out
    (``terminal_emulator._visible_cells``): a cell spans the columns up to the
    next one. A cell carries pyte's attributes (``fg``, ``bg``, ``bold``,
    ``italics``, ``underscore``, ``strikethrough``, ``reverse``) plus ``dim``
    and ``conceal``. *cursor* is the visible cursor's (row, column), drawn as
    an outline that leaves the character under it readable. With
    *reverse_screen* (DECSCNM) the default colors are swapped.
    """
    foreground, background = _DEFAULT_FOREGROUND, _DEFAULT_BACKGROUND
    if reverse_screen:
        foreground, background = background, foreground
    # One entry per cell: (span, glyph key, foreground, background, cursor).
    layout: list[list[tuple[int, tuple[Any, ...], int, int, bool]]] = []
    colors = {background} if cursor is None else {background, _CURSOR}
    for row_index, cells in enumerate(rows):
        painted: list[tuple[int, tuple[Any, ...], int, int, bool]] = []
        for index, (column, cell, text) in enumerate(cells):
            end = cells[index + 1][0] if index + 1 < len(cells) else columns
            span = max(1, end - column)
            fg, bg = _cell_colors(cell, foreground, background)
            under_cursor = (
                cursor is not None and cursor[0] == row_index and column <= cursor[1] < end
            )
            colors.update((fg, bg))
            painted.append((span, _glyph_key(cell, text), fg, bg, under_cursor))
        layout.append(painted)
    palette = sorted(colors) if len(colors) <= _PALETTE_COLORS else None
    pixel: Callable[[int], bytes] = _rgb
    if palette is not None:
        pixel = {color: bytes((index,)) for index, color in enumerate(palette)}.__getitem__
    cursor_pixel = pixel(_CURSOR) if cursor is not None else b""
    tiles: dict[tuple[Any, ...], tuple[bytes, ...]] = {}
    scanlines: list[bytes] = []
    for painted in layout:
        row_tiles = []
        for span, glyph, fg, bg, under_cursor in painted:
            key = (span, glyph, fg, bg, under_cursor)
            tile = tiles.get(key)
            if tile is None:
                outline = cursor_pixel if under_cursor else b""
                tile = _tile(span, glyph, pixel(fg), pixel(bg), outline)
                tiles[key] = tile
            row_tiles.append(tile)
        for pixel_row in range(CELL_HEIGHT):
            # Each scanline starts with its filter type: none.
            scanlines.append(b"\x00" + b"".join(tile[pixel_row] for tile in row_tiles))
    return _png(columns * CELL_WIDTH, len(layout) * CELL_HEIGHT, b"".join(scanlines), palette)


def _cell_colors(cell: Any, foreground: int, background: int) -> tuple[int, int]:
    """The colors a cell's character and background are drawn in."""
    fg_name = cell.fg
    if cell.bold and fg_name in _BOLD_BRIGHT:
        fg_name = "bright" + fg_name
    fg = _color(fg_name, foreground)
    bg = _color(cell.bg, background)
    if cell.reverse:
        fg, bg = bg, fg
    if getattr(cell, "dim", False):
        fg = _blend(fg, bg)
    return fg, bg


def _color(value: Any, default: int) -> int:
    """pyte's color value as 0xRRGGBB: a name, six hex digits, or ``default``."""
    if not isinstance(value, str):
        return default
    named = _NAMED_COLORS.get(value)
    if named is not None:
        return named
    if len(value) == 6:
        try:
            return int(value, 16)
        except ValueError:
            return default
    return default


def _blend(color: int, other: int) -> int:
    """The color halfway between two: faint text over its background."""
    return ((color & 0xFEFEFE) >> 1) + ((other & 0xFEFEFE) >> 1)


def _rgb(color: int) -> bytes:
    return color.to_bytes(3, "big")


def _glyph_key(cell: Any, text: str) -> tuple[Any, ...]:
    """What decides a cell's pixels besides its colors and width."""
    if getattr(cell, "conceal", False):
        return ("", False, False, False, False)
    return (
        text,
        bool(cell.bold),
        bool(cell.italics),
        bool(cell.underscore),
        bool(cell.strikethrough),
    )


def _tile(
    span: int,
    glyph: tuple[Any, ...],
    fg: bytes,
    bg: bytes,
    outline: bytes,
) -> tuple[bytes, ...]:
    """A cell's pixel rows: its character and styles, framed in the cursor's
    *outline* color when the cursor is on it."""
    text, bold, italics, underline, strikethrough = glyph
    pixels = span * CELL_WIDTH
    masks = list(_glyph_masks(text, span))
    if not _keeps_shape(text):
        if italics:
            masks = [mask >> _ITALIC_SHIFT[row] for row, mask in enumerate(masks)]
        if bold:
            masks = [mask | mask >> 1 for mask in masks]
    full = (1 << pixels) - 1
    if underline:
        masks[_UNDERLINE_ROW] = full
    if strikethrough:
        masks[_STRIKETHROUGH_ROW] = full
    rows = [_pixel_row(mask, pixels, fg, bg) for mask in masks]
    if outline:
        step = len(outline)
        rows[0] = rows[-1] = outline * pixels
        rows[1:-1] = [outline + row[step:-step] + outline for row in rows[1:-1]]
    return tuple(rows)


@lru_cache(maxsize=4096)
def _pixel_row(mask: int, pixels: int, fg: bytes, bg: bytes) -> bytes:
    """One pixel row: *fg* where *mask* has a bit set, the leftmost pixel highest."""
    if not mask:
        return bg * pixels
    return b"".join(fg if mask >> shift & 1 else bg for shift in range(pixels - 1, -1, -1))


@lru_cache(maxsize=4096)
def _glyph_masks(text: str, span: int) -> tuple[int, ...]:
    """The pixel rows of *text* in a cell *span* columns wide, one bit per pixel.

    The first character is drawn; combining marks after it are drawn over it,
    other zero-width characters (joiners, variation selectors) are not.
    A glyph twice the cell's width is squeezed into it; a narrower one is
    centred. A character the font lacks is drawn as an empty box.
    """
    pixels = span * CELL_WIDTH
    masks = [0] * CELL_HEIGHT
    if not text or text == " ":
        return tuple(masks)
    glyphs = _font()
    for index, character in enumerate(text):
        if index and not _draws_over(character):
            continue
        bitmap = glyphs.get(ord(character))
        if bitmap is None:
            if index == 0:
                masks = _missing_glyph(pixels)
            continue
        masks = [mask | row for mask, row in zip(masks, _fit(bitmap, pixels), strict=True)]
    return tuple(masks)


def _fit(bitmap: bytes, pixels: int) -> list[int]:
    """A font bitmap's rows fitted into a cell *pixels* wide."""
    step = len(bitmap) // CELL_HEIGHT
    rows = [int.from_bytes(bitmap[row * step : (row + 1) * step]) for row in range(CELL_HEIGHT)]
    glyph_pixels = step * 8
    if glyph_pixels == pixels:
        return rows
    if glyph_pixels < pixels:
        return [row << (pixels - glyph_pixels) // 2 for row in rows]
    return [_squeeze(row, glyph_pixels, pixels) for row in rows]


def _squeeze(row: int, glyph_pixels: int, pixels: int) -> int:
    """Narrow a pixel row: each cell pixel is set when any glyph pixel it covers is."""
    ratio = glyph_pixels // pixels
    covered = (1 << ratio) - 1
    squeezed = 0
    for bit in range(pixels):
        if row >> (bit * ratio) & covered:
            squeezed |= 1 << bit
    return squeezed


def _missing_glyph(pixels: int) -> list[int]:
    edge = ((1 << (pixels - 2)) - 1) << 1
    sides = 1 << (pixels - 2) | 2
    return [0, edge, *([sides] * (CELL_HEIGHT - 4)), edge, 0]


def _draws_over(character: str) -> bool:
    """A zero-width character drawn over the one before it: a combining mark."""
    code = ord(character)
    if any(low <= code <= high for low, high in _VARIATION_SELECTORS):
        return False
    return unicodedata.category(character) in ("Mn", "Me")


def _keeps_shape(text: str) -> bool:
    """Box-drawing, block and legacy computing characters join their neighbours:
    bold and italics leave them unchanged."""
    code = ord(text[0]) if text else 0
    return 0x2500 <= code <= 0x259F or 0x1FB00 <= code <= 0x1FBFF


@cache
def _font() -> dict[int, bytes]:
    """The bundled Unifont glyphs by code point, read once per worker."""
    glyphs: dict[int, bytes] = {}
    for line in gzip.decompress(_FONT_PATH.read_bytes()).decode("ascii").splitlines():
        code, _separator, bitmap = line.partition(":")
        if bitmap:
            glyphs[int(code, 16)] = bytes.fromhex(bitmap)
    return glyphs


def _png(width: int, height: int, scanlines: bytes, palette: list[int] | None) -> bytes:
    """Encode filtered scanlines (8-bit palette indices or RGB) as a PNG file."""
    color_type = 3 if palette is not None else 2
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    chunks = [_chunk(b"IHDR", header)]
    if palette is not None:
        chunks.append(_chunk(b"PLTE", b"".join(_rgb(color) for color in palette)))
    chunks.append(_chunk(b"IDAT", zlib.compress(scanlines, _COMPRESSION_LEVEL)))
    chunks.append(_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


__all__ = ["CELL_HEIGHT", "CELL_WIDTH", "paint_screen"]
