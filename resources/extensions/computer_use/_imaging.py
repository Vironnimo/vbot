"""Pure image sizing and masking, also used by the native Desktop."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from PIL import Image, ImageDraw

from .target import WindowInfo

MAX_LONG_EDGE = 1568
MAX_AREA = 1_150_000
MASK_FILL = (128, 128, 128)
type Area = tuple[int, int, int, int]


def fit(width: int, height: int) -> tuple[int, int]:
    """Return *width* x *height* scaled down to the image limits, never up."""
    factor = min(1.0, MAX_LONG_EDGE / max(width, height), math.sqrt(MAX_AREA / (width * height)))
    if factor >= 1.0:
        return width, height
    return max(1, math.floor(width * factor)), max(1, math.floor(height * factor))


def mask(
    image: Image.Image,
    area: Area,
    windows: Sequence[WindowInfo],
    visible: Callable[[WindowInfo], bool],
) -> tuple[Image.Image, list[str]]:
    """Hide every window of *area* that is not *visible*; return the image and hidden apps.

    *image* shows the physical *area* at any size. Windows paint bottom to top:
    a visible window reveals its frame, any other window covers it with a solid
    fill, and pixels no window covers stay hidden. Edges round toward hiding.
    The names are the apps that still have a hidden part, topmost first.
    """
    left, top, right, bottom = area
    scale_x = image.width / (right - left)
    scale_y = image.height / (bottom - top)
    shown = Image.new("L", image.size, 0)
    owners = Image.new("I", image.size, 0)
    draw_shown = ImageDraw.Draw(shown)
    draw_owners = ImageDraw.Draw(owners)
    hidden: dict[int, str] = {}
    for index in range(len(windows) - 1, -1, -1):
        window = windows[index]
        x0, y0 = max(window.left, left), max(window.top, top)
        x1, y1 = min(window.right, right), min(window.bottom, bottom)
        if x1 <= x0 or y1 <= y0:
            continue
        reveal = visible(window)
        inward, outward = (math.ceil, math.floor) if reveal else (math.floor, math.ceil)
        box = (
            inward((x0 - left) * scale_x),
            inward((y0 - top) * scale_y),
            outward((x1 - left) * scale_x),
            outward((y1 - top) * scale_y),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        corners = [box[0], box[1], box[2] - 1, box[3] - 1]
        draw_shown.rectangle(corners, fill=255 if reveal else 0)
        draw_owners.rectangle(corners, fill=index + 1)
        if not reveal:
            hidden[index + 1] = window.app.name
    present = {value for _, value in owners.getcolors(len(windows) + 1) or []}
    names = [hidden[label] for label in sorted(hidden) if label in present]
    covered = Image.composite(image.convert("RGB"), Image.new("RGB", image.size, MASK_FILL), shown)
    return covered, list(dict.fromkeys(names))


def resized(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    if image.size == size:
        return image
    return image.resize(size, Image.Resampling.LANCZOS)
