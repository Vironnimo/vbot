"""The Desktop's taskbar badge: the number of unread results on the taskbar button.

The loaded WebUI knows what is unread and sends the count through the bridge
(``setTaskbarBadge``); the Desktop only shows it. The count belongs to the page
that sent it, so every page load clears the badge and the connection screen
shows none. Windows draws it as an overlay on the window's taskbar button
(:func:`bind_window_badge`); other platforms have no badge.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import sys
import threading
import uuid
from collections.abc import Callable
from typing import Any

from desktop._windows import win32_library

logger = logging.getLogger("vbot.desktop.taskbar_badge")

BADGE_ERROR_INVALID = "badge_invalid"
#: Counts above this show as ``9+``: a taskbar overlay has room for two characters.
BADGE_MAX_SHOWN = 9
# The WebUI's unread indicator: ``--blue`` with ``--bg`` text.
_BADGE_COLOR = (0x74, 0xAD, 0xF2)
_BADGE_TEXT_COLOR = (0x15, 0x13, 0x0F)
_CLSID_TASKBAR_LIST = "56fdf344-fd6d-11d0-958a-006097c9a090"
_IID_TASKBAR_LIST3 = "ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf"
# Positions in the ITaskbarList3 method table.
_HR_INIT = 3
_SET_OVERLAY_ICON = 18


class TaskbarBadgeError(ValueError):
    """A badge count that is not a whole number of zero or more."""

    error_code = BADGE_ERROR_INVALID


def badge_label(count: int) -> str:
    """Return the text the badge shows for ``count``; empty for no badge."""
    if count <= 0:
        return ""
    return str(count) if count <= BADGE_MAX_SHOWN else f"{BADGE_MAX_SHOWN}+"


class TaskbarBadge:
    """Owner of the count the taskbar button shows.

    Thread-safe: bridge calls run on their own threads. :meth:`attach` binds
    the native window; until then, and on platforms without a badge,
    :attr:`supported` is false and a count is only remembered.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._apply: Callable[[str, str], None] | None = None

    @property
    def supported(self) -> bool:
        """Whether a window shows the badge."""
        return self._apply is not None

    def attach(self, apply: Callable[[str, str], None]) -> None:
        """Bind the function that shows a label and its description from any thread."""
        with self._lock:
            self._apply = apply

    def set(self, count: Any) -> dict[str, Any]:
        """Show ``count`` unread results (0 removes the badge); returns ``{count}``.

        Raises :class:`TaskbarBadgeError` for anything but a whole number of
        zero or more.
        """
        # JavaScript numbers may arrive as floats; booleans are ints in Python.
        if (
            isinstance(count, bool)
            or not isinstance(count, int | float)
            or count < 0
            or count != int(count)
        ):
            raise TaskbarBadgeError(f"badge count must be a whole number >= 0, got {count!r}")
        return self._show(int(count))

    def clear(self) -> None:
        """Remove the badge: the page that sent the count is gone."""
        self._show(0)

    def _show(self, count: int) -> dict[str, Any]:
        with self._lock:
            if count != self._count:
                self._count = count
                if self._apply is not None:
                    unit = "result" if count == 1 else "results"
                    self._apply(badge_label(count), f"{count} unread {unit}" if count else "")
                logger.debug("Desktop taskbar badge set to %d", count)
            return {"count": self._count}


def bind_window_badge(window: Any) -> Callable[[str, str], None] | None:
    """Return the function that shows a badge on the window's taskbar button.

    It takes the label (empty removes the badge) and its description for
    assistive technology and may be called from any thread; the taskbar is
    changed on the GUI thread. Returns ``None`` on platforms without a badge.
    A failure is logged once per stage and leaves the taskbar as it is.
    """
    if sys.platform != "win32":
        return None
    failures: set[str] = set()
    taskbar: list[Callable[[int, int | None, str | None], None]] = []

    def report(stage: str) -> None:
        if stage not in failures:
            failures.add(stage)
            logger.warning("Desktop taskbar badge failed (%s)", stage, exc_info=True)

    def show(native: Any, label: str, description: str) -> None:
        try:
            if native.IsDisposed or not native.IsHandleCreated:
                return
            if not taskbar:
                taskbar.append(_overlay_setter())
            handle = native.Handle.ToInt64()
            if not label:
                taskbar[0](handle, None, None)
                return
            icon = _badge_icon(label, int(native.DeviceDpi))
            try:
                taskbar[0](handle, icon, description)
            finally:
                # The taskbar keeps its own copy.
                win32_library("user32").DestroyIcon(ctypes.c_void_p(icon))
        except Exception:
            report("show")

    def apply(label: str, description: str) -> None:
        native = getattr(window, "native", None)
        if native is None:
            return
        try:
            system = importlib.import_module("System")
            native.BeginInvoke(system.Action(lambda: show(native, label, description)))
        except Exception:
            report("apply")

    return apply


