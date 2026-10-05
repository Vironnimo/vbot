"""Desktop launch, target probing, and window wiring for the vBot pywebview accessor.

The entrypoint builds the in-window server-selection controller
(:mod:`desktop.connection`), Voice (:mod:`desktop.wakeword.controller`), the
page pushes (:mod:`desktop.page_events`) and, for a packaged launch, the update
restart (:mod:`desktop.restart`), wires the *same* bridge facade
(:mod:`desktop.bridge`) as the window's single ``js_api`` (so both the shell
connection screen and the remote WebUI call into it), and hands the live window
to the controller. There is no silent localhost default: the controller
auto-connects to the last-used server after the GUI loop starts, or shows the
connection screen on first run / any unreachable target.

This module still owns the shared probing primitives (``probe_target`` /
``validate_host`` / ``validate_port`` / ``build_target_url`` and the ``PROBE_*``
classifications) that the connection controller reuses; it no longer owns the
old static fallback page or pre-loop target resolution — the controller subsumes
both.
"""

from __future__ import annotations

import argparse
import faulthandler
import importlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, override

import httpx

from desktop import _windows, restart
from desktop.settings import (
    config_dir,
    read_window_size,
    write_window_size,
)

if TYPE_CHECKING:
    from desktop.connection import ConnectionController
    from desktop.page_events import PageEventDispatcher
    from desktop.speech.microphone import MicrophoneService
    from desktop.wakeword.controller import VoiceController

logger = logging.getLogger("vbot.desktop")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
WINDOW_TITLE = "vBot"
APP_BACKGROUND_COLOR = "#15130F"
DEFAULT_ICON_FILE_NAME = "icon.png"
WINDOWS_ICON_FILE_NAME = "icon.ico"
WEBVIEW_STORAGE_DIR_NAME = "webview"
FALLBACK_SCREEN_WIDTH = 1600
FALLBACK_SCREEN_HEIGHT = 1000
DEFAULT_WINDOW_SCREEN_RATIO = 0.8
DEFAULT_WINDOW_MAX_WIDTH = 1440
DEFAULT_WINDOW_MAX_HEIGHT = 960
MINIMUM_WINDOW_WIDTH = 800
MINIMUM_WINDOW_HEIGHT = 600
WINDOW_SCREEN_EDGE_ALLOWANCE = 80
PROBE_TIMEOUT_SECONDS = 2.0
PROBE_WEBUI_AVAILABLE = "webui_available"
PROBE_WEBUI_UNAVAILABLE = "webui_unavailable"
PROBE_SERVER_UNREACHABLE = "server_unreachable"
PROBE_NOT_VBOT_SERVER = "not_vbot_server"
PROBE_INVALID_TARGET = "invalid_target"
# Block URL-structure characters plus HTML/JS metacharacters: none appear in a
# valid host name or IPv4 literal, and barring them keeps a stored host from
# ever carrying a markup/script payload into the connection screen.
INVALID_HOST_CHARACTERS = frozenset("/\\:?#@[]'\"`<>&();")
ACCESSOR_QUERY_PARAM = "accessor=desktop"
SESSION_LINK_PART_MAX_LENGTH = 512
DESKTOP_LOG_DIRECTORY_NAME = "logs"
DESKTOP_LOG_FILE_SUFFIX = ".log"
_DESKTOP_LOG_HANDLER_FLAG = "_vbot_desktop_log_handler"
_DESKTOP_LOG_FORMAT = "%(asctime)s [%(vbot_level)s] %(name)s - %(message)s"
_DESKTOP_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Fatal errors (a native crash, a fatal interpreter error) bypass logging; the
# fault handler writes them to this file beside the daily logs instead.
DESKTOP_CRASH_LOG_NAME = "desktop-crash.log"
# Checked at each start: a larger crash log moves to its single previous generation.
_DESKTOP_CRASH_LOG_ROTATE_BYTES = 1024 * 1024


class HttpResponse(Protocol):
    """Subset of an HTTP response used by Desktop probing."""

    status_code: int

    def json(self) -> Any:
        """Return the decoded JSON body."""


class HttpGet(Protocol):
    """Synchronous HTTP GET callable used by Desktop probing."""

    def __call__(self, url: str, *, timeout: float, trust_env: bool) -> HttpResponse:
        """Fetch a URL with a bounded timeout."""


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
class DesktopTarget:
    """Resolved Desktop server target."""

    host: str
    port: int
    url: str
    configuration_error: str | None = None


