"""Platform-neutral imaging: coordinate frames, display choice, masking and images.

Each published screenshot carries its own pixel space and physical rectangle.
Coordinates measured in screenshots are mapped here,
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

from PIL import Image

from core.storage.layout import DataDirectoryLayout
from core.storage.temp_files import artifact_owner
from core.tools import ToolContext
from core.utils.ids import write_id_file

from ._imaging import MASK_FILL, MAX_AREA, MAX_LONG_EDGE
from ._imaging import Area as Area
from ._imaging import fit as fit
from ._imaging import mask as mask
from ._imaging import resized as resized
from .target import AppInfo, Display, WindowInfo

# Temporary-file category of published images (``TEMPORARY_FILE_RETENTION``).
IMAGE_CATEGORY = "computer_use"
# Lossless PNG at zlib level 4: every later request uploads each screenshot of the
# history again, and level 4 is the strongest within the encode budget (about 30 ms).
# Not WebP: xAI carries only JPEG and PNG (extensions/computer-use.md -> Screenshot encoding).
_PNG_COMPRESS_LEVEL = 4


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
    # The windows over the image's area when it was taken, topmost first.
    stack: tuple[WindowInfo, ...] = ()

    @classmethod
    def of(cls, display: Display) -> Frame:
        return cls(display, *fit(display.width, display.height), display_area(display))

    @property
    def size_text(self) -> str:
        return f"{self.width}x{self.height}"

    def contains(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

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

    def owner(self, x: int, y: int) -> AppInfo | None:
        """The app the image shows at screen pixel (x, y), if the image covers it."""
        left, top, right, bottom = self.area
        if not (left <= x < right and top <= y < bottom):
            return None
        return app_at(self.stack, x, y)


def app_at(windows: Sequence[WindowInfo], x: int, y: int) -> AppInfo | None:
    """The app of the topmost of *windows* (topmost first) at screen pixel (x, y)."""
    return next((item.app for item in windows if item.contains(x, y)), None)


def overlaps(window: WindowInfo, area: Area) -> bool:
    """Whether *window* covers part of the physical *area*."""
    left, top, right, bottom = area
    return (
        window.left < right and left < window.right and window.top < bottom and top < window.bottom
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


def publish_image(context: ToolContext, image: Image.Image) -> Path:
    """Attach *image* to the Tool Result for the Model and show it to the user.

    The file lies in the calling Session's own temporary-file folder, which a
    restricted Session may read.
    """
    stream = io.BytesIO()
    image.save(stream, format="PNG", compress_level=_PNG_COMPRESS_LEVEL)
    raw = stream.getvalue()
    owner = artifact_owner(context.project_id, context.agent_id, context.session_id)
    directory = DataDirectoryLayout(context.data_root).temporary / IMAGE_CATEGORY / owner
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
    "app_at",
    "choose_display",
    "describe_displays",
    "display_area",
    "display_of_window",
    "find_display",
    "fit",
    "mask",
    "overlaps",
    "publish_image",
    "resized",
]
