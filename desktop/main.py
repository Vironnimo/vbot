"""Desktop launch, target probing, and window wiring for the vBot pywebview accessor.

The entrypoint builds the in-window server-selection controller
(:mod:`desktop.connection`) and the voice bridge (:mod:`desktop.wakeword.bridge`),
wires the *same* bridge as the window's single ``js_api`` (so both the shell
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
import importlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from desktop.settings import (
    config_dir,
    read_wakeword_settings,
    read_window_size,
    write_window_size,
)

if TYPE_CHECKING:
    from desktop.connection import ConnectionController

logger = logging.getLogger("vbot.desktop")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
WINDOW_TITLE = "vBot"
ICON_FILE_NAME = "icon.png"
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
DESKTOP_LOG_DIRECTORY_NAME = "logs"
DESKTOP_LOG_FILE_SUFFIX = ".log"
_DESKTOP_LOG_HANDLER_FLAG = "_vbot_desktop_log_handler"
_DESKTOP_LOG_FORMAT = "%(asctime)s [%(vbot_level)s] %(name)s - %(message)s"
_DESKTOP_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class HttpResponse(Protocol):
    """Subset of an HTTP response used by Desktop probing."""

    status_code: int

    def json(self) -> Any:
        """Return the decoded JSON body."""


class HttpGet(Protocol):
    """Synchronous HTTP GET callable used by Desktop probing."""

    def __call__(self, url: str, *, timeout: float) -> HttpResponse:
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
class DesktopProbeResult:
    """Result of probing a target vBot server and its WebUI root."""

    status: str
    target: DesktopTarget


@dataclass(frozen=True)
class DesktopWindowLayout:
    """Initial Desktop window size and its screen-safe resize floor."""

    width: int
    height: int
    minimum_width: int
    minimum_height: int


class _DesktopLogFormatter(logging.Formatter):
    """Render the Desktop process with the same structured labels as vBot logs."""

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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse Desktop CLI target arguments."""

    parser = argparse.ArgumentParser(description="Open the vBot desktop shell")
    parser.add_argument("--host")
    parser.add_argument("--port", type=_parse_port)
    parser.add_argument(
        "--mock-wakeword",
        action="store_true",
        help="Use a mock wakeword engine for UI validation without a real microphone.",
    )
    return parser.parse_args(argv)


def desktop_dir() -> Path:
    """Return the source-run Desktop directory used for the optional app icon."""

    return Path(__file__).resolve().parent


def icon_path(base_dir: Path | None = None) -> Path:
    """Return the optional source-run Desktop icon path."""

    return (base_dir if base_dir is not None else desktop_dir()) / ICON_FILE_NAME


def build_target_url(host: str, port: int) -> str:
    """Build the HTTP WebUI root URL for a resolved Desktop target."""

    return f"http://{validate_host(host)}:{port}/"


def probe_target(
    target: DesktopTarget,
    *,
    get: HttpGet = httpx.get,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> DesktopProbeResult:
    """Classify the configured server target before Desktop window creation."""

    if target.configuration_error is not None:
        return DesktopProbeResult(status=PROBE_INVALID_TARGET, target=target)

    health_url = f"{target.url.rstrip('/')}/health"
    try:
        health_response = get(health_url, timeout=timeout)
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_SERVER_UNREACHABLE, target=target)

    if health_response.status_code != 200 or not _is_vbot_health_response(health_response):
        return DesktopProbeResult(status=PROBE_NOT_VBOT_SERVER, target=target)

    try:
        webui_response = get(target.url, timeout=timeout)
    except httpx.RequestError:
        return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)

    if 200 <= webui_response.status_code <= 399:
        return DesktopProbeResult(status=PROBE_WEBUI_AVAILABLE, target=target)
    return DesktopProbeResult(status=PROBE_WEBUI_UNAVAILABLE, target=target)