@dataclass(frozen=True)
class SessionLink:
    """One Session to open in the WebUI: its Agent address (``agent`` or
    ``agent@project``) and its Session id."""

    agent: str
    session: str


@dataclass(frozen=True)
class DesktopProbeResult:
    """Result of probing a target vBot server and its WebUI root."""

    status: str
    target: DesktopTarget


@dataclass(frozen=True)
class DesktopWindowLayout:
    """Initial Desktop window size, its screen-safe resize floor, and the
    pywebview Screen to center the window on (or None for OS-default centering)."""

    width: int
    height: int
    minimum_width: int
    minimum_height: int
    screen: Any = None


class _DesktopLogFormatter(logging.Formatter):
    """Render the Desktop process with the same structured labels as vBot logs."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        original_label = getattr(record, "vbot_level", None)
        record.vbot_level = "WARN" if record.levelname == "WARNING" else record.levelname
        try:
            return super().format(record)
        finally:
            if original_label is None:
                delattr(record, "vbot_level")
            else:
                record.vbot_level = original_label


class _DesktopDailyFileHandler(logging.FileHandler):
    """Write the standalone Desktop process to per-user daily log files."""

    def __init__(
        self,
        logs_directory: Path,
        *,
        current_date_provider: Callable[[], date] = date.today,
    ) -> None:
        self._logs_directory = logs_directory
        self._logs_directory.mkdir(parents=True, exist_ok=True)
        self._current_date_provider = current_date_provider
        self._active_date = current_date_provider()
        self.original_logger_level = logging.NOTSET
        self.original_logger_propagate = True
        super().__init__(self._path_for(self._active_date), encoding="utf-8")

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self._rotate_if_needed()
        super().emit(record)

    def _rotate_if_needed(self) -> None:
        current_date = self._current_date_provider()
        if current_date == self._active_date:
            return
        self.acquire()
        try:
            if current_date == self._active_date:
                return
            if self.stream is not None:
                self.stream.close()
            self._active_date = current_date
            self.baseFilename = os.fspath(self._path_for(current_date))
            self.stream = self._open()
        finally:
            self.release()

    def _path_for(self, target_date: date) -> Path:
        return self._logs_directory / f"{target_date.isoformat()}{DESKTOP_LOG_FILE_SUFFIX}"


def configure_desktop_logging(
    desktop_config_directory: Path | None = None,
) -> logging.Handler | None:
    """Attach structured per-user file logging for the standalone Desktop process."""

    vbot_logger = logging.getLogger("vbot")
    for handler in vbot_logger.handlers:
        if getattr(handler, _DESKTOP_LOG_HANDLER_FLAG, False):
            return handler
    try:
        handler = _DesktopDailyFileHandler(
            (desktop_config_directory or config_dir()) / DESKTOP_LOG_DIRECTORY_NAME
        )
    except OSError:
        return None
    setattr(handler, _DESKTOP_LOG_HANDLER_FLAG, True)
    handler.original_logger_level = vbot_logger.level
    handler.original_logger_propagate = vbot_logger.propagate
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        _DesktopLogFormatter(_DESKTOP_LOG_FORMAT, datefmt=_DESKTOP_LOG_DATE_FORMAT)
    )
    vbot_logger.setLevel(logging.INFO)
    vbot_logger.propagate = False
    vbot_logger.addHandler(handler)
    return handler


def close_desktop_logging(handler: logging.Handler | None) -> None:
    """Detach the file handler created for this Desktop process."""

    if handler is None:
        return
    vbot_logger = logging.getLogger("vbot")
    vbot_logger.removeHandler(handler)
    handler.close()
    original_level = getattr(handler, "original_logger_level", logging.NOTSET)
    original_propagate = getattr(handler, "original_logger_propagate", True)
    vbot_logger.setLevel(original_level)
    vbot_logger.propagate = original_propagate


def enable_desktop_crash_log(desktop_config_directory: Path | None = None) -> None:
    """Record this process's fatal errors in the crash log beside its daily logs.

    Native libraries (WebView2 through .NET, PortAudio, the wake word and speech
    models) can end the process without a Python traceback. Each start appends
    one line in the log format, so a crashed process's dump (every thread's
    Python stack, and the C stack where the platform provides one) follows the
    line of its own start. The fault handler keeps the file object referenced,
    so it stays open until the interpreter has finalized.
    """

    path = (
        (desktop_config_directory or config_dir())
        / DESKTOP_LOG_DIRECTORY_NAME
        / DESKTOP_CRASH_LOG_NAME
    )
    started = time.strftime(_DESKTOP_LOG_DATE_FORMAT)
    header = f"{started} [INFO] vbot.desktop - Desktop process started (pid={os.getpid()})\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A missing file needs nothing; a file another Desktop process holds open
        # cannot move and keeps growing until a later start.
        with suppress(OSError):
            if path.stat().st_size > _DESKTOP_CRASH_LOG_ROTATE_BYTES:
                os.replace(path, path.with_name(f"{path.name}.1"))
        crash_log = path.open("ab", buffering=0)
        try:
            crash_log.write(header.encode("utf-8"))
        except OSError:
            crash_log.close()
            raise
    except OSError as exc:
        logger.warning("Opening the crash log failed (path=%s error=%s)", path, exc)
        return
    faulthandler.enable(file=crash_log, all_threads=True, c_stack=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse Desktop CLI target arguments."""

    parser = argparse.ArgumentParser(description="Open the vBot desktop shell")
    parser.add_argument("--host")
    parser.add_argument("--port", type=_parse_port)
    parser.add_argument(
        "--open-session",
        nargs=2,
        metavar=("AGENT_ADDRESS", "SESSION_ID"),
        type=_parse_session_link_part,
        help=(
            "Open this Session once the WebUI has loaded. A vBot Desktop that is "
            "already running comes to the front and opens the Session when it "
            "shows the --host/--port server (without them, whichever it shows)."
        ),
    )
    parser.add_argument(
        "--mock-wakeword",
        action="store_true",
        help="Use a mock wakeword engine for UI validation without a real microphone.",
    )
    return parser.parse_args(argv)


def desktop_dir() -> Path:
    """Return the source-run Desktop directory used for the optional app icon."""

    return Path(__file__).resolve().parent


def icon_path(base_dir: Path | None = None, *, platform: str = sys.platform) -> Path:
    """Return the platform-native optional Desktop icon path."""

    file_name = WINDOWS_ICON_FILE_NAME if platform == "win32" else DEFAULT_ICON_FILE_NAME
    return (base_dir if base_dir is not None else desktop_dir()) / file_name


def build_target_url(host: str, port: int) -> str:
    """Build the HTTP WebUI root URL for a resolved Desktop target."""

    return f"http://{validate_host(host)}:{port}/"


def probe_target(
    target: DesktopTarget,
    *,
    get: HttpGet | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> DesktopProbeResult:
    """Classify the target using one client and connection for both requests."""

    if target.configuration_error is not None:
        return DesktopProbeResult(status=PROBE_INVALID_TARGET, target=target)

    if get is not None:
        return _probe_target(target, lambda url: get(url, timeout=timeout, trust_env=False))
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        return _probe_target(target, client.get)


def _probe_target(target: DesktopTarget, get: Callable[[str], HttpResponse]) -> DesktopProbeResult:
    health_url = f"{target.url.rstrip('/')}/health"
    try:
        health_response = get(health_url)
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_SERVER_UNREACHABLE, target=target)

    if health_response.status_code != 200 or not _is_vbot_health_response(health_response):
        return DesktopProbeResult(status=PROBE_NOT_VBOT_SERVER, target=target)

    try:
        webui_response = get(target.url)
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)

    if 200 <= webui_response.status_code <= 399:
        return DesktopProbeResult(status=PROBE_WEBUI_AVAILABLE, target=target)
    return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)


def load_webview() -> WebviewModule:
    """Import pywebview lazily so non-desktop test gates do not require it."""

    _windows.configure_dpi()
    try:
        webview = importlib.import_module("webview")
        _windows.configure_winforms()
        return webview
    except ImportError as exc:
        raise RuntimeError(
            "pywebview is required to run vBot Desktop. "
            "Install the desktop optional dependency group, for example: "
            'pip install -e ".[desktop]"'
        ) from exc


def launch_desktop(
    argv: list[str] | None = None,
    *,
    settings_file: Path | None = None,
    probe: Callable[[DesktopTarget], DesktopProbeResult] = probe_target,
    webview_module: WebviewModule | None = None,
    app_icon_path: Path | None = None,
) -> bool:
    """Build the controller, bridge, and window, then run the GUI loop.

    Returns ``False`` without creating a window when a Desktop for the same
    config directory is already running (it is asked to come to the front
    instead), else ``True`` after the window closed.

    Lifecycle (pywebview requires this order): create the window *before* the
    loop with the connection screen as neutral initial content and the bridge as
    its single ``js_api``; hand the window to the controller; attach visible
    startup to the window's ``shown`` event; then start the loop. Server probing
    and optional Voice startup therefore happen only after the native window is
    visible. No native menu is attached; connected server management lives in
    Desktop app Settings.

    Target selection: an explicit ``--host`` / ``--port`` override connects
    straight to that target; with no flags the controller auto-connects to the
    last-used server, or shows the connection screen on first run. There is no
    silent localhost default — only a *deliberate* CLI override skips
    auto-connect. Either launch connect keeps trying a server that is still
    unreachable for a while, showing a waiting state. The effective launch
    target (override else last-used) is resolved once and used for both the
    window navigation and Voice's server URL, so window and Voice always point
    at the same server.

    ``--open-session`` rides on the first navigation to the WebUI. A launch
    that finds a running Desktop hands it over with the activation instead
    (see :func:`_activation_handler`).
    """

    args = parse_args(argv)
    override = _resolve_launch_override(args)
    session_link = _resolve_session_link(args)
    desktop_config_directory = settings_file.parent if settings_file is not None else config_dir()
    contract = restart.relaunch_contract()
    # A packaged launch may be the successor of a Desktop restarting into this
    # version; it restores what that Desktop showed.
    restart_request = (
        restart.take_restart_request(desktop_config_directory, now=time.time())
        if contract is not None
        else None
    )
    if restart_request is not None:
        logger.info("Desktop starting as the successor of a restarting Desktop")
        if restart_request.server is not None:
            override, session_link = restart_request.server, None
    instance = _windows.claim_desktop_instance(
        desktop_config_directory,
        request=_activation_request(override, session_link),
        handoff=restart_request.nonce if restart_request is not None else None,
    )
    if instance is None:
        if restart_request is not None:
            return False
        if session_link is not None:
            logger.info(
                "vBot Desktop is already running; focused the open window and handed "
                "over Session %s of %s",
                session_link.session,
                session_link.agent,
            )
        elif override is not None:
            logger.info(
                "vBot Desktop is already running; focused the open window and ignored "
                "the requested target %s:%s",
                *override,
            )
        else:
            logger.info("vBot Desktop is already running; focused the open window")
        return False
    try:
        _run_desktop(
            args,
            override,
            session_link,
            instance,
            contract=contract,
            restart_request=restart_request,
            desktop_config_directory=desktop_config_directory,
            settings_file=settings_file,
            probe=probe,
            webview_module=webview_module,
            app_icon_path=app_icon_path,
        )
    finally:
        instance.close()
    return True


def _run_desktop(
    args: argparse.Namespace,
    override: tuple[str, int] | None,
    session_link: SessionLink | None,
    instance: _windows.DesktopInstance,
    *,
    contract: restart.RelaunchContract | None = None,
    restart_request: restart.RestartRequest | None = None,
    desktop_config_directory: Path,
    settings_file: Path | None,
    probe: Callable[[DesktopTarget], DesktopProbeResult],
    webview_module: WebviewModule | None,
    app_icon_path: Path | None,
) -> None:
    """Create the window and its services for the instance that owns the Desktop."""

    from desktop.bridge import DesktopBridge
    from desktop.connection import ConnectionController, build_connection_html
    from desktop.hotkey import LIVE_VOICE_HOTKEY, HotkeyController, HotkeyHandlers
    from desktop.page_events import PageEventDispatcher
    from desktop.speech.microphone import MicrophoneService

    webview = webview_module if webview_module is not None else load_webview()

    controller = ConnectionController(settings_file=settings_file, probe=probe)
    server_url = _resolve_launch_server_url(override, controller)
    # Every remembered server plus the launch target becomes a secure context,
    # so Live voice can open the microphone over plain HTTP on the LAN. The list
    # is fixed for this process; a server added later needs a restart.
    secure_origins = _windows.webview_secure_origins(_launch_targets(controller, override))
    page_events = PageEventDispatcher()
    # Built first: it moves an older microphone choice out of the Voice
    # settings before Voice reads them.
    microphone = MicrophoneService(settings_path=settings_file)
    live_hotkey = HotkeyController(
        preference=LIVE_VOICE_HOTKEY,
        settings_path=settings_file,
        handlers=HotkeyHandlers(on_press=lambda: page_events.request_live("toggle", "hotkey")),
    )
    voice = _create_voice(args, settings_file, microphone, server_url, page_events)
    window_holder: list[Any] = []
    window_state = _WindowState()
    desktop_restart = (
        restart.DesktopRestart(
            contract,
            config_directory=desktop_config_directory,
            page=page_events,
            active_server=controller.active_target,
            location=lambda: _page_location(window_holder, controller),
            placement=lambda: (
                window_state.placement(window_holder[0], settings_file) if window_holder else None
            ),
            shell_busy=voice.is_busy,
            close_window=lambda: window_holder[0].destroy(),
        )
        if contract is not None
        else None
    )
    bridge = DesktopBridge(
        voice=voice,
        microphone=microphone,
        connection=controller,
        live_hotkey=live_hotkey,
        secure_origins=secure_origins,
        restart=desktop_restart,
    )
    # Voice follows the window: every successful in-window connect retargets
    # it, so first-run connect and runtime server switches never leave Voice
    # pointed at the launch-time (or empty) server.
    controller.set_active_server_listener(voice.set_server_url)

    # The window must be created with initial content before the GUI loop; the
    # connection screen is a safe neutral page that the post-loop entry callable
    # replaces once the loop is live (navigating to the WebUI on connect).
    initial_html = build_connection_html(servers=controller.list_servers())
    placement = restart_request.placement if restart_request is not None else None
    window_layout = resolve_window_layout(
        (placement.width, placement.height)
        if placement is not None
        else read_window_size(settings_file),
        _primary_screen(webview),
    )
    placement_kwargs: dict[str, Any] = {}
    if placement is not None:
        # A restart reopens the window where and how it was; the position is
        # only kept for the normal state and was taken seconds ago.
        if placement.x is not None and placement.y is not None:
            placement_kwargs.update(x=placement.x, y=placement.y)
        placement_kwargs.update(
            minimized=placement.state == "minimized",
            maximized=placement.state == "maximized",
        )
    window = webview.create_window(
        WINDOW_TITLE,
        html=initial_html,
        background_color=APP_BACKGROUND_COLOR,
        text_select=True,
        js_api=bridge,
        width=window_layout.width,
        height=window_layout.height,
        min_size=(window_layout.minimum_width, window_layout.minimum_height),
        screen=window_layout.screen,
        **placement_kwargs,
    )
    window_holder.append(window)
    window_state.track(window, placement.state if placement is not None else "normal")
    _windows.bind_window_dpi(window, (window_layout.minimum_width, window_layout.minimum_height))
    _windows.allow_server_microphone(
        window, lambda: _windows.url_origin(controller.active_server_url())
    )
    controller.attach_window(window)
    page_events.attach_window(window)
    instance.listen(
        _activation_handler(
            controller, page_events, _WindowFocus(window, window_state), desktop_restart
        )
    )

    start_kwargs: dict[str, Any] = {}
    resolved_icon_path = app_icon_path if app_icon_path is not None else icon_path()
    if resolved_icon_path.exists():
        # pywebview icon support varies by backend/platform, so custom icons are optional.
        start_kwargs["icon"] = str(resolved_icon_path)

    # Persist the WebView2 profile (localStorage, cookies) across Desktop
    # restarts. pywebview defaults to private_mode=True, which points the
    # user-data folder at a temp dir and deletes it on window close — that
    # would wipe the WebUI's remembered agent/project selection on every
    # launch. private_mode=False keeps the profile, and the explicit
    # storage_path pins it beside the Desktop settings file instead of the
    # shared %APPDATA%\pywebview folder.
    start_kwargs["private_mode"] = False
    start_kwargs["storage_path"] = str(desktop_config_directory / WEBVIEW_STORAGE_DIR_NAME)

    connection_entry = _select_launch_entry(
        controller,
        override,
        session_link,
        location=restart_request.location if restart_request is not None else None,
    )

    def start_visible_services() -> None:
        # The lightweight shell must become visible before any network probe or
        # optional ML/audio initialization. Connect first so Voice follows the
        # window's resolved target; Voice starts listening only when enabled.
        connection_entry()
        voice.start()
        live_hotkey.start()
        if desktop_restart is not None:
            desktop_restart.start()

    window.events.shown += start_visible_services

    def persist_window_size() -> None:
        # Save only size, not position: OS centering keeps the window reachable
        # after a monitor is disconnected or the display layout changes.
        try:
            write_window_size(window.width, window.height, settings_file)
        except AttributeError, OSError, RuntimeError, ValueError:
            logger.warning("Desktop window size could not be persisted", exc_info=True)

    window.events.closing += persist_window_size

    # WebView2 reads its browser arguments when webview.start creates it.
    _windows.apply_browser_arguments(secure_origins)
    try:
        webview.start(**start_kwargs)
    finally:
        # First, so a launch connect still waiting never retargets a closed Voice.
        controller.close()
        if desktop_restart is not None:
            desktop_restart.close()
        live_hotkey.stop()
        voice.close()
        page_events.close()