def _overlay_setter() -> Callable[[int, int | None, str | None], None]:
    """Create the shell's taskbar object; must run on the GUI thread.

    Returns ``set_overlay(window_handle, icon_handle, description)``; no icon
    removes the overlay.
    """
    if sys.platform != "win32":
        raise OSError("The taskbar overlay exists only on Windows")

    def guid(text: str) -> Any:
        return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)

    create = win32_library("ole32").CoCreateInstance
    create.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create.restype = ctypes.c_long
    instance = ctypes.c_void_p()
    # CLSCTX_INPROC_SERVER
    _check(
        create(
            ctypes.byref(guid(_CLSID_TASKBAR_LIST)),
            None,
            1,
            ctypes.byref(guid(_IID_TASKBAR_LIST3)),
            ctypes.byref(instance),
        ),
        "CoCreateInstance",
    )
    methods = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    initialize = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(methods[_HR_INIT])
    _check(initialize(instance), "HrInit")
    set_overlay_icon = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p
    )(methods[_SET_OVERLAY_ICON])

    def set_overlay(window_handle: int, icon_handle: int | None, description: str | None) -> None:
        # The object lives as long as the process: `instance` stays referenced here.
        _check(
            set_overlay_icon(instance, window_handle, icon_handle, description), "SetOverlayIcon"
        )

    return set_overlay


def _check(result: int, call: str) -> None:
    if result < 0:
        raise OSError(f"{call} failed (0x{result & 0xFFFFFFFF:08X})")


def _badge_icon(label: str, dpi: int) -> int:
    """Draw the badge as an icon handle the caller destroys; runs on the GUI thread."""
    system = importlib.import_module("System")
    drawing = importlib.import_module("System.Drawing")
    smoothing = importlib.import_module("System.Drawing.Drawing2D").SmoothingMode
    text_hint = importlib.import_module("System.Drawing.Text").TextRenderingHint
    # The overlay is a small icon: 16 pixels at 100 percent scaling.
    size = max(16, round(16 * dpi / 96))
    bitmap = drawing.Bitmap(size, size)
    graphics = drawing.Graphics.FromImage(bitmap)
    fill = drawing.SolidBrush(drawing.Color.FromArgb(*_BADGE_COLOR))
    ink = drawing.SolidBrush(drawing.Color.FromArgb(*_BADGE_TEXT_COLOR))
    font = drawing.Font(
        "Segoe UI",
        system.Single(size * (0.72 if len(label) == 1 else 0.6)),
        drawing.FontStyle.Bold,
        drawing.GraphicsUnit.Pixel,
    )
    layout = drawing.StringFormat()
    try:
        graphics.SmoothingMode = smoothing.AntiAlias
        # ClearType needs an opaque background; the icon's is transparent.
        graphics.TextRenderingHint = text_hint.AntiAlias
        graphics.FillEllipse(fill, 0, 0, size - 1, size - 1)
        layout.Alignment = drawing.StringAlignment.Center
        layout.LineAlignment = drawing.StringAlignment.Center
        graphics.DrawString(
            label,
            font,
            ink,
            drawing.RectangleF(
                system.Single(0), system.Single(0), system.Single(size), system.Single(size)
            ),
            layout,
        )
        return int(bitmap.GetHicon().ToInt64())
    finally:
        for resource in (layout, font, ink, fill, graphics, bitmap):
            resource.Dispose()