def load_webview() -> WebviewModule:
    """Import pywebview lazily so non-desktop test gates do not require it."""

    try:
        return importlib.import_module("webview")
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
) -> None:
    """Build the controller, bridge, and window, then run the GUI loop.

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
    auto-connect. The effective launch target (override else last-used) is
    resolved once and used for both the window navigation and the voice worker's
    server URL, so window and voice always point at the same server.
    """

    from desktop.connection import ConnectionController, build_connection_html

    args = parse_args(argv)
    webview = webview_module if webview_module is not None else load_webview()

    controller = ConnectionController(settings_file=settings_file, probe=probe)
    override = _resolve_launch_override(args)
    server_url = _resolve_launch_server_url(override, controller)
    bridge = _create_wakeword_bridge(args, settings_file, controller, server_url)
    wakeword_enabled = bool(read_wakeword_settings(settings_file).get("enabled", False))
    # Voice follows the window: every successful in-window connect retargets the
    # worker, so first-run connect and runtime server switches no longer leave
    # voice pointed at the launch-time (or empty) server.
    controller.set_active_server_listener(bridge.set_server_url)

    # The window must be created with initial content before the GUI loop; the
    # connection screen is a safe neutral page that the post-loop entry callable
    # replaces once the loop is live (navigating to the WebUI on connect).
    initial_html = build_connection_html(servers=controller.list_servers())
    window_layout = resolve_window_layout(
        read_window_size(settings_file),
        _primary_screen_size(webview),
    )
    window = webview.create_window(
        WINDOW_TITLE,
        html=initial_html,
        text_select=True,
        js_api=bridge,
        width=window_layout.width,
        height=window_layout.height,
        min_size=(window_layout.minimum_width, window_layout.minimum_height),
    )
    controller.attach_window(window)

    start_kwargs: dict[str, Any] = {}
    resolved_icon_path = app_icon_path if app_icon_path is not None else icon_path()
    if resolved_icon_path.exists():
        # pywebview icon support varies by backend/platform, so custom icons are optional.
        start_kwargs["icon"] = str(resolved_icon_path)

    connection_entry = _select_launch_entry(controller, override)

    def start_visible_services() -> None:
        # The lightweight shell must become visible before any network probe or
        # optional ML/audio initialization. Connect first so Voice follows the
        # window's resolved target, then create its worker only when enabled.
        connection_entry()
        if wakeword_enabled:
            bridge._start_worker()

    window.events.shown += start_visible_services

    def persist_window_size() -> None:
        # Save only size, not position: OS centering keeps the window reachable
        # after a monitor is disconnected or the display layout changes.
        try:
            write_window_size(window.width, window.height, settings_file)
        except (AttributeError, OSError, RuntimeError, ValueError):
            logger.warning("Desktop window size could not be persisted", exc_info=True)

    window.events.closing += persist_window_size

    try:
        webview.start(**start_kwargs)
    finally:
        bridge._stop_worker()


def resolve_window_layout(
    saved_size: tuple[int, int] | None,
    screen_size: tuple[int, int],
) -> DesktopWindowLayout:
    """Return a useful initial size bounded to the current primary display.

    A fresh install uses roughly 80% of the display with a desktop-sized cap.
    A remembered size wins when present, but is clamped so a former large
    monitor cannot produce an unreachable or unusable window on a smaller one.
    """

    screen_width, screen_height = screen_size
    available_width = max(1, screen_width - WINDOW_SCREEN_EDGE_ALLOWANCE)
    available_height = max(1, screen_height - WINDOW_SCREEN_EDGE_ALLOWANCE)
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
    )


def _primary_screen_size(webview: WebviewModule) -> tuple[int, int]:
    """Return primary-screen logical pixels, with a stable fallback."""

    try:
        screens = webview.screens
        primary_screen = screens[0]
        width = int(primary_screen.width)
        height = int(primary_screen.height)
    except (AttributeError, IndexError, OSError, RuntimeError, TypeError, ValueError):
        return FALLBACK_SCREEN_WIDTH, FALLBACK_SCREEN_HEIGHT
    if width <= 0 or height <= 0:
        return FALLBACK_SCREEN_WIDTH, FALLBACK_SCREEN_HEIGHT
    return width, height


