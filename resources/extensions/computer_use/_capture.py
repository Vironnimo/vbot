"""Native observation and image preparation, shared by local and remote targets."""

from __future__ import annotations

from PIL import Image

from ._imaging import Area, mask, resized
from .target import CaptureChangedError, DesktopSnapshot, DesktopTarget


def snapshot(target: DesktopTarget, points: list[tuple[int, int]]) -> DesktopSnapshot:
    return DesktopSnapshot(
        tuple(target.displays()),
        tuple(target.windows()),
        target.foreground(),
        tuple(target.window_at(x, y) for x, y in points),
    )


def capture_region(
    target: DesktopTarget,
    area: Area,
    observed: DesktopSnapshot,
    visible: list[int],
    size: tuple[int, int],
) -> Image.Image:
    # Never resize private pixels into neighbouring visible pixels. Check both
    # sides of the complete multi-monitor capture before masking and resizing.
    if not observed.same_layout(target.snapshot([])):
        raise CaptureChangedError()
    capture = Image.new("RGB", (area[2] - area[0], area[3] - area[1]))
    for display in observed.displays:
        left, top = max(area[0], display.left), max(area[1], display.top)
        right = min(area[2], display.left + display.width)
        bottom = min(area[3], display.top + display.height)
        if left < right and top < bottom:
            image = target.capture(display).crop(
                (
                    left - display.left,
                    top - display.top,
                    right - display.left,
                    bottom - display.top,
                )
            )
            capture.paste(image, (left - area[0], top - area[1]))
    if not observed.same_layout(target.snapshot([])):
        raise CaptureChangedError()
    handles = set(visible)
    covered, _ = mask(capture, area, observed.windows, lambda window: window.handle in handles)
    return resized(covered, size)
