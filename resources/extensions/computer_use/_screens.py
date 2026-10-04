"""Platform-neutral imaging: coordinate frames, display choice, masking and images.

Each published image carries its own pixel space and physical rectangle.
Coordinates measured in screenshots and zooms are mapped here,
without making the Agent reconstruct the scaling or the rectangle's origin.
Masking hides every window whose application the Agent may not see.
"""

from __future__ import annotations

import base64
import io
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from core.storage.layout import DataDirectoryLayout
from core.tools import ToolContext
from core.utils.ids import write_id_file

from .target import AppInfo, Display, WindowInfo

MAX_LONG_EDGE = 1568
MAX_AREA = 1_150_000
MASK_FILL = (128, 128, 128)
# A zoom of a small region is enlarged by a whole factor up to this, within ZOOM_EDGE.
ZOOM_MAX_FACTOR, ZOOM_EDGE = 4, 1024
# Temporary-file category of published images (``TEMPORARY_FILE_RETENTION``).
IMAGE_CATEGORY = "computer_use"

type Area = tuple[int, int, int, int]


def fit(width: int, height: int) -> tuple[int, int]:
    """Return *width* x *height* scaled down to the image limits, never up."""
    factor = min(1.0, MAX_LONG_EDGE / max(width, height), math.sqrt(MAX_AREA / (width * height)))
    if factor >= 1.0:
        return width, height
    return max(1, math.floor(width * factor)), max(1, math.floor(height * factor))


def zoom_fit(width: int, height: int) -> tuple[int, int]:
    """Return the image size for a zoom of *width* x *height* screen pixels.

    Small regions are enlarged so that small text and targets cover more image
    pixels; larger ones are fitted like a screenshot.
    """
    factor = min(ZOOM_MAX_FACTOR, ZOOM_EDGE // max(width, height))
    if factor < 2:
        return fit(width, height)
    return width * factor, height * factor


@dataclass(frozen=True)
class Frame:
    """One image's pixels mapped to an observed physical rectangle."""

    display: Display
    width: int
    height: int
    area: Area
    screenshot_id: str = ""
    displays: tuple[Display, ...] = ()
    window: WindowInfo | None = None
    # The app whose window was in front when the image was taken.
    front: AppInfo | None = None

    @classmethod
    def of(cls, display: Display) -> Frame:
        return cls(display, *fit(display.width, display.height), display_area(display))

    @property
    def size_text(self) -> str:
        return f"{self.width}x{self.height}"

    def contains(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    @property
    def screen_pixels(self) -> float:
        """How many screen pixels one image pixel spans, on the coarser axis."""
        left, top, right, bottom = self.area
        return max((right - left) / self.width, (bottom - top) / self.height)

    def to_physical(self, x: int, y: int) -> tuple[int, int]:
        """The screen pixel under the centre of image pixel (x, y)."""
        left, top, right, bottom = self.area
        return (
            left + min(right - left - 1, math.floor((x + 0.5) * (right - left) / self.width)),
            top + min(bottom - top - 1, math.floor((y + 0.5) * (bottom - top) / self.height)),
        )

    def to_frame(self, x: int, y: int) -> tuple[int, int]:
        """The image pixel that shows screen pixel (x, y)."""
        left, top, right, bottom = self.area
        return (
            min(self.width - 1, max(0, math.floor((x - left + 0.5) * self.width / (right - left)))),
            min(
                self.height - 1, max(0, math.floor((y - top + 0.5) * self.height / (bottom - top)))
            ),
        )

    def region(self, x0: int, y0: int, x1: int, y1: int) -> Area:
        """Map image corners, including its exclusive bottom/right edges."""
        left, top, right, bottom = self.area
        return (
            left + round(x0 * (right - left) / self.width),
            top + round(y0 * (bottom - top) / self.height),
            left + round(x1 * (right - left) / self.width),
            top + round(y1 * (bottom - top) / self.height),
        )


def display_area(display: Display) -> Area:
    return (
        display.left,
        display.top,
        display.left + display.width,
        display.top + display.height,
    )


def display_of_window(displays: Sequence[Display], window: WindowInfo) -> Display | None:
    """Return the display showing most of *window*."""
    best: Display | None = None
    best_overlap = 0
    for display in displays:
        left, top, right, bottom = display_area(display)
        overlap = max(0, min(right, window.right) - max(left, window.left)) * max(
            0, min(bottom, window.bottom) - max(top, window.top)
        )
        if overlap > best_overlap:
            best, best_overlap = display, overlap
    return best


def find_display(displays: Sequence[Display], value: str) -> Display | None:
    """Return the display named *value*: its name, 1-based number, id or ``primary``."""
    text = value.strip()
    folded = text.casefold()
    if folded == "primary":
        return next((display for display in displays if display.primary), None)
    if text.isdigit():
        number = int(text)
        return displays[number - 1] if 1 <= number <= len(displays) else None
    for display in displays:
        if folded in {display.name.casefold(), display.id.casefold()}:
            return display
    matches = [display for display in displays if folded in display.name.casefold()]
    return matches[0] if len(matches) == 1 else None


def choose_display(
    displays: Sequence[Display],
    selection: str,
    foreground: WindowInfo | None,
    visible: Callable[[WindowInfo], bool],
) -> Display:
    """Return the display for *selection*; ``auto`` follows a visible foreground window."""
    if selection != "auto":
        chosen = next((display for display in displays if display.id == selection), None)
        if chosen is not None:
            return chosen
    if foreground is not None and visible(foreground):
        holding = display_of_window(displays, foreground)
        if holding is not None:
            return holding
    return next((display for display in displays if display.primary), displays[0])


def describe_displays(displays: Sequence[Display], current: Display | None) -> str:
    """One line per display: number, name, physical size, primary and current marks."""
    lines = []
    for number, display in enumerate(displays, start=1):
        marks = [
            mark
            for mark, shown in (
                ("primary", display.primary),
                ("current", current is not None and display.id == current.id),
            )
            if shown
        ]
        suffix = f" ({', '.join(marks)})" if marks else ""
        lines.append(f'{number}. "{display.name}" {display.width}x{display.height}{suffix}')
    return "\n".join(lines)


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


def publish_image(context: ToolContext, image: Image.Image) -> Path:
    """Attach *image* to the Tool Result for the Model and show it to the user."""
    stream = io.BytesIO()
    image.save(stream, format="PNG", compress_level=1)
    raw = stream.getvalue()
    directory = DataDirectoryLayout(context.data_root).temporary / IMAGE_CATEGORY
    path = write_id_file(directory, "shot", ".png", raw)
    context.result_media.append(
        {
            "path": path.as_posix(),
            "filename": path.name,
            "media_type": "image/png",
            "base64": base64.b64encode(raw).decode("ascii"),
        }
    )
    context.add_display_media(path, "image/png")
    return path


__all__ = [
    "IMAGE_CATEGORY",
    "MASK_FILL",
    "MAX_AREA",
    "MAX_LONG_EDGE",
    "Frame",
    "choose_display",
    "describe_displays",
    "display_area",
    "display_of_window",
    "find_display",
    "fit",
    "zoom_fit",
    "mask",
    "publish_image",
    "resized",
]