def _select_launch_entry(
    controller: ConnectionController,
    override: tuple[str, int] | None,
) -> Callable[[], Any]:
    """Return the nullary visible-window entry callable and log the chosen branch.

    An explicit CLI override connects straight to that target (the controller
    remembers it as a side effect of a successful connect); otherwise the
    controller auto-connects to last-used, or shows the connection screen on
    first run. The callback is attached to pywebview's window ``shown`` event,
    so the override branch is wrapped in a zero-argument closure.
    """

    if override is not None:
        host, port = override
        logger.info("Desktop starting; connecting to CLI override %s:%s", host, port)

        def connect_override() -> Any:
            return controller.connect(host, port)

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
    return controller.auto_connect


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


def _create_wakeword_bridge(
    args: argparse.Namespace,
    settings_file: Path | None,
    controller: ConnectionController,
    server_url: str,
) -> Any:
    """Create the DesktopBridge with engine and worker for the wakeword pipeline.

    The controller is passed in as the bridge's connection delegate (so the
    shell connection screen's ``connect`` call routes through it). ``server_url``
    is the *effective launch target* the caller resolved once (CLI override else
    last-used), so the local voice pipeline sends transcripts to the same server
    the window opens. An empty ``server_url`` is reported as an actionable Voice
    startup error before the engine or microphone opens.

    Mock mode is explicit through ``--mock-wakeword``. A missing on-device stack
    selects the non-simulating unavailable worker instead, so production never
    shows fake listening/sending activity. Returns the bridge instance in every
    mode so the WebUI can query capabilities and the concrete reason.
    """

    from desktop.wakeword.bridge import DesktopBridge
    from desktop.wakeword.worker import (
        MockWakewordWorker,
        UnavailableWakewordWorker,
        WakewordWorker,
        check_speech_to_text_readiness,
    )

    def worker_factory(bridge: DesktopBridge) -> Any:
        if bool(args.mock_wakeword):
            return MockWakewordWorker(bridge=bridge)
        # The TFLite detector and sounddevice are optional Desktop extras.
        # Probe them only when Voice is actually starting, never on an ordinary
        # Desktop launch with Voice disabled.
        if not _real_wakeword_available():
            bridge._set_mode("unavailable")
            return UnavailableWakewordWorker(bridge=bridge)
        bridge._set_mode("real")
        engine = bridge._create_wakeword_engine()
        # Read the current server URL off the bridge (not a captured constant) so
        # a worker rebuilt after a server switch targets the new server.
        return WakewordWorker(
            engine=engine,
            bridge=bridge,
            settings_path=settings_file,
            server_url=bridge.server_url,
            config_reader=bridge.worker_config,
            speech_readiness_checker=check_speech_to_text_readiness,
            calibration_checker=bridge.wakeword_calibration_active,
        )

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        connection=controller,
        server_url=server_url,
        mock=bool(args.mock_wakeword),
        mode="mock" if bool(args.mock_wakeword) else "real",
        speech_readiness_checker=check_speech_to_text_readiness,
    )
    return bridge


def _real_wakeword_available() -> bool:
    """Whether the on-device wakeword stack can be imported.

    The detector, microphone capture, and VAD modules must import for the real
    worker; a missing dependency selects unavailable mode. The worker factory calls this
    lazily only when Voice is enabled or explicitly retried, keeping the stack
    out of the normal Desktop startup path.
    """

    try:
        import pyopen_wakeword  # type: ignore[import-untyped]  # noqa: F401
        import sounddevice  # type: ignore[import-untyped]  # noqa: F401
        import webrtcvad  # type: ignore[import-untyped]  # noqa: F401
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
    try:
        launch_desktop(argv)
    except Exception:
        logger.error("Desktop stopped unexpectedly", exc_info=True)
        raise
    else:
        logger.info("Desktop stopped normally")
    finally:
        close_desktop_logging(log_handler)


def _parse_port(value: str) -> int:
    """Argparse adapter for Desktop port validation."""

    try:
        return validate_port(value, source="--port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":
    main()
