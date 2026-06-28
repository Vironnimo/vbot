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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from desktop.settings import read_wakeword_settings

if TYPE_CHECKING:
    from desktop.connection import ConnectionController

logger = logging.getLogger("vbot.desktop")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
WINDOW_TITLE = "vBot"
ICON_FILE_NAME = "icon.png"
PROBE_TIMEOUT_SECONDS = 2.0
PROBE_WEBUI_AVAILABLE = "webui_available"
PROBE_WEBUI_UNAVAILABLE = "webui_unavailable"
PROBE_SERVER_UNREACHABLE = "server_unreachable"
PROBE_NOT_VBOT_SERVER = "not_vbot_server"
PROBE_INVALID_TARGET = "invalid_target"
INVALID_HOST_CHARACTERS = frozenset("/\\:?#@[]")
ACCESSOR_QUERY_PARAM = "accessor=desktop"


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
    ``start``. ``start`` therefore takes the post-loop entry callable
    (``func``), the native ``menu`` list, and the optional ``icon``.
    """

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
    menu_module: Any = None,
    app_icon_path: Path | None = None,
) -> None:
    """Build the controller, bridge, window, and menu, then run the GUI loop.

    Lifecycle (pywebview requires this order): create the window *before* the
    loop with the connection screen as neutral initial content and the bridge as
    its single ``js_api``; hand the window to the controller; then start the loop
    with a post-loop entry callable and the native Server menu attached. The
    entry callable runs only after the loop is live, so its ``load_url`` /
    ``load_html`` navigation is valid.

    Target selection: an explicit ``--host`` / ``--port`` override connects
    straight to that target; with no flags the controller auto-connects to the
    last-used server, or shows the connection screen on first run. There is no
    silent localhost default — only a *deliberate* CLI override skips
    auto-connect. The effective launch target (override else last-used) is
    resolved once and used for both the window navigation and the voice worker's
    server URL, so window and voice always point at the same server.
    """

    from desktop.connection import (
        ConnectionController,
        build_connection_html,
        build_server_menu,
    )

    args = parse_args(argv)
    webview = webview_module if webview_module is not None else load_webview()

    controller = ConnectionController(settings_file=settings_file, probe=probe)
    override = _resolve_launch_override(args)
    server_url = _resolve_launch_server_url(override, controller)
    bridge = _create_wakeword_bridge(args, settings_file, controller, server_url)

    # The window must be created with initial content before the GUI loop; the
    # connection screen is a safe neutral page that the post-loop entry callable
    # replaces once the loop is live (navigating to the WebUI on connect).
    initial_html = build_connection_html(servers=controller.list_servers())
    window = webview.create_window(
        WINDOW_TITLE,
        html=initial_html,
        text_select=True,
        js_api=bridge,
    )
    controller.attach_window(window)

    start_kwargs: dict[str, Any] = {"menu": build_server_menu(controller, menu_module=menu_module)}
    resolved_icon_path = app_icon_path if app_icon_path is not None else icon_path()
    if resolved_icon_path.exists():
        # pywebview icon support varies by backend/platform, so custom icons are optional.
        start_kwargs["icon"] = str(resolved_icon_path)

    entry_callable = _select_launch_entry(controller, override)

    try:
        webview.start(entry_callable, **start_kwargs)
    finally:
        bridge._stop_worker()


def _select_launch_entry(
    controller: ConnectionController,
    override: tuple[str, int] | None,
) -> Callable[[], Any]:
    """Return the nullary post-loop entry callable and log the chosen branch.

    An explicit CLI override connects straight to that target (the controller
    remembers it as a side effect of a successful connect); otherwise the
    controller auto-connects to last-used, or shows the connection screen on
    first run. pywebview's ``func`` is nullary, so the override branch is wrapped
    in a zero-argument closure.
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
    the window opens. An empty ``server_url`` (first run, nothing to target)
    makes the worker skip sending — the no-target behavior the worker guards.

    Uses MockWakewordWorker when --mock-wakeword is set or when openWakeWord
    cannot be imported. Returns the bridge instance (never None — the bridge
    always exists so the WebUI can query capabilities).
    """

    from desktop.wakeword.bridge import DesktopBridge
    from desktop.wakeword.worker import MockWakewordWorker, WakewordWorker

    use_mock = bool(args.mock_wakeword)

    def worker_factory(bridge: DesktopBridge) -> Any:
        if use_mock:
            return MockWakewordWorker(bridge=bridge)
        try:
            import sounddevice  # type: ignore[import-untyped]  # noqa: F401

            from desktop.wakeword.engine import OpenWakeWordEngine
        except ImportError:
            return MockWakewordWorker(bridge=bridge)

        wakeword_config = read_wakeword_settings(settings_file)
        engine = OpenWakeWordEngine(
            wake_phrase=wakeword_config.get("wake_phrase", "hey_jarvis"),
            sensitivity=wakeword_config.get("sensitivity", 0.5),
        )
        return WakewordWorker(
            engine=engine,
            bridge=bridge,
            settings_path=settings_file,
            server_url=server_url,
        )

    bridge = DesktopBridge(
        settings_path=settings_file,
        worker_factory=worker_factory,
        connection=controller,
    )

    wakeword_config = read_wakeword_settings(settings_file)
    if wakeword_config.get("enabled", False):
        bridge._start_worker()

    return bridge


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

    launch_desktop(argv)


def _parse_port(value: str) -> int:
    """Argparse adapter for Desktop port validation."""

    try:
        return validate_port(value, source="--port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


if __name__ == "__main__":
    main()
