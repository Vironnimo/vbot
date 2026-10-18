"""Native window placement, focus and activation request handling."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from desktop import _windows, restart
from desktop.settings import read_window_size

if TYPE_CHECKING:
    from desktop.connection import ConnectionController
    from desktop.page_events import PageEventDispatcher
logger = logging.getLogger("vbot.desktop")
FALLBACK_SCREEN_WIDTH = 1600
FALLBACK_SCREEN_HEIGHT = 1000
DEFAULT_WINDOW_SCREEN_RATIO = 0.8
DEFAULT_WINDOW_MAX_WIDTH = 1440
DEFAULT_WINDOW_MAX_HEIGHT = 960
MINIMUM_WINDOW_WIDTH = 800
MINIMUM_WINDOW_HEIGHT = 600
WINDOW_SCREEN_EDGE_ALLOWANCE = 80
SESSION_LINK_PART_MAX_LENGTH = 512


class WebviewModule(Protocol):
    """Subset of pywebview used by the Desktop shell.

    pywebview requires the window to be created with initial content *before*
    the GUI loop starts; ``Window.load_url`` / ``load_html`` may only run after
    ``start``. Desktop therefore attaches its startup callback to the window's
    ``shown`` event and uses ``start`` only to enter the native GUI loop.
    """

    screens: list[Any]

    def create_window(self, title: str, **kwargs: Any) -> Any:
        """Create a window before the GUI loop starts (needs ``url`` or ``html``)."""

    def start(self, func: Any = None, **kwargs: Any) -> Any:
        """Start the native GUI loop, calling ``func`` once after it starts."""


@dataclass(frozen=True)
class SessionLink:
    """One Session to open in the WebUI: its Agent address (``agent`` or
    ``agent@project``) and its Session id."""

    agent: str
    session: str


@dataclass(frozen=True)
class DesktopWindowLayout:
    """Initial Desktop window size, its screen-safe resize floor, and the
    pywebview Screen to center the window on (or None for OS-default centering)."""

    width: int
    height: int
    minimum_width: int
    minimum_height: int
    screen: Any = None


def current_page_url(window_holder: list[Any]) -> str | None:
    """The window's top-level URL; ``None`` before the window exists or for inline HTML."""
    if not window_holder:
        return None
    try:
        url = window_holder[0].get_current_url()
    except Exception:
        return None
    return url if isinstance(url, str) else None


