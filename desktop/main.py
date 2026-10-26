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

Server targets and health probing live in :mod:`desktop.probe`, native window
placement and activation in :mod:`desktop.window`, and standalone logs in
:mod:`desktop._logging`. Server addresses and validation live in
:mod:`desktop.server_address`, sign-in in :mod:`desktop.access`.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from desktop import _http, _windows, probe, restart
from desktop import _logging as desktop_logging
from desktop import window as desktop_window
from desktop.server_address import (
    parse_server_address,
    validate_port,
)
from desktop.settings import (
    SETTINGS_FILE_NAME,
    config_dir,
    read_window_size,
    write_window_size,
)

if TYPE_CHECKING:
    from desktop.access import ServerAccess
    from desktop.connection import ConnectionController
    from desktop.page_events import PageEventDispatcher
    from desktop.speech.cues import CuePlayer
    from desktop.speech.microphone import MicrophoneService
    from desktop.wakeword.controller import VoiceController

logger = logging.getLogger("vbot.desktop")

WINDOW_TITLE = "vBot"
APP_BACKGROUND_COLOR = "#15130F"
DEFAULT_ICON_FILE_NAME = "icon.png"
WINDOWS_ICON_FILE_NAME = "icon.ico"
WEBVIEW_STORAGE_DIR_NAME = "webview"


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
        "--server-data-dir",
        type=Path,
        help=(
            "The data directory of the vBot server this installation runs. The "
            "Desktop then signs in to that server on this computer with its local "
            "access token instead of pairing."
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


def load_webview() -> desktop_window.WebviewModule:
    """Import pywebview lazily so non-desktop test gates do not require it."""

    _windows.configure_dpi()
    try:
        webview = importlib.import_module("webview")
        from desktop.bridge import configure_bridge_logging

        configure_bridge_logging()
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
    probe: Callable[[probe.DesktopTarget], probe.DesktopProbeResult] = probe.probe_target,
    webview_module: desktop_window.WebviewModule | None = None,
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
    (see :func:`desktop_window.activation_handler`).
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
        request=desktop_window.activation_request(override, session_link),
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
    session_link: desktop_window.SessionLink | None,
    instance: _windows.DesktopInstance,
    *,
    contract: restart.RelaunchContract | None = None,
    restart_request: restart.RestartRequest | None = None,
    desktop_config_directory: Path,
    settings_file: Path | None,
    probe: Callable[[probe.DesktopTarget], probe.DesktopProbeResult],
    webview_module: desktop_window.WebviewModule | None,
    app_icon_path: Path | None,
) -> None:
    """Create the window and its services for the instance that owns the Desktop."""

    from desktop.access import ServerAccess
    from desktop.bridge import DesktopBridge
    from desktop.connection import ConnectionController
    from desktop.credentials import DEVICE_TOKENS_FILE_NAME, DeviceTokenStore
    from desktop.dictation.controller import DictationController
    from desktop.execution import ComputerSharing
    from desktop.hotkey import LIVE_VOICE_HOTKEY, HotkeyController, HotkeyHandlers
    from desktop.page_events import PageEventDispatcher
    from desktop.page_permissions import PagePermissions
    from desktop.speech.cues import CuePlayer
    from desktop.speech.microphone import MicrophoneService
    from desktop.system_actions import DesktopSystemActions, guard_page_windows
    from desktop.zoom import DesktopZoom

    _http.prewarm_outbound_http()
    webview = webview_module if webview_module is not None else load_webview()

    # Signs the window and the native requests in to servers in the access mode
    # ``authenticated``: the installation's own server with its local access
    # token, any other with the device token stored when this Desktop paired.
    identities = DeviceTokenStore(desktop_config_directory / DEVICE_TOKENS_FILE_NAME)
    access = ServerAccess(
        store=identities,
        server_data_dir=args.server_data_dir,
    )
    controller = ConnectionController(settings_file=settings_file, probe=probe, access=access)
    server_url = _resolve_launch_server_url(override, controller)
    # Every remembered plain-HTTP server plus the launch target becomes a secure
    # context, so Live voice can open the microphone over plain HTTP on the LAN.
    # The list is fixed for this process; a server added later needs a restart.
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
    # One player for every Voice and dictation cue, so they play in order.
    cues = CuePlayer()
    voice = _create_voice(args, settings_file, microphone, server_url, page_events, cues, access)
    dictation = DictationController(
        settings_path=settings_file,
        microphone=microphone,
        server_url=server_url,
        page=page_events,
        wake_phrases=voice,
        cues=cues,
        authorization=access,
    )
    window_holder: list[Any] = []
    window_state = desktop_window.WindowState()

    def terminal_capability(_emit: Any) -> Any:
        from desktop.terminal_execution import TerminalCapability

        return TerminalCapability()

    def computer_capability(emit: Any) -> Any:
        from desktop.computer_execution import ComputerExecution

        return ComputerExecution(emit=emit)

    computer_sharing = ComputerSharing(
        settings_path=settings_file or desktop_config_directory / SETTINGS_FILE_NAME,
        identities=identities,
        authorization=access,
        factories={"terminal": terminal_capability, "computer_use": computer_capability},
        publish=page_events.publish_computer_sharing,
    )
    desktop_restart = (
        restart.DesktopRestart(
            contract,
            config_directory=desktop_config_directory,
            page=page_events,
            active_server=controller.active_target,
            location=lambda: desktop_window.page_location(window_holder, controller),
            placement=lambda: (
                window_state.placement(window_holder[0], settings_file) if window_holder else None
            ),
            shell_busy=lambda: voice.is_busy() or dictation.is_busy(),
            close_window=lambda: window_holder[0].destroy(),
            automatic_hold=lambda: computer_sharing.active_terminals > 0,
            prepare_restart=computer_sharing.prepare_restart,
            restart_failed=computer_sharing.resume,
        )
        if contract is not None
        else None
    )
    zoom = DesktopZoom(settings_path=settings_file)
    system_actions = DesktopSystemActions()

    def confirm(title: str, message: str) -> bool:
        # A native dialog the page cannot draw or answer; bridge calls run on their own thread.
        return bool(window_holder) and bool(
            window_holder[0].create_confirmation_dialog(title, message)
        )

    permissions = PagePermissions(
        page_url=lambda: desktop_window.current_page_url(window_holder),
        own_server=access.is_installation_server,
        confirm=confirm,
    )
    bridge = DesktopBridge(
        voice=voice,
        microphone=microphone,
        connection=controller,
        system_actions=system_actions,
        live_hotkey=live_hotkey,
        dictation=dictation,
        secure_origins=secure_origins,
        restart=desktop_restart,
        zoom=zoom,
        permissions=permissions,
        computer_sharing=computer_sharing,
        confirm=confirm,
    )

    # Voice and dictation follow the window: every successful in-window connect
    # retargets them, so first-run connect and runtime server switches never
    # leave them pointed at the launch-time (or empty) server.
    def follow_server(url: str) -> None:
        computer_sharing.set_server_url(url)
        voice.set_server_url(url)
        dictation.set_server_url(url)

    controller.set_active_server_listener(follow_server)
    controller.set_server_change_guard(lambda url: computer_sharing.changing_server(url, confirm))

    # The window must be created with initial content before the GUI loop; the
    # connection screen is a safe neutral page that the post-loop entry callable
    # replaces once the loop is live (navigating to the WebUI on connect).
    initial_html = controller.connection_screen_html()
    placement = restart_request.placement if restart_request is not None else None
    window_layout = desktop_window.resolve_window_layout(
        (placement.width, placement.height)
        if placement is not None
        else read_window_size(settings_file),
        desktop_window.primary_screen(webview),
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
    apply_zoom = _windows.bind_window_zoom(window, zoom.factor)
    if apply_zoom is not None:
        zoom.attach(apply_zoom)
    controller.attach_window(window)
    # Continues a window sign-in: ticket first, then the WebUI.
    window.events.loaded += controller.page_loaded
    window.events.loaded += permissions.page_loaded
    page_events.attach_window(window)
    instance.listen(
        desktop_window.activation_handler(
            controller,
            page_events,
            desktop_window.WindowFocus(window, window_state),
            desktop_restart,
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
    storage_path = desktop_config_directory / WEBVIEW_STORAGE_DIR_NAME
    _make_private_directory(storage_path)
    start_kwargs["storage_path"] = str(storage_path)
    # The other choice loads a page's new window in this window, bridge included.
    settings = getattr(webview, "settings", None)
    if isinstance(settings, dict):
        settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

    connection_entry = _select_launch_entry(
        controller,
        override,
        session_link,
        location=restart_request.location if restart_request is not None else None,
    )

    def start_visible_services() -> None:
        # The backend is loaded now; a page's new windows open as web links only,
        # in the default browser, before the window shows any server's page.
        if guard_page_windows(system_actions) == 0:
            logger.warning("Desktop found no pywebview backend to guard new windows in")
        # The lightweight shell must become visible before any network probe or
        # optional ML/audio initialization. Connect first so Voice follows the
        # window's resolved target; Voice starts listening only when enabled.
        connection_entry()
        computer_sharing.start()
        voice.start()
        live_hotkey.start()
        dictation.start()
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

    window.events.closing += lambda: computer_sharing.allow_close(confirm)
    window.events.closing += persist_window_size

    # WebView2 reads its browser arguments when webview.start creates it.
    _windows.apply_browser_arguments(secure_origins)
    try:
        webview.start(**start_kwargs)
    finally:
        # First, so a launch connect still waiting never retargets a closed Voice.
        controller.close()
        computer_sharing.close()
        if desktop_restart is not None:
            desktop_restart.close()
        live_hotkey.stop()
        dictation.stop()
        voice.close()
        cues.close()
        page_events.close()


def _make_private_directory(path: Path) -> None:
    """Create the WebView profile directory for the user alone on POSIX.

    It holds the WebUI's sign-in cookies. On Windows the per-user application
    data folder it lives in is private already.
    """
    if sys.platform == "win32":
        return
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError:
        logger.warning("The Desktop could not make %s private", path, exc_info=True)


def _launch_targets(
    controller: ConnectionController,
    override: tuple[str, int] | None,
) -> list[tuple[str, int]]:
    """Return every plain-HTTP server this launch may show: remembered ones plus the override.

    An HTTPS server is a secure context already and needs no WebView2 switch.
    """

    targets = [
        (entry.host, entry.port) for entry in controller.list_servers() if entry.scheme == "http"
    ]
    if override is not None:
        try:
            address = parse_server_address(*override)
        except ValueError:
            return targets
        if address.scheme == "http":
            targets.append((address.host, address.port))
    return targets


def _select_launch_entry(
    controller: ConnectionController,
    override: tuple[str, int] | None,
    session_link: desktop_window.SessionLink | None,
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
        logger.info("Desktop starting; auto-connecting to %s", launch_target.address.display)

    def auto_connect() -> Any:
        return controller.auto_connect(open_session=session_link)

    return auto_connect


def _create_voice(
    args: argparse.Namespace,
    settings_file: Path | None,
    microphone: MicrophoneService,
    server_url: str,
    page_events: PageEventDispatcher,
    cues: CuePlayer | None = None,
    access: ServerAccess | None = None,
) -> VoiceController:
    """Create Voice for the window's server; it starts listening on ``start()``.

    ``server_url`` is the *effective launch target* the caller resolved once
    (CLI override else last-used), so Voice sends commands to the server the
    window opens, signed in through ``access``. Mock mode is explicit through
    ``--mock-wakeword``. A missing on-device stack is detected lazily, when a
    listener first starts, and reported as the ``unavailable`` mode instead of
    simulated activity.
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
        cues=cues,
        authorization=access,
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
    effect. A missing half is filled from ``probe.DEFAULT_HOST`` / ``probe.DEFAULT_PORT``
    (acceptable because the user explicitly asked to launch at a specific
    target). With neither flag given, returns ``None`` and the launcher falls
    back to last-used auto-connect.
    """

    if args.host is None and args.port is None:
        return None
    host = args.host if args.host is not None else probe.DEFAULT_HOST
    port = args.port if args.port is not None else probe.DEFAULT_PORT
    return (host, port)


def _resolve_session_link(args: argparse.Namespace) -> desktop_window.SessionLink | None:
    """Return the ``--open-session`` request, if one was given."""

    if args.open_session is None:
        return None
    agent, session = args.open_session
    return desktop_window.SessionLink(agent=agent, session=session)


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
        host, port = entry.address.bridge_host, entry.port
    try:
        return probe.build_target_url(host, port)
    except ValueError:
        return ""


def main(argv: list[str] | None = None) -> None:
    """Open the vBot Desktop shell, routing target selection through the window."""

    log_handler = desktop_logging.configure_desktop_logging()
    desktop_logging.enable_desktop_crash_log()
    try:
        opened = launch_desktop(argv)
    except Exception:
        logger.error("Desktop stopped unexpectedly", exc_info=True)
        raise
    else:
        if opened:
            logger.info("Desktop stopped normally")
    finally:
        desktop_logging.close_desktop_logging(log_handler)


def _parse_port(value: str) -> int:
    """Argparse adapter for Desktop port validation."""

    try:
        return validate_port(value, source="--port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_session_link_part(value: str) -> str:
    """Argparse adapter for the Agent address and Session id of ``--open-session``."""

    if not desktop_window.is_session_link_part(value):
        raise argparse.ArgumentTypeError(
            "--open-session needs a non-empty Agent address and Session id without "
            f"spaces or control characters, at most {desktop_window.SESSION_LINK_PART_MAX_LENGTH} "
            "characters each"
        )
    return value


if __name__ == "__main__":
    main()
