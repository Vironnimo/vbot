"""The Windows desktop target: the signed-in user's real displays, windows, apps and input.

``WindowsTarget`` implements ``DesktopTarget`` with Win32 through ctypes and
Pillow. Every method enters per-monitor DPI awareness on its calling thread,
so all coordinates are physical virtual-desktop pixels. Input comes from
:class:`WindowsInput`; app identity from ``_win_apps``. Captures come without a
colour filter that desktop composition applied, when references prove one
(``_color_filter``).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Iterator, Sequence

from PIL import Image

from . import _color_filter, _win32, _win_apps
from ._win_input import WindowsInput
from ._win_overlay import ActivityOverlay
from .target import AppInfo, Display, TargetError, WindowInfo

_LOCKED = (
    "The Windows screen is locked or shows a secure prompt such as User Account Control. "
    "Ask the user to unlock it or answer the prompt."
)
_FIRST_DISCOVERY_WAIT = 20.0  # seconds the first app lookup waits for the Start-menu listing
_ACTIVATION_WAIT = 0.5
# Seconds a reference window may take to render itself; a busy app blocks rendering.
_REFERENCE_WAIT = 0.25
_TASKBAR_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
# The desktop renders the composed screen, filter included, so it is no reference.
_DESKTOP_CLASSES = frozenset({"Progman", "WorkerW"})
_LOGGER = logging.getLogger("vbot.extensions.computer_use")


def name_displays(monitors: Sequence[_win32.Monitor], names: dict[str, str]) -> list[Display]:
    """Displays in their stable order (primary first, then left to right) with unique names.

    A monitor without a model name is "Display N" after its position; repeated model
    names get the position in parentheses.
    """
    ordered = sorted(monitors, key=lambda item: (not item.primary, item.left, item.top))
    labels = [names.get(item.device) or "" for item in ordered]
    displays = []
    for number, (monitor, label) in enumerate(zip(ordered, labels, strict=True), start=1):
        if not label:
            label = f"Display {number}"
        elif labels.count(label) > 1:
            label = f"{label} ({number})"
        displays.append(
            Display(
                id=monitor.device,
                name=label,
                left=monitor.left,
                top=monitor.top,
                width=monitor.right - monitor.left,
                height=monitor.bottom - monitor.top,
                primary=monitor.primary,
                scale_percent=round(monitor.dpi * 100 / 96),
            )
        )
    return displays


class WindowsTarget(WindowsInput):
    """``DesktopTarget`` for the interactive Windows desktop vBot runs on."""

    def __init__(self) -> None:
        super().__init__()
        self._start = _win_apps.StartApps(windows_dir=_win32.windows_directory())
        self._descriptions: dict[str, str | None] = {}
        self._own_integrity: int | None = None
        self._display_names: tuple[tuple[str, ...], dict[str, str]] = ((), {})
        self._overlay = ActivityOverlay()
        # The colour filter last proven per display id, and windows still rendering.
        self._filters: dict[str, _color_filter.Matrix] = {}
        self._filtered: set[str] = set()
        self._rendering: set[int] = set()

    def readiness(self) -> str | None:
        if sys.platform != "win32":
            return "Computer Use needs a Windows desktop; this vBot server runs on another system."
        try:
            if not _win32.interactive_window_station():
                return (
                    "vBot runs without an interactive desktop (for example as a Windows "
                    "service), so it cannot see or control the screen. Run vBot in the "
                    "signed-in user's session."
                )
        except (TargetError, OSError, AttributeError) as error:  # AttributeError: old Windows
            return f"The Windows desktop is unavailable ({error})."
        return None

    # Displays and screenshots

    def displays(self) -> list[Display]:
        _win32.enter_thread()
        monitors = _win32.monitors()
        if not monitors:
            raise TargetError("Windows reports no display.", "computer_use_unavailable")
        devices = tuple(sorted(monitor.device for monitor in monitors))
        if devices != self._display_names[0]:
            self._display_names = (devices, _win32.monitor_names())
        return name_displays(monitors, self._display_names[1])

    def capture(self, display: Display) -> Image.Image:
        _win32.enter_thread()
        if not _win32.input_desktop_available():
            raise TargetError(_LOCKED, "computer_use_unavailable")
        if display.width <= 0 or display.height <= 0:
            raise TargetError("The display has no visible area.", "computer_use_unavailable")
        pixels = _win32.capture_bgrx(display.left, display.top, display.width, display.height)
        size = (display.width, display.height)
        image = Image.frombuffer("RGB", size, pixels, "raw", "BGRX", 0, 1)
        return self._without_filter(display, image)

    def _without_filter(self, display: Display, image: Image.Image) -> Image.Image:
        """*image* with a proven composition colour filter removed, else unchanged."""
        matrix = _color_filter.measure(
            self._references(display, image), self._filters.get(display.id)
        )
        if (matrix is not None) != (display.id in self._filtered):
            self._filtered ^= {display.id}
            _LOGGER.debug(
                "Screen colour filter %s on display %s",
                "removed from captures" if matrix is not None else "no longer removed",
                display.id,
            )
        if matrix is None:
            return image
        self._filters[display.id] = matrix
        return _color_filter.remove(image, matrix)

    def _references(
        self, display: Display, image: Image.Image
    ) -> Iterator[_color_filter.Reference]:
        """The foreground window and the taskbar as they drew themselves, beside *image*."""
        foreground = _win32.foreground_window()
        handles = (
            [foreground]
            if foreground and _win32.window_class(foreground) not in _DESKTOP_CLASSES
            else []
        )
        handles += [
            handle
            for handle in _win32.top_level_windows()
            if handle != foreground and _win32.window_class(handle) in _TASKBAR_CLASSES
        ]
        for handle in handles:
            if (
                not _win32.is_visible(handle)
                or _win32.is_minimized(handle)
                or _win32.is_cloaked(handle)
                or _win32.is_hung(handle)
            ):
                continue
            bounds = _win32.window_bounds(handle)
            if bounds is None:
                continue
            left, top = max(bounds[0], display.left), max(bounds[1], display.top)
            right = min(bounds[2], display.left + display.width)
            bottom = min(bounds[3], display.top + display.height)
            if right - left < 32 or bottom - top < 16:
                continue
            rendered = self._render(handle)
            if rendered is None:
                continue
            rect, pixels = rendered
            size = (rect[2] - rect[0], rect[3] - rect[1])
            drawn = Image.frombuffer("RGB", size, pixels, "raw", "BGRX", 0, 1)
            yield _color_filter.Reference(
                drawn.crop((left - rect[0], top - rect[1], right - rect[0], bottom - rect[1])),
                image.crop(
                    (
                        left - display.left,
                        top - display.top,
                        right - display.left,
                        bottom - display.top,
                    )
                ),
            )

    def _render(self, handle: int) -> tuple[tuple[int, int, int, int], bytes] | None:
        """A window's own rendering, or ``None`` when it does not arrive within the wait.

        A window busy past the wait finishes on its own thread; until then it is skipped.
        """
        if handle in self._rendering:
            return None
        result: list[tuple[tuple[int, int, int, int], bytes] | None] = []

        def render() -> None:
            try:
                _win32.enter_thread()
                result.append(_win32.render_window_bgrx(handle))
            except Exception:
                _LOGGER.debug("A reference window could not render", exc_info=True)
            finally:
                self._rendering.discard(handle)

        self._rendering.add(handle)
        thread = threading.Thread(target=render, name="computer-use-reference", daemon=True)
        thread.start()
        thread.join(_REFERENCE_WAIT)
        return result[0] if result else None

    # Windows

    def windows(self) -> list[WindowInfo]:
        _win32.enter_thread()
        resolver = self._resolver()
        left, top, width, height = _win32.virtual_screen()
        found = []
        for handle in _win32.top_level_windows():
            if (
                not _win32.is_visible(handle)
                or _win32.is_minimized(handle)
                or _win32.is_cloaked(handle)
            ):
                continue
            bounds = _win32.window_bounds(handle)
            if bounds is None or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue
            if (
                bounds[2] <= left
                or bounds[3] <= top
                or bounds[0] >= left + width
                or bounds[1] >= top + height
            ):
                continue  # parked off-screen
            if _invisible_overlay(handle):
                continue
            found.append(self._describe(handle, bounds, resolver))
        return found

    def foreground(self) -> WindowInfo | None:
        _win32.enter_thread()
        handle = _win32.foreground_window()
        return self._describe(handle, None, self._resolver()) if handle else None

    def window_at(self, x: int, y: int) -> WindowInfo | None:
        _win32.enter_thread()
        handle = _win32.window_at(x, y)
        return self._describe(handle, None, self._resolver()) if handle else None

    # Apps

    def apps(self) -> list[AppInfo]:
        _win32.enter_thread()
        index = self._start.index(wait=_FIRST_DISCOVERY_WAIT)
        resolver = self._resolver(index)
        running = [resolver.window(handle)[0] for handle in self._app_windows()]
        return _win_apps.merge_apps(index.apps, running)

    def set_activity(self, active: bool) -> None:
        if active:
            self._overlay.show()
        else:
            self._overlay.hide()

    def close(self) -> None:
        self._overlay.close()

    def open(self, app: AppInfo) -> None:
        _win32.enter_thread()
        resolver = self._resolver()
        for handle in self._app_windows():
            window_app, facts = resolver.window(handle)
            shell = _win_apps.is_shell_surface(_win32.window_class(handle), facts.executable)
            if not shell and app.matches(window_app):
                self._activate(handle, app.name)
                return
        target = self._launch_target(app)
        if target is None:
            raise TargetError(
                f"{app.name} has no open window, and Windows offers no way to start it.",
                "invalid_arguments",
            )
        # A zero mouse move makes vBot the latest input source, which lets it pass the
        # foreground on, so the started app opens in front instead of flashing.
        self._nudge()
        _win32.allow_any_foreground()
        _win_apps.launch(target)

    # Mechanics

    def _resolver(self, index: _win_apps.StartIndex | None = None) -> _win_apps.AppResolver:
        if self._own_integrity is None:
            bound = _win32.api()
            self._own_integrity = _win_apps.token_integrity(bound.kernel32.GetCurrentProcess())
        # The first resolution waits for the Start menu listing, so a window's app has
        # its Start-menu name from the first call on; later calls never wait for it.
        return _win_apps.AppResolver(
            self._start.index(wait=_FIRST_DISCOVERY_WAIT) if index is None else index,
            self._descriptions,
            self._own_integrity,
        )

    def _describe(
        self,
        handle: int,
        bounds: tuple[int, int, int, int] | None,
        resolver: _win_apps.AppResolver,
    ) -> WindowInfo:
        app, facts = resolver.window(handle)
        left, top, right, bottom = bounds or _win32.window_bounds(handle) or (0, 0, 0, 0)
        return WindowInfo(
            handle=handle,
            title=_win32.window_title(handle),
            app=app,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            elevated=resolver.elevated(facts),
            owner=_win32.window_owner(handle),
        )

    @staticmethod
    def _app_windows() -> list[int]:
        """Windows a user switches to (as Alt+Tab lists them), minimized ones included."""
        handles = []
        for handle in _win32.top_level_windows():
            if not _win32.is_visible(handle) or _win32.is_cloaked(handle):
                continue
            style = _win32.extended_style(handle)
            if style & (_win32.WS_EX_TOOLWINDOW | _win32.WS_EX_NOACTIVATE) and not (
                style & _win32.WS_EX_APPWINDOW
            ):
                continue
            if _win32.window_owner(handle) and not style & _win32.WS_EX_APPWINDOW:
                continue
            handles.append(handle)
        return handles

    def _launch_target(self, app: AppInfo) -> str | None:
        index = self._start.index(wait=_FIRST_DISCOVERY_WAIT)
        entries = [entry for entry in index.apps if entry.keys & app.keys]
        entries.sort(key=lambda entry: entry.name != app.name)
        if entries:
            return f"shell:AppsFolder\\{entries[0].app_id}"
        return next(
            (
                key
                for key in sorted(app.keys)
                if key.endswith(".exe") and not key.startswith(("exe:", "pkg:", "vbot:"))
            ),
            None,
        )

    def _activate(self, handle: int, name: str) -> None:
        """Restore and bring a window to the front despite Windows' foreground lock."""
        popup = _win32.last_active_popup(handle)
        if _win32.is_minimized(handle):
            _win32.show_window(handle, _win32.SW_RESTORE)
        if (
            popup
            and popup != handle
            and _win32.is_visible(popup)
            and _win32.is_enabled(popup)
            and _win32.window_process(popup)[0] == _win32.window_process(handle)[0]
        ):
            handle = popup  # a modal dialog keeps its owner disabled; activate the dialog
        if _win32.foreground_window() == handle:
            return
        # Windows lets the source of the latest input change the foreground window.
        self._nudge()
        if _win32.set_foreground(handle) or _win32.attach_and_activate(handle):
            return
        self._tap_alt()
        _win32.set_foreground(handle)
        deadline = time.monotonic() + _ACTIVATION_WAIT
        while _win32.foreground_window() != handle:
            if time.monotonic() >= deadline:
                raise TargetError(
                    f"Windows did not bring {name} to the front. Ask the user to switch to it, "
                    "or click its taskbar button."
                )
            self._pause(0.05)


def _invisible_overlay(handle: int) -> bool:
    """A layered window the user cannot see or click: click-through or fully transparent."""
    style = _win32.extended_style(handle)
    if not style & _win32.WS_EX_LAYERED:
        return False
    return bool(style & _win32.WS_EX_TRANSPARENT) or _win32.layered_alpha(handle) == 0