class WindowState:
    """Track whether the Desktop window is minimized or maximized.

    pywebview reports these only as events. The state brings a minimized,
    formerly maximized window back maximized (``restore`` always returns to the
    normal size) and describes the window for a restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._minimized = False
        self._maximized = False

    def track(self, window: Any, initial: str = "normal") -> None:
        with self._lock:
            self._minimized = initial == "minimized"
            self._maximized = initial == "maximized"
        window.events.minimized += self._on_minimized
        window.events.maximized += self._on_maximized
        window.events.restored += self._on_restored

    def flags(self) -> tuple[bool, bool]:
        """Return ``(minimized, maximized)``."""
        with self._lock:
            return self._minimized, self._maximized

    def placement(self, window: Any, settings_file: Path | None) -> restart.WindowPlacement:
        """Describe the window for its restart successor (logical pixels)."""
        minimized, maximized = self.flags()
        if minimized or maximized:
            # Such a window reports no useful normal size or position; the
            # successor uses the remembered size and centers.
            width, height = read_window_size(settings_file) or (window.width, window.height)
            state: restart.WindowState = "minimized" if minimized else "maximized"
            return restart.WindowPlacement(state, int(width), int(height))
        return restart.WindowPlacement(
            "normal", int(window.width), int(window.height), int(window.x), int(window.y)
        )

    def _on_minimized(self) -> None:
        with self._lock:
            self._minimized = True

    def _on_maximized(self) -> None:
        with self._lock:
            self._minimized = False
            self._maximized = True

    def _on_restored(self) -> None:
        with self._lock:
            self._minimized = False
            self._maximized = False


class WindowFocus:
    """Bring the Desktop window to the front from a background thread.

    The Window API marshals onto the GUI thread itself.
    """

    def __init__(self, window: Any, state: WindowState) -> None:
        self._window = window
        self._state = state

    def bring_to_front(self) -> None:
        minimized, maximized = self._state.flags()
        if minimized:
            if maximized:
                self._window.maximize()
            else:
                self._window.restore()
        self._window.show()


def page_location(window_holder: list[Any], controller: ConnectionController) -> str | None:
    """Return the WebUI URL fragment the window shows, for a restart to restore."""

    active = controller.active_server_url()
    if not window_holder or active is None:
        return None
    url = window_holder[0].get_current_url()
    if not isinstance(url, str) or not url.startswith(active):
        return None
    _, separator, fragment = url.partition("#")
    location = f"#{fragment}" if separator and fragment else None
    return location if restart.is_location(location) else None


def resolve_window_layout(
    saved_size: tuple[int, int] | None,
    screen: Any | None,
) -> DesktopWindowLayout:
    """Return a useful initial size bounded to the current primary display.

    A fresh install uses roughly 80% of the display with a desktop-sized cap.
    A remembered size wins when present, but is clamped so a former large
    monitor cannot produce an unreachable or unusable window on a smaller one.

    DPI-aware: the clamp uses the screen's work area in logical pixels, so a
    window that fits logically also fits physically after the monitor's DPI
    scale is applied. The returned screen is passed to pywebview's
    create_window so the window centers on the correct monitor with correct
    DPI conversion, instead of WinForms' CenterScreen default which can
    center on the wrong monitor or mismatch logical/physical dimensions.
    """

    if screen is None:
        screen_width = FALLBACK_SCREEN_WIDTH
        screen_height = FALLBACK_SCREEN_HEIGHT
        available_width = max(1, screen_width - WINDOW_SCREEN_EDGE_ALLOWANCE)
        available_height = max(1, screen_height - WINDOW_SCREEN_EDGE_ALLOWANCE)
        placement_screen: Any = None
    else:
        _, _, screen_width, screen_height = _screen_work_area(screen)
        available_width = max(1, screen_width)
        available_height = max(1, screen_height)
        placement_screen = screen

    minimum_width = min(MINIMUM_WINDOW_WIDTH, available_width)
    minimum_height = min(MINIMUM_WINDOW_HEIGHT, available_height)

    if saved_size is None:
        preferred_width = round(screen_width * DEFAULT_WINDOW_SCREEN_RATIO)
        preferred_height = round(screen_height * DEFAULT_WINDOW_SCREEN_RATIO)
        width = min(preferred_width, DEFAULT_WINDOW_MAX_WIDTH)
        height = min(preferred_height, DEFAULT_WINDOW_MAX_HEIGHT)
    else:
        width, height = saved_size

    return DesktopWindowLayout(
        width=max(minimum_width, min(width, available_width)),
        height=max(minimum_height, min(height, available_height)),
        minimum_width=minimum_width,
        minimum_height=minimum_height,
        screen=placement_screen,
    )


def primary_screen(webview: WebviewModule) -> Any | None:
    """Return the pywebview Screen that contains the desktop origin (0, 0).

    WinForms does not guarantee screens[0] is the primary monitor; the list
    order is arbitrary. The primary screen is the one whose bounds include the
    origin. Falls back to screens[0] when no screen contains the origin or when
    screen discovery fails.
    """

    try:
        screens = webview.screens
        if not screens:
            return None
        for candidate in screens:
            cx = int(getattr(candidate, "x", 0))
            cy = int(getattr(candidate, "y", 0))
            cw = int(getattr(candidate, "width", 0))
            ch = int(getattr(candidate, "height", 0))
            if cx <= 0 < cx + cw and cy <= 0 < cy + ch:
                return _windows.logical_primary_screen(candidate)
        return screens[0]
    except AttributeError, IndexError, OSError, RuntimeError, TypeError, ValueError:
        return None


def _screen_work_area(screen: Any) -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of the screen's usable work area in logical pixels.

    The work area excludes OS chrome like the taskbar. pywebview's Screen.frame
    holds the platform-native work-area rectangle, whose attribute casing differs
    by platform (Windows uses PascalCase, GTK uses lowercase). Both are tried.
    Falls back to the full screen bounds when the frame is unavailable.
    """

    frame = getattr(screen, "frame", None)
    if frame is not None:
        for width_attr, height_attr, x_attr, y_attr in (
            ("Width", "Height", "X", "Y"),
            ("width", "height", "x", "y"),
        ):
            if hasattr(frame, width_attr) and hasattr(frame, height_attr):
                try:
                    work_width = int(getattr(frame, width_attr))
                    work_height = int(getattr(frame, height_attr))
                    if work_width > 0 and work_height > 0:
                        work_x = int(getattr(frame, x_attr, getattr(screen, "x", 0)))
                        work_y = int(getattr(frame, y_attr, getattr(screen, "y", 0)))
                        return work_x, work_y, work_width, work_height
                except TypeError, ValueError, AttributeError:
                    continue
    return (
        int(getattr(screen, "x", 0)),
        int(getattr(screen, "y", 0)),
        int(getattr(screen, "width", FALLBACK_SCREEN_WIDTH)),
        int(getattr(screen, "height", FALLBACK_SCREEN_HEIGHT)),
    )