class _WindowState:
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


class _WindowFocus:
    """Bring the Desktop window to the front from a background thread.

    The Window API marshals onto the GUI thread itself.
    """

    def __init__(self, window: Any, state: _WindowState) -> None:
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


def _page_location(window_holder: list[Any], controller: ConnectionController) -> str | None:
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


def _launch_targets(
    controller: ConnectionController,
    override: tuple[str, int] | None,
) -> list[tuple[str, int]]:
    """Return every server this launch may show: remembered ones plus the override."""

    targets = [(entry.host, entry.port) for entry in controller.list_servers()]
    if override is not None:
        targets.append(override)
    return targets


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


def _primary_screen(webview: WebviewModule) -> Any | None:
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


def _select_launch_entry(
    controller: ConnectionController,
    override: tuple[str, int] | None,
    session_link: SessionLink | None,
    *,
    location: str | None = None,
) -> Callable[[], Any]:
    """Return the nullary visible-window entry callable and log the chosen branch.

    An explicit CLI override (or a restart successor's server) connects
    straight to that target (the controller remembers it as a side effect of a
    successful connect); otherwise the controller auto-connects to last-used,
    or shows the connection screen on first run. Both launch connects wait for
    a server that is still unreachable (``ConnectionController.launch_connect``).
    A ``session_link`` rides on that first connect only. The callback is
    attached to pywebview's window ``shown`` event, so both branches are
    wrapped in a zero-argument closure.
    """

    if override is not None:
        host, port = override
        logger.info("Desktop starting; connecting to CLI override %s:%s", host, port)

        def connect_override() -> Any:
            return controller.launch_connect(
                host, port, open_session=session_link, location=location
            )

        return connect_override

    launch_target = controller.resolve_last_used()
    if launch_target is None:
        logger.info("Desktop starting with no saved server; showing connection screen")
    else:
        logger.info(
            "Desktop starting; auto-connecting to %s:%s",
            launch_target.host,
            launch_target.port,
        )

    def auto_connect() -> Any:
        return controller.auto_connect(open_session=session_link)

    return auto_connect


def _activation_handler(
    controller: ConnectionController,
    page_events: PageEventDispatcher,
    focus: _WindowFocus,
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


def _activation_request(
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
        and _is_session_link_part(agent)
        and _is_session_link_part(session)
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


def validate_port(value: Any, *, source: str = "port") -> int:
    """Validate a TCP port value."""

    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be an integer port") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{source} must be between 1 and 65535")
    return port


def validate_host(value: Any, *, source: str = "host") -> str:
    """Validate a localhost or LAN host value before building an HTTP URL."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a host name or IP address")
    host = value.strip()
    if not host:
        raise ValueError(f"{source} must not be empty")
    if any(character.isspace() for character in host):
        raise ValueError(f"{source} must not contain whitespace")
    if any(character in INVALID_HOST_CHARACTERS for character in host):
        raise ValueError(f"{source} must be a host name or IP address, not a URL")
    return host


def _is_vbot_health_response(response: HttpResponse) -> bool:
    """Return whether /health matches the vBot server identity contract."""

    try:
        payload = response.json()
    except ValueError:
        return False
    return bool(payload == {"status": "ok"})


def _create_voice(
    args: argparse.Namespace,
    settings_file: Path | None,
    microphone: MicrophoneService,
    server_url: str,
    page_events: PageEventDispatcher,
) -> VoiceController:
    """Create Voice for the window's server; it starts listening on ``start()``.

    ``server_url`` is the *effective launch target* the caller resolved once
    (CLI override else last-used), so Voice sends commands to the server the
    window opens. Mock mode is explicit through ``--mock-wakeword``. A missing
    on-device stack is detected lazily, when a listener first starts, and
    reported as the ``unavailable`` mode instead of simulated activity.
    """

    from desktop.wakeword.controller import VoiceController

    return VoiceController(
        settings_path=settings_file,
        microphone=microphone,
        server_url=server_url,
        sink=page_events,
        live_requests=page_events.request_live,
        mock=bool(args.mock_wakeword),
        stack_available=_real_wakeword_available,
    )


def _real_wakeword_available() -> bool:
    """Whether the on-device wakeword stack can be imported.

    The detector, microphone capture, and anti-aliasing resampler modules must
    import for a real listener; a missing dependency selects the
    unavailable mode. Voice calls this lazily when a listener first starts
    (Voice enabled) or on a retry, keeping the stack out of the normal Desktop
    startup path.
    """

    try:
        import pyopen_wakeword  # type: ignore[import-untyped]  # noqa: F401
        import sounddevice  # type: ignore[import-untyped]  # noqa: F401
        import soxr  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        return False
    return True


def _resolve_launch_override(args: argparse.Namespace) -> tuple[str, int] | None:
    """Return an explicit ``(host, port)`` launch override from the CLI flags.

    A ``--host`` and/or ``--port`` is a *deliberate* target, not the silent
    localhost default the plan removed — so when either is given it must take
    effect. A missing half is filled from ``DEFAULT_HOST`` / ``DEFAULT_PORT``
    (acceptable because the user explicitly asked to launch at a specific
    target). With neither flag given, returns ``None`` and the launcher falls
    back to last-used auto-connect.
    """

    if args.host is None and args.port is None:
        return None
    host = args.host if args.host is not None else DEFAULT_HOST
    port = args.port if args.port is not None else DEFAULT_PORT
    return (host, port)


def _resolve_session_link(args: argparse.Namespace) -> SessionLink | None:
    """Return the ``--open-session`` request, if one was given."""

    if args.open_session is None:
        return None
    agent, session = args.open_session
    return SessionLink(agent=agent, session=session)


def _resolve_launch_server_url(
    override: tuple[str, int] | None,
    controller: ConnectionController,
) -> str:
    """Return the WebUI base URL of the effective launch target for the worker.

    Uses the CLI override when present, else the controller's last-used / first
    remembered target, so the voice worker and the window point at the *same*
    server. Returns an empty string when there is no target (first run, no flags)
    or when the host cannot form a valid URL — the worker treats that as "no
    server" and skips network calls.
    """

    if override is not None:
        host, port = override
    else:
        entry = controller.resolve_last_used()
        if entry is None:
            return ""
        host, port = entry.host, entry.port
    try:
        return build_target_url(host, port)
    except ValueError:
        return ""


def main(argv: list[str] | None = None) -> None:
    """Open the vBot Desktop shell, routing target selection through the window."""

    log_handler = configure_desktop_logging()
    enable_desktop_crash_log()
    try:
        opened = launch_desktop(argv)
    except Exception:
        logger.error("Desktop stopped unexpectedly", exc_info=True)
        raise
    else:
        if opened:
            logger.info("Desktop stopped normally")
    finally:
        close_desktop_logging(log_handler)


def _parse_port(value: str) -> int:
    """Argparse adapter for Desktop port validation."""

    try:
        return validate_port(value, source="--port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_session_link_part(value: str) -> str:
    """Argparse adapter for the Agent address and Session id of ``--open-session``."""

    if not _is_session_link_part(value):
        raise argparse.ArgumentTypeError(
            "--open-session needs a non-empty Agent address and Session id without "
            f"spaces or control characters, at most {SESSION_LINK_PART_MAX_LENGTH} "
            "characters each"
        )
    return value


def _is_session_link_part(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= SESSION_LINK_PART_MAX_LENGTH
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )


if __name__ == "__main__":
    main()
