"""The Desktop window's zoom: one stored level that scales the whole window.

Like a browser's page zoom, it scales everything the window shows (the WebUI and
the connection screen). It belongs to this Desktop installation, not to a
server, and lives in the ``zoom`` section of the Desktop settings file as
``{"percent": <level>}``; a missing or unknown value reads as
:data:`DEFAULT_ZOOM`. Only the levels in :data:`ZOOM_LEVELS` exist.

The page offers the control (Settings -> Appearance) and the shortcuts (Ctrl +
Plus/Minus/0, Ctrl + mouse wheel) and sends the chosen level through the
bridge. WebView2's own zoom inputs are off, so the stored level is always the
window's zoom (:func:`desktop._windows.bind_window_zoom` applies it).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from desktop import settings as desktop_settings

logger = logging.getLogger("vbot.desktop.zoom")

ZOOM_SECTION = "zoom"
#: The zoom levels in percent: Chromium's page zoom steps from 50 to 200 percent.
ZOOM_LEVELS = (50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200)
DEFAULT_ZOOM = 100
ZOOM_ERROR_INVALID = "zoom_invalid"


class ZoomError(ValueError):
    """A zoom value that is not one of :data:`ZOOM_LEVELS`."""

    error_code = ZOOM_ERROR_INVALID


class DesktopZoom:
    """Owner of the stored zoom level and its application to the window.

    Thread-safe: bridge calls run on their own threads. :meth:`attach` binds the
    native window; until then, and on platforms without one, the level is only
    stored and :attr:`supported` is false.
    """

    def __init__(self, *, settings_path: Path | None = None) -> None:
        self._settings_path = settings_path
        self._lock = threading.Lock()
        self._percent = _stored_level(desktop_settings.read_section(ZOOM_SECTION, settings_path))
        self._apply: Callable[[float], None] | None = None

    @property
    def supported(self) -> bool:
        """Whether a window applies the zoom."""
        return self._apply is not None

    def attach(self, apply: Callable[[float], None]) -> None:
        """Bind the function that applies a zoom factor to the window from any thread."""
        with self._lock:
            self._apply = apply

    def factor(self) -> float:
        """Return the stored zoom as a factor (1.0 for 100 percent)."""
        with self._lock:
            return self._percent / 100

    def status(self) -> dict[str, Any]:
        """Return ``{supported, percent, levels, default}``."""
        with self._lock:
            return self._status_locked()

    def set(self, percent: Any) -> dict[str, Any]:
        """Store and apply one of :data:`ZOOM_LEVELS`; returns the status.

        Raises :class:`ZoomError` for any other value and ``OSError`` when the
        settings file cannot be written; the zoom then stays unchanged.
        """
        level = _parse_level(percent)
        with self._lock:
            desktop_settings.update_section(
                ZOOM_SECTION,
                lambda section: {**section, "percent": level},
                self._settings_path,
            )
            self._percent = level
            if self._apply is not None:
                self._apply(level / 100)
            logger.debug("Desktop zoom set to %d%%", level)
            return self._status_locked()

    def _status_locked(self) -> dict[str, Any]:
        return {
            "supported": self._apply is not None,
            "percent": self._percent,
            "levels": list(ZOOM_LEVELS),
            "default": DEFAULT_ZOOM,
        }


def _parse_level(value: Any) -> int:
    # JavaScript numbers may arrive as floats; booleans are ints in Python.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ZoomError(f"zoom must be one of {ZOOM_LEVELS}, got {value!r}")
    if value not in ZOOM_LEVELS:
        raise ZoomError(f"zoom must be one of {ZOOM_LEVELS}, got {value!r}")
    return int(value)


def _stored_level(section: dict[str, Any]) -> int:
    try:
        return _parse_level(section.get("percent"))
    except ZoomError:
        return DEFAULT_ZOOM