def activation_handler(
    controller: ConnectionController,
    page_events: PageEventDispatcher,
    focus: WindowFocus,
    desktop_restart: restart.DesktopRestart | None = None,
) -> Callable[[Mapping[str, Any] | None], None]:
    """Return the running Desktop's answer to a later launch.

    The window always comes to the front. A handed-over ``--open-session``
    request additionally opens its Session through the page, but only when
    the window shows the requested server: the one the launch named with
    ``--host`` / ``--port``, or any server when it named none. A restart
    successor's ``{"handoff": nonce}`` instead lets the restart close this
    window.
    """

    def on_activate(request: Mapping[str, Any] | None) -> None:
        if request is not None and "handoff" in request:
            if desktop_restart is not None:
                desktop_restart.accept_handoff(request.get("handoff"))
            else:
                logger.warning("Ignoring a Desktop handoff; this Desktop is not restarting")
            return
        link = _requested_session(request, controller)
        if link is not None:
            page_events.request_open_session(link.agent, link.session)
        focus.bring_to_front()

    return on_activate


def activation_request(
    override: tuple[str, int] | None,
    session_link: SessionLink | None,
) -> dict[str, Any] | None:
    """Return what a launch hands over to a running Desktop, if anything."""

    if session_link is None:
        return None
    host, port = override if override is not None else (None, None)
    return {
        "host": host,
        "port": port,
        "agent": session_link.agent,
        "session": session_link.session,
    }


def _requested_session(
    request: Mapping[str, Any] | None,
    controller: ConnectionController,
) -> SessionLink | None:
    """Return the handed-over Session when the window shows its server."""

    if request is None:
        return None
    agent = request.get("agent")
    session = request.get("session")
    host = request.get("host")
    port = request.get("port")
    if not (
        isinstance(agent, str)
        and isinstance(session, str)
        and is_session_link_part(agent)
        and is_session_link_part(session)
    ):
        logger.warning("Ignoring a malformed Session request from another Desktop launch")
        return None
    if host is None and port is None:
        shown = controller.active_server_url() is not None
    elif isinstance(host, str) and isinstance(port, int) and not isinstance(port, bool):
        shown = controller.is_active_server(host, port)
    else:
        logger.warning("Ignoring a malformed Session request from another Desktop launch")
        return None
    if not shown:
        logger.info(
            "Not opening Session %s of %s: the window does not show the requested server",
            session,
            agent,
        )
        return None
    return SessionLink(agent=agent, session=session)


def is_session_link_part(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= SESSION_LINK_PART_MAX_LENGTH
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )
