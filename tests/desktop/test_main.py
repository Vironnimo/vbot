"""Desktop entry point: arguments, probing, window layout, and the whole launch."""

from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from desktop import connection as desktop_connection
from desktop import hotkey as desktop_hotkey
from desktop import main as desktop_main
from desktop import page_events as desktop_page_events
from desktop import restart as desktop_restart
from desktop.main import DesktopProbeResult, DesktopTarget

_TEST_DESKTOP_SESSION_ID = "desktop-test-session"
REPO_ROOT = Path(__file__).resolve().parents[2]


class _FixedUuid:
    hex = _TEST_DESKTOP_SESSION_ID


class FakeDesktopInstance:
    """Single-instance guard double owned by the launch under test."""

    def __init__(self) -> None:
        self.on_activate: Callable[[dict[str, Any] | None], None] | None = None
        self.closed = False

    def listen(self, on_activate: Callable[[dict[str, Any] | None], None]) -> None:
        self.on_activate = on_activate

    def close(self) -> None:
        self.closed = True


class FakeLiveHotkey:
    """Live hotkey double: tests must never register a real global hotkey."""

    supported = True

    def __init__(self, events: list[str], *, settings_path: Any, on_press: Callable[[], None]):
        self.events = events
        self.settings_path = settings_path
        self.on_press = on_press

    def start(self) -> None:
        self.events.append("hotkey.start")

    def stop(self) -> None:
        self.events.append("hotkey.stop")

    def status(self) -> dict[str, Any]:
        return {"supported": True, "enabled": False, "hotkey": {}, "error_code": None}

    def update(self, _changes: Any) -> dict[str, Any]:
        return self.status()


class LaunchSeams:
    """Records the Windows-native launch glue instead of touching the host."""

    def __init__(self) -> None:
        self.instance: FakeDesktopInstance | None = FakeDesktopInstance()
        self.claimed: list[Path] = []
        self.handed_over: list[dict[str, Any] | None] = []
        self.handoffs: list[str | None] = []
        self.browser_origins: list[tuple[str, ...]] = []
        self.secure_origin_targets: list[list[tuple[str, int]]] = []
        self.microphone_origin: Callable[[], str | None] | None = None
        self.events: list[str] = []
        self.hotkeys: list[FakeLiveHotkey] = []

    def hotkey(self, **kwargs: Any) -> FakeLiveHotkey:
        hotkey = FakeLiveHotkey(self.events, **kwargs)
        self.hotkeys.append(hotkey)
        return hotkey

    def claim(
        self,
        config_directory: Path,
        *,
        request: dict[str, Any] | None = None,
        handoff: str | None = None,
    ) -> FakeDesktopInstance | None:
        self.claimed.append(config_directory)
        self.handed_over.append(request)
        self.handoffs.append(handoff)
        return self.instance

    def secure_origins(self, targets: Any) -> tuple[str, ...]:
        targets = list(targets)
        self.secure_origin_targets.append(targets)
        return tuple(f"http://{host}:{port}" for host, port in targets)

    def apply(self, origins: Any) -> None:
        self.events.append("apply_browser_arguments")
        self.browser_origins.append(tuple(origins))

    def allow_microphone(self, _window: Any, origin: Callable[[], str | None]) -> None:
        self.microphone_origin = origin


@pytest.fixture(autouse=True)
def launch_seams(monkeypatch: pytest.MonkeyPatch) -> LaunchSeams:
    """Keep Desktop navigation expectations deterministic and the host untouched."""

    seams = LaunchSeams()
    monkeypatch.setattr(desktop_connection, "uuid4", lambda: _FixedUuid())
    monkeypatch.setattr(desktop_main._windows, "primary_scale", lambda: 1.0)
    monkeypatch.setattr(desktop_main._windows, "bind_window_dpi", lambda *_: None)
    monkeypatch.setattr(desktop_main._windows, "claim_desktop_instance", seams.claim)
    monkeypatch.setattr(desktop_main._windows, "webview_secure_origins", seams.secure_origins)
    monkeypatch.setattr(desktop_main._windows, "apply_browser_arguments", seams.apply)
    monkeypatch.setattr(desktop_main._windows, "allow_server_microphone", seams.allow_microphone)
    monkeypatch.setattr(desktop_hotkey, "LiveHotkeyController", seams.hotkey)
    return seams


@dataclass
class FakeResponse:
    status_code: int
    payload: Any = None

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeWindow:
    """Live-window double recording the navigation the controller drives."""

    def __init__(self) -> None:
        self.loaded_urls: list[str] = []
        self.loaded_html: list[str] = []
        self.width = 1280
        self.height = 800
        self.events = FakeWindowEvents()
        self.focus_calls: list[str] = []

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def load_html(self, content: str) -> None:
        self.loaded_html.append(content)

    def evaluate_js(self, _script: str) -> bool:
        return True

    def maximize(self) -> None:
        self.focus_calls.append("maximize")

    def restore(self) -> None:
        self.focus_calls.append("restore")

    def show(self) -> None:
        self.focus_calls.append("show")


class FakeEvent:
    """Small pywebview Event double supporting handler registration and emission."""

    def __init__(self) -> None:
        self.handlers: list[Callable[[], Any]] = []

    def __iadd__(self, handler: Callable[[], Any]) -> FakeEvent:
        self.handlers.append(handler)
        return self

    def emit(self) -> None:
        for handler in self.handlers:
            handler()


class FakeWindowEvents:
    def __init__(self) -> None:
        self.before_show = FakeEvent()
        self.shown = FakeEvent()
        self.closing = FakeEvent()
        self.minimized = FakeEvent()
        self.maximized = FakeEvent()
        self.restored = FakeEvent()


@dataclass
class FakeFrame:
    """Minimal work-area rectangle double (Windows PascalCase convention)."""

    X: int = 0
    Y: int = 0
    Width: int = 1600
    Height: int = 1000


@dataclass
class FakeScreen:
    """pywebview Screen double with origin, scale, and optional work area."""

    width: int = 1600
    height: int = 1000
    x: int = 0
    y: int = 0
    scale: float = 1.0
    frame: Any = None

    def __post_init__(self) -> None:
        if self.frame is None:
            self.frame = FakeFrame(Width=self.width, Height=self.height)


class FakeWebview:
    """pywebview double honoring the create-before-start / shown-event order.

    ``create_window`` returns the :class:`FakeWindow` the controller later
    navigates; ``start`` emits its ``shown`` event exactly where pywebview makes
    the native window visible, so tests exercise the real startup boundary
    without a GUI.
    """

    def __init__(self) -> None:
        self.created_windows: list[tuple[str, dict[str, Any]]] = []
        self.window = FakeWindow()
        self.screens: list[Any] = [FakeScreen()]
        self.start_calls: list[dict[str, Any]] = []
        self.start_func: Callable[[], Any] | None = None
        self.launch_events: list[str] | None = None

    def create_window(self, title: str, **kwargs: Any) -> FakeWindow:
        self.created_windows.append((title, kwargs))
        self.window.width = kwargs["width"]
        self.window.height = kwargs["height"]
        return self.window

    def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
        self.start_calls.append(kwargs)
        self.start_func = func
        if self.launch_events is not None:
            self.launch_events.append("webview.start")
        if func is not None:
            func()
        self.window.events.shown.emit()


class StartRaisesWebview(FakeWebview):
    def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
        raise RuntimeError("gui loop crashed")


class RecordingVoice:
    """Voice double recording its lifecycle calls during a launch."""

    def __init__(self, events: list[str], on_start: Callable[[], None] | None = None) -> None:
        self.events = events
        self.on_start = on_start
        self.server_urls: list[str] = []

    def start(self) -> None:
        if self.on_start is not None:
            self.on_start()
        self.events.append("voice.start")

    def close(self) -> None:
        self.events.append("voice.close")

    def set_server_url(self, server_url: str) -> None:
        self.server_urls.append(server_url)


def _available(target: DesktopTarget) -> DesktopProbeResult:
    return DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target)


def _launch(
    tmp_path: Path,
    argv: Sequence[str] = (),
    *,
    webview: FakeWebview | None = None,
    settings: dict[str, Any] | None = None,
    probe: Callable[[DesktopTarget], DesktopProbeResult] = _available,
    icon: Path | None = None,
) -> FakeWebview:
    """Run a whole launch that owns the Desktop instance against a fake pywebview."""

    settings_file = tmp_path / "settings.json"
    if settings is not None:
        settings_file.write_text(json.dumps(settings), encoding="utf-8")
    fake_webview = webview if webview is not None else FakeWebview()
    opened = desktop_main.launch_desktop(
        list(argv),
        settings_file=settings_file,
        probe=probe,
        webview_module=fake_webview,
        app_icon_path=icon if icon is not None else tmp_path / "missing-icon.png",
    )
    assert opened is True
    return fake_webview


def _bridge(fake_webview: FakeWebview) -> Any:
    return fake_webview.created_windows[0][1]["js_api"]


def _webui_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/?accessor=desktop&desktop_session={_TEST_DESKTOP_SESSION_ID}"


def _stored(tmp_path: Path) -> dict[str, Any]:
    stored: dict[str, Any] = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    return stored


SAVED_PI = {"servers": [{"host": "pi.lan", "port": 9000}]}


# -- Arguments and entry point -------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "host", "port", "open_session", "mock_wakeword"),
    [
        ([], None, None, None, False),
        (["--host", "192.168.1.50", "--port", "9000"], "192.168.1.50", 9000, None, False),
        (
            ["--open-session", "builder@project", "session-1", "--port", "9000"],
            None,
            9000,
            ["builder@project", "session-1"],
            False,
        ),
        (["--mock-wakeword"], None, None, None, True),
    ],
)
def test_parse_args_reads_the_target_the_session_and_the_mock_wakeword_flag(
    argv: list[str],
    host: str | None,
    port: int | None,
    open_session: list[str] | None,
    mock_wakeword: bool,
) -> None:
    args = desktop_main.parse_args(argv)

    assert (args.host, args.port, args.open_session, args.mock_wakeword) == (
        host,
        port,
        open_session,
        mock_wakeword,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--port", "0"],
        ["--port", "65536"],
        ["--port", "not-a-port"],
        ["--open-session", "builder"],
        ["--open-session", "", "session-1"],
        ["--open-session", "builder", "session 1"],
        ["--open-session", "builder", "x" * 513],
    ],
)
def test_parse_args_rejects_invalid_values(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        desktop_main.parse_args(argv)


@pytest.mark.parametrize(("opened", "records"), [(True, 1), (False, 0)])
def test_main_reports_a_normal_shutdown_only_after_its_own_window(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    opened: bool,
    records: int,
) -> None:
    """A launch that only focused an already running Desktop stops silently."""

    monkeypatch.setattr(desktop_main, "configure_desktop_logging", lambda: None)
    monkeypatch.setattr(desktop_main, "close_desktop_logging", lambda _handler: None)
    monkeypatch.setattr(desktop_main, "launch_desktop", lambda _argv: opened)

    with caplog.at_level("INFO", logger="vbot.desktop"):
        desktop_main.main([])

    assert len([record for record in caplog.records if record.name == "vbot.desktop"]) == records


def test_desktop_logging_writes_structured_daily_file(tmp_path: Path) -> None:
    handler = desktop_main.configure_desktop_logging(tmp_path)
    assert handler is not None
    try:
        logging.getLogger("vbot.desktop.wakeword.controller").warning(
            "Voice state: error (speech_to_text_unconfigured)"
        )
        handler.flush()

        log_files = list((tmp_path / "logs").glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text(encoding="utf-8")
        assert "[WARN] vbot.desktop.wakeword.controller" in content
        assert "error (speech_to_text_unconfigured)" in content
    finally:
        desktop_main.close_desktop_logging(handler)


def test_icon_path_selects_the_platform_native_asset(tmp_path: Path) -> None:
    assert desktop_main.icon_path(tmp_path, platform="win32") == tmp_path / "icon.ico"
    assert desktop_main.icon_path(tmp_path, platform="linux") == tmp_path / "icon.png"
    assert desktop_main.icon_path(tmp_path, platform="darwin") == tmp_path / "icon.png"


def test_bundled_windows_icon_is_a_multiresolution_ico() -> None:
    icon_data = desktop_main.icon_path(platform="win32").read_bytes()

    assert icon_data[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(icon_data[4:6], byteorder="little") > 1


# -- Package boundaries ----------------------------------------------------------


def _imported_packages(path: Path) -> set[str]:
    packages: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            packages.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            packages.add(node.module.split(".")[0])
    return packages


def test_desktop_stays_a_thin_client_of_the_server() -> None:
    """Desktop imports no server, core, or CLI code and never manages server processes."""

    modules = sorted((REPO_ROOT / "desktop").rglob("*.py"))
    assert len(modules) > 10

    for module in modules:
        assert not _imported_packages(module) & {"server", "core", "cli"}, module
        source = module.read_text(encoding="utf-8").lower()
        for verb in ("start", "stop", "restart"):
            assert not re.search(rf"\bserver {verb}\b", source), module


def test_disabled_voice_builds_its_bridge_without_loading_the_audio_stack(
    tmp_path: Path,
) -> None:
    """Only a fresh process shows which modules Desktop startup imports."""

    script = """
import sys
from pathlib import Path
from desktop.bridge import DesktopBridge
from desktop.connection import ConnectionController
from desktop.main import _create_voice, parse_args
from desktop.page_events import PageEventDispatcher

settings = Path(sys.argv[1]) / 'settings.json'
controller = ConnectionController(settings_file=settings)
page_events = PageEventDispatcher()
voice = _create_voice(parse_args([]), settings, '', page_events)
bridge = DesktopBridge(voice=voice, connection=controller)
voice.start()
assert bridge.getDesktopCapabilities()['voiceApi'] == 2
assert bridge.getVoiceStatus()['state'] == 'off'
voice.close()
page_events.close()
audio_modules = {
    'desktop.wakeword.capture',
    'desktop.wakeword.detection',
    'desktop.wakeword.commands',
    'desktop.wakeword.echo',
    'desktop.wakeword._speech_detection',
}
assert not audio_modules & sys.modules.keys(), audio_modules & sys.modules.keys()
heavy = {'numpy', 'sounddevice', 'pyopen_wakeword', 'onnxruntime', 'soxr', 'livekit'}
assert not heavy & sys.modules.keys(), heavy & sys.modules.keys()

# The package still exposes Voice when a caller actually needs it.
from desktop.wakeword import VoiceController
assert VoiceController.__module__ == 'desktop.wakeword.controller'
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("missing_module", [None, "pyopen_wakeword", "sounddevice", "soxr"])
def test_real_wakeword_availability_requires_the_complete_voice_stack(
    monkeypatch: pytest.MonkeyPatch, missing_module: str | None
) -> None:
    for module_name in ("pyopen_wakeword", "sounddevice", "soxr"):
        monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
    if missing_module is not None:
        monkeypatch.setitem(sys.modules, missing_module, None)

    assert desktop_main._real_wakeword_available() is (missing_module is None)


# -- Probe classification --------------------------------------------------------

PROBE_TARGET = DesktopTarget("vbot.lan", 9000, "http://vbot.lan:9000/")
HEALTH_URL = "http://vbot.lan:9000/health"
ROOT_URL = "http://vbot.lan:9000/"
HEALTH_OK = FakeResponse(200, {"status": "ok"})


@pytest.mark.parametrize(
    ("health", "root", "status"),
    [
        (HEALTH_OK, FakeResponse(200), desktop_main.PROBE_WEBUI_AVAILABLE),
        (HEALTH_OK, FakeResponse(399), desktop_main.PROBE_WEBUI_AVAILABLE),
        (HEALTH_OK, FakeResponse(400), desktop_main.PROBE_WEBUI_UNAVAILABLE),
        (HEALTH_OK, FakeResponse(500), desktop_main.PROBE_WEBUI_UNAVAILABLE),
        (HEALTH_OK, httpx.ConnectError("closed"), desktop_main.PROBE_WEBUI_UNAVAILABLE),
        (httpx.ConnectError("refused"), None, desktop_main.PROBE_SERVER_UNREACHABLE),
        (FakeResponse(503, {"status": "ok"}), None, desktop_main.PROBE_NOT_VBOT_SERVER),
        (FakeResponse(200, {"status": "starting"}), None, desktop_main.PROBE_NOT_VBOT_SERVER),
        (
            FakeResponse(200, {"status": "ok", "version": "dev"}),
            None,
            desktop_main.PROBE_NOT_VBOT_SERVER,
        ),
        (FakeResponse(200, ValueError("no json")), None, desktop_main.PROBE_NOT_VBOT_SERVER),
        (FakeResponse(200, ["ok"]), None, desktop_main.PROBE_NOT_VBOT_SERVER),
    ],
    ids=[
        "webui-200",
        "webui-399",
        "webui-400",
        "webui-500",
        "webui-request-error",
        "health-request-error",
        "health-503",
        "health-starting",
        "health-extra-keys",
        "health-not-json",
        "health-not-object",
    ],
)
def test_probe_target_classifies_the_server_with_one_request_per_step(
    health: FakeResponse | httpx.RequestError,
    root: FakeResponse | httpx.RequestError | None,
    status: str,
) -> None:
    replies = {HEALTH_URL: health, ROOT_URL: root}
    requested: list[str] = []

    def get(url: str, *, timeout: float, trust_env: bool) -> FakeResponse:
        assert (timeout, trust_env) == (desktop_main.PROBE_TIMEOUT_SECONDS, False)
        requested.append(url)
        reply = replies[url]
        assert reply is not None, f"unexpected request: {url}"
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = desktop_main.probe_target(PROBE_TARGET, get=get)

    assert result.status == status
    # No retry loop: /health once, then the WebUI root once when /health is vBot's.
    assert requested == ([HEALTH_URL] if root is None else [HEALTH_URL, ROOT_URL])


def test_probe_target_reports_a_configuration_error_without_a_request() -> None:
    def get(url: str, *, timeout: float, trust_env: bool) -> FakeResponse:
        raise AssertionError(f"unexpected request: {url}")

    target = DesktopTarget("bad host", 8420, "", configuration_error="bad host")

    assert desktop_main.probe_target(target, get=get).status == desktop_main.PROBE_INVALID_TARGET


@pytest.mark.parametrize("root_fails", [False, True])
def test_probe_reuses_one_unproxied_client_and_closes_it(
    monkeypatch: pytest.MonkeyPatch, root_fails: bool
) -> None:
    clients: list[httpx.Client] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if root_fails:
            raise httpx.ConnectError("test transport unavailable", request=request)
        return httpx.Response(200, text="test WebUI")

    class ProbeClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["trust_env"] is False
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(self)

    monkeypatch.setattr(desktop_main.httpx, "Client", ProbeClient)
    target = DesktopTarget("vbot.test", 8420, "http://vbot.test:8420/")
    result = desktop_main.probe_target(target, timeout=0.75)

    assert result.status == (
        desktop_main.PROBE_WEBUI_UNAVAILABLE if root_fails else desktop_main.PROBE_WEBUI_AVAILABLE
    )
    assert len(clients) == 1
    assert clients[0].is_closed
    assert [request.url.path for request in requests] == ["/health", "/"]
    assert all(request.extensions["timeout"]["connect"] == 0.75 for request in requests)


# -- Host/port validation and URL building ---------------------------------------


@pytest.mark.parametrize("host", ["", "   ", "http://localhost", "bad host", "host/path", None])
def test_validate_host_rejects_non_host_values_naming_their_source(host: object) -> None:
    with pytest.raises(ValueError, match="^settings.host "):
        desktop_main.validate_host(host, source="settings.host")


@pytest.mark.parametrize("port", [0, 65536, "not-a-port", None])
def test_validate_port_rejects_out_of_range_and_non_numeric(port: object) -> None:
    with pytest.raises(ValueError):
        desktop_main.validate_port(port)


def test_build_target_url_formats_local_and_lan_targets_as_plain_http() -> None:
    assert desktop_main.build_target_url("127.0.0.1", 8420) == "http://127.0.0.1:8420/"
    assert desktop_main.build_target_url("192.168.1.44", 9000) == "http://192.168.1.44:9000/"
    assert desktop_main.build_target_url("vbot.lan", 8500) == "http://vbot.lan:8500/"


# -- Window layout -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("saved_size", "screen", "size"),
    [
        (None, FakeScreen(1920, 1080), (1440, 864)),
        ((1380, 900), FakeScreen(1920, 1080), (1380, 900)),
        ((1800, 1100), FakeScreen(1280, 720), (1280, 720)),
        (
            (2048, 1152),
            FakeScreen(2048, 1152, frame=FakeFrame(Width=2048, Height=1104)),
            (2048, 1104),
        ),
        (None, None, (1280, 800)),
    ],
    ids=["first-run", "remembered-fits", "smaller-screen", "taskbar-work-area", "no-screen"],
)
def test_resolve_window_layout_bounds_the_size_to_the_primary_work_area(
    saved_size: tuple[int, int] | None, screen: FakeScreen | None, size: tuple[int, int]
) -> None:
    layout = desktop_main.resolve_window_layout(saved_size, screen)

    assert (layout.width, layout.height) == size
    assert (layout.minimum_width, layout.minimum_height) == (800, 600)
    assert layout.screen is screen


@pytest.mark.parametrize(
    ("screens", "placed_on"),
    [
        ([FakeScreen(1920, 1080, x=-1920, y=270), FakeScreen(2048, 1152)], 1),
        ([FakeScreen(1920, 1080, x=-1920)], 0),
        ([], None),
    ],
    ids=["primary-holds-the-origin", "first-screen-fallback", "no-screens"],
)
def test_launch_places_the_window_on_the_primary_screen(
    tmp_path: Path, screens: list[FakeScreen], placed_on: int | None
) -> None:
    fake_webview = FakeWebview()
    fake_webview.screens = screens

    _launch(tmp_path, webview=fake_webview)

    screen = fake_webview.created_windows[0][1]["screen"]
    assert screen is (None if placed_on is None else screens[placed_on])


@pytest.mark.parametrize("scale", [1.25, 2.0])
def test_launch_converts_a_physical_primary_screen_to_logical_pixels_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scale: float
) -> None:
    monkeypatch.setattr(desktop_main._windows, "primary_scale", lambda: scale)
    physical = types.SimpleNamespace(
        x=0,
        y=0,
        width=2560,
        height=1440,
        scale=1.0,
        frame=types.SimpleNamespace(X=0, Y=0, Width=2560, Height=1380),
    )
    fake_webview = FakeWebview()
    fake_webview.screens = [physical]

    _launch(tmp_path, webview=fake_webview, settings={"window": {"width": 3000, "height": 2000}})

    kwargs = fake_webview.created_windows[0][1]
    screen = kwargs["screen"]
    assert (screen.width, screen.height, screen.scale) == (
        int(2560 / scale),
        int(1440 / scale),
        scale,
    )
    assert kwargs["width"] * scale <= 2560
    assert kwargs["height"] * scale <= 1380
    assert (physical.width, physical.frame.Height) == (2560, 1380)


# -- Launch --------------------------------------------------------------------------


@pytest.mark.parametrize("icon_exists", [False, True])
def test_launch_creates_the_window_before_one_gui_loop(tmp_path: Path, icon_exists: bool) -> None:
    icon = tmp_path / "icon.ico"
    if icon_exists:
        icon.write_bytes(b"fake-icon")

    fake_webview = _launch(tmp_path, icon=icon)

    [(title, kwargs)] = fake_webview.created_windows
    assert title == desktop_main.WINDOW_TITLE
    # The window opens on the neutral connection screen (no URL before the loop),
    # and the same bridge object is its single js_api for both screen and WebUI.
    assert "url" not in kwargs
    assert 'id="connect-form"' in kwargs["html"]
    assert kwargs["background_color"] == desktop_main.APP_BACKGROUND_COLOR
    assert kwargs["text_select"] is True
    assert (kwargs["width"], kwargs["height"], kwargs["min_size"]) == (1280, 800, (800, 600))
    assert kwargs["screen"] is fake_webview.screens[0]
    assert hasattr(kwargs["js_api"], "connect")
    assert hasattr(kwargs["js_api"], "getVoiceStatus")
    # No native menu; the WebView2 profile survives restarts beside the settings
    # file (pywebview's private mode would delete it); the icon is optional.
    [start] = fake_webview.start_calls
    assert "menu" not in start
    assert start["private_mode"] is False
    assert start["storage_path"] == str(tmp_path / desktop_main.WEBVIEW_STORAGE_DIR_NAME)
    assert start.get("icon") == (str(icon) if icon_exists else None)


def test_launch_restores_and_persists_the_window_size(tmp_path: Path) -> None:
    fake_webview = _launch(tmp_path, settings={"window": {"width": 1400, "height": 900}})
    _, kwargs = fake_webview.created_windows[0]
    assert (kwargs["width"], kwargs["height"]) == (1400, 900)

    fake_webview.window.width, fake_webview.window.height = 1500, 920
    fake_webview.window.events.closing.emit()

    assert _stored(tmp_path)["window"] == {"width": 1500, "height": 920}


def test_launch_connects_the_created_window_to_the_saved_server_once_shown(
    tmp_path: Path,
) -> None:
    fake_webview = _launch(tmp_path, settings=SAVED_PI)

    # start() received no eager callback: the shown event navigated the window.
    assert fake_webview.start_func is None
    assert len(fake_webview.window.events.shown.handlers) == 1
    assert fake_webview.window.loaded_urls == [_webui_url("pi.lan", 9000)]


def test_first_launch_shows_the_connection_screen_without_probing_localhost(
    tmp_path: Path,
) -> None:
    probed: list[DesktopTarget] = []

    def record_probe(target: DesktopTarget) -> DesktopProbeResult:
        probed.append(target)
        return _available(target)

    fake_webview = _launch(tmp_path, probe=record_probe)

    assert probed == []
    assert fake_webview.window.loaded_urls == []
    [html] = fake_webview.window.loaded_html
    assert 'id="connect-form"' in html


@pytest.mark.parametrize(
    ("argv", "saved", "target"),
    [
        (["--host", "pi.lan", "--port", "9000"], None, ("pi.lan", 9000)),
        (["--port", "9000"], None, (desktop_main.DEFAULT_HOST, 9000)),
        (["--host", "pi.lan"], None, ("pi.lan", desktop_main.DEFAULT_PORT)),
        (
            ["--host", "new.lan", "--port", "9000"],
            {
                "servers": [{"host": "old.lan", "port": 8420}],
                "last_used": {"host": "old.lan", "port": 8420},
            },
            ("new.lan", 9000),
        ),
    ],
    ids=["first-run", "port-only", "host-only", "over-last-used"],
)
def test_a_target_override_connects_directly_and_becomes_last_used(
    tmp_path: Path,
    argv: list[str],
    saved: dict[str, Any] | None,
    target: tuple[str, int],
) -> None:
    fake_webview = _launch(tmp_path, argv, settings=saved)

    host, port = target
    assert fake_webview.window.loaded_urls == [_webui_url(host, port)]
    assert fake_webview.window.loaded_html == []
    stored = _stored(tmp_path)
    assert stored["last_used"] == {"host": host, "port": port}
    assert {"host": host, "port": port} in stored["servers"]
    assert len(stored["servers"]) == (1 if saved is None else 2)


@pytest.mark.parametrize(
    ("argv", "saved"),
    [([], SAVED_PI), (["--host", "pi.lan", "--port", "9000"], None)],
    ids=["last-used", "override"],
)
def test_closing_the_window_ends_a_launch_connect_still_waiting_for_its_server(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    argv: list[str],
    saved: dict[str, Any] | None,
) -> None:
    def unreachable(target: DesktopTarget) -> DesktopProbeResult:
        return DesktopProbeResult(desktop_main.PROBE_SERVER_UNREACHABLE, target)

    with caplog.at_level("INFO", logger="vbot.desktop.connection"):
        fake_webview = _launch(tmp_path, argv, settings=saved, probe=unreachable)

    # The shown window waited for the server until it closed, then stopped.
    [page] = fake_webview.window.loaded_html
    assert 'id="connection-waiting"' in page
    assert fake_webview.window.loaded_urls == []
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name == "vbot-desktop-launch-wait" and thread.is_alive()
    ]
    assert "reason=closed" in caplog.text


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["--open-session", "builder@project", "session-1"], ("pi.lan", 9000)),
        (
            ["--host", "nas.lan", "--port", "8420", "--open-session", "builder@project", "s-1"],
            ("nas.lan", 8420),
        ),
    ],
    ids=["last-used", "override"],
)
def test_a_requested_session_rides_on_the_first_navigation(
    tmp_path: Path, argv: list[str], target: tuple[str, int]
) -> None:
    fake_webview = _launch(tmp_path, argv, settings=SAVED_PI)

    session = argv[-1]
    assert fake_webview.window.loaded_urls == [
        f"{_webui_url(*target)}&open_agent=builder%40project&open_session={session}"
    ]


# -- Voice during a launch ------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "saved", "server_url"),
    [
        (["--host", "pi.lan", "--port", "9000"], SAVED_PI, "http://pi.lan:9000/"),
        ([], SAVED_PI, "http://pi.lan:9000/"),
        ([], None, ""),
    ],
    ids=["override", "last-used", "nothing-saved"],
)
def test_voice_starts_after_the_window_is_shown_and_follows_its_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    saved: dict[str, Any] | None,
    server_url: str,
) -> None:
    fake_webview = FakeWebview()
    events: list[str] = []
    created_for: list[str] = []

    def window_shown_first() -> None:
        assert len(fake_webview.created_windows) == 1
        assert fake_webview.window.loaded_urls or fake_webview.window.loaded_html

    voice = RecordingVoice(events, on_start=window_shown_first)

    def create_voice(_args: Any, _settings: Any, url: str, _page_events: Any) -> RecordingVoice:
        created_for.append(url)
        return voice

    monkeypatch.setattr(desktop_main, "_create_voice", create_voice)

    _launch(tmp_path, argv, webview=fake_webview, settings=saved)

    assert created_for == [server_url]
    assert events == ["voice.start", "voice.close"]
    # Every successful in-window connect retargets Voice.
    assert voice.server_urls == ([server_url] if server_url else [])


def test_a_failing_gui_loop_closes_voice_hotkey_and_instance_without_starting_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_seams: LaunchSeams
) -> None:
    voice_events: list[str] = []
    monkeypatch.setattr(desktop_main, "_create_voice", lambda *_args: RecordingVoice(voice_events))

    with pytest.raises(RuntimeError, match="gui loop crashed"):
        _launch(tmp_path, webview=StartRaisesWebview())

    assert voice_events == ["voice.close"]
    assert launch_seams.instance is not None
    assert launch_seams.instance.closed is True
    assert launch_seams.events == ["apply_browser_arguments", "hotkey.stop"]


def test_launch_with_disabled_voice_never_probes_wakeword_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_probed() -> bool:
        raise AssertionError("Voice dependencies must stay lazy while Voice is disabled")

    monkeypatch.setattr(desktop_main, "_real_wakeword_available", fail_if_probed)
    monkeypatch.setitem(sys.modules, "desktop.wakeword.capture", None)
    monkeypatch.setitem(sys.modules, "desktop.wakeword.echo", None)

    _launch(tmp_path)


def test_enabled_voice_probes_dependencies_only_after_window_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed = threading.Event()

    class RunningWebview(FakeWebview):
        """Keeps the GUI loop running until Voice reported its start result."""

        def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
            super().start(func, **kwargs)
            bridge = _bridge(self)
            deadline = time.monotonic() + 5
            while bridge.getVoiceStatus()["state"] != "error" and time.monotonic() < deadline:
                time.sleep(0.01)

    fake_webview = RunningWebview()

    def unavailable_after_window_created() -> bool:
        assert len(fake_webview.created_windows) == 1
        probed.set()
        return False

    monkeypatch.setattr(desktop_main, "_real_wakeword_available", unavailable_after_window_created)

    _launch(tmp_path, webview=fake_webview, settings={"wakeword": {"enabled": True}})

    assert probed.is_set()
    assert _bridge(fake_webview).getVoiceStatus()["mode"] == "unavailable"


def test_mock_wakeword_flag_selects_the_mock_voice_mode(tmp_path: Path) -> None:
    fake_webview = _launch(tmp_path, ["--mock-wakeword"])

    assert _bridge(fake_webview).getVoiceStatus()["mode"] == "mock"


# -- Single instance, browser arguments, and Live voice ------------------------------


SESSION_REQUEST = {"agent": "builder@project", "session": "session-1"}


@pytest.mark.parametrize(
    ("argv", "handed_over", "logged"),
    [
        (
            ["--host", "pi.lan", "--port", "9000"],
            None,
            "ignored the requested target pi.lan:9000",
        ),
        (
            ["--open-session", "builder@project", "session-1"],
            {"host": None, "port": None, **SESSION_REQUEST},
            "handed over Session session-1 of builder@project",
        ),
        (
            ["--port", "9000", "--open-session", "builder@project", "session-1"],
            {"host": desktop_main.DEFAULT_HOST, "port": 9000, **SESSION_REQUEST},
            "handed over Session session-1 of builder@project",
        ),
    ],
    ids=["target", "session", "session-on-target"],
)
def test_second_launch_focuses_the_running_desktop_without_a_window(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    caplog: pytest.LogCaptureFixture,
    argv: list[str],
    handed_over: dict[str, Any] | None,
    logged: str,
) -> None:
    launch_seams.instance = None
    fake_webview = FakeWebview()

    with caplog.at_level("INFO", logger="vbot.desktop"):
        opened = desktop_main.launch_desktop(
            argv,
            settings_file=tmp_path / "settings.json",
            probe=_available,
            webview_module=fake_webview,
            app_icon_path=tmp_path / "missing-icon.png",
        )

    assert opened is False
    assert launch_seams.claimed == [tmp_path]
    assert launch_seams.handed_over == [handed_over]
    assert fake_webview.created_windows == []
    assert fake_webview.start_calls == []
    assert launch_seams.browser_origins == []
    assert not (tmp_path / "settings.json").exists()
    assert logged in caplog.text


@pytest.mark.parametrize(
    ("window_events", "focus_calls"),
    [
        ([], ["show"]),
        (["minimized"], ["restore", "show"]),
        (["maximized", "minimized"], ["maximize", "show"]),
        (["maximized", "minimized", "restored"], ["show"]),
        (["minimized", "maximized"], ["show"]),
    ],
)
def test_a_second_launch_brings_the_window_back_at_its_previous_size(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    window_events: list[str],
    focus_calls: list[str],
) -> None:
    fake_webview = _launch(tmp_path)
    instance = launch_seams.instance
    assert instance is not None
    assert instance.closed is True  # released when the window closed
    assert instance.on_activate is not None

    for name in window_events:
        getattr(fake_webview.window.events, name).emit()
    instance.on_activate(None)

    assert fake_webview.window.focus_calls == focus_calls


def test_launch_makes_every_known_server_a_secure_origin_before_webview_starts(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    fake_webview = FakeWebview()
    fake_webview.launch_events = launch_seams.events

    _launch(
        tmp_path,
        ["--host", "pi.lan", "--port", "9000"],
        webview=fake_webview,
        settings={"servers": [{"host": "a.lan", "port": 8420}]},
    )

    assert launch_seams.secure_origin_targets == [[("a.lan", 8420), ("pi.lan", 9000)]]
    assert launch_seams.browser_origins == [("http://a.lan:8420", "http://pi.lan:9000")]
    assert launch_seams.events[:2] == ["apply_browser_arguments", "webview.start"]
    assert _bridge(fake_webview).getDesktopCapabilities()["secureOrigins"] == [
        "http://a.lan:8420",
        "http://pi.lan:9000",
    ]


@pytest.mark.parametrize(
    ("argv", "origin"),
    [(["--host", "pi.lan", "--port", "9000"], "http://pi.lan:9000"), ([], None)],
    ids=["connected", "connection-screen"],
)
def test_microphone_permission_follows_the_connected_server(
    tmp_path: Path, launch_seams: LaunchSeams, argv: list[str], origin: str | None
) -> None:
    _launch(tmp_path, argv)

    assert launch_seams.microphone_origin is not None
    assert launch_seams.microphone_origin() == origin


def test_live_hotkey_runs_only_while_the_window_is_shown(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    fake_webview = FakeWebview()
    fake_webview.launch_events = launch_seams.events

    _launch(tmp_path, webview=fake_webview)

    assert launch_seams.events == [
        "apply_browser_arguments",
        "webview.start",
        "hotkey.start",
        "hotkey.stop",
    ]
    assert launch_seams.hotkeys[0].settings_path == tmp_path / "settings.json"
    assert _bridge(fake_webview).getDesktopCapabilities()["liveHotkey"] is True


class RecordingDispatcher:
    """Page event double recording the pushes a launch asks for."""

    def __init__(self) -> None:
        self.window: Any = None
        self.requests: list[tuple[str, str]] = []
        self.opened_sessions: list[tuple[str, str]] = []
        self.closed = False

    def attach_window(self, window: Any) -> None:
        self.window = window

    def request_live(self, action: str, source: str) -> None:
        self.requests.append((action, source))

    def request_open_session(self, agent: str, session: str) -> None:
        self.opened_sessions.append((agent, session))

    def publish_status(self, _status: Any) -> None:
        pass

    def publish_event(self, _event: Any) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _record_page_events(monkeypatch: pytest.MonkeyPatch) -> list[RecordingDispatcher]:
    dispatchers: list[RecordingDispatcher] = []

    def create() -> RecordingDispatcher:
        dispatcher = RecordingDispatcher()
        dispatchers.append(dispatcher)
        return dispatcher

    monkeypatch.setattr(desktop_page_events, "PageEventDispatcher", create)
    return dispatchers


@pytest.mark.parametrize(
    ("handed_over", "opened"),
    [
        ({"host": None, "port": None, **SESSION_REQUEST}, True),
        ({"host": "PI.lan", "port": 9000, **SESSION_REQUEST}, True),
        ({"host": "pi.lan", "port": 9001, **SESSION_REQUEST}, False),
        ({"host": "pi.lan", "port": 9000, "agent": "", "session": "session-1"}, False),
        ({"host": "pi.lan", "port": "9000", **SESSION_REQUEST}, False),
        (None, False),
    ],
    ids=["shown-server", "same-server", "other-server", "no-agent", "bad-port", "no-request"],
)
def test_a_running_desktop_opens_a_handed_over_session_only_on_its_server(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    monkeypatch: pytest.MonkeyPatch,
    handed_over: dict[str, Any] | None,
    opened: bool,
) -> None:
    dispatchers = _record_page_events(monkeypatch)
    fake_webview = _launch(tmp_path, settings=SAVED_PI)
    instance = launch_seams.instance
    assert instance is not None and instance.on_activate is not None

    instance.on_activate(handed_over)

    expected = [(SESSION_REQUEST["agent"], SESSION_REQUEST["session"])] if opened else []
    assert dispatchers[0].opened_sessions == expected
    assert fake_webview.window.focus_calls == ["show"]


def test_a_running_desktop_on_its_connection_screen_opens_no_session(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatchers = _record_page_events(monkeypatch)
    fake_webview = _launch(tmp_path)
    instance = launch_seams.instance
    assert instance is not None and instance.on_activate is not None

    instance.on_activate({"host": None, "port": None, **SESSION_REQUEST})

    assert dispatchers[0].opened_sessions == []
    assert fake_webview.window.focus_calls == ["show"]


@pytest.mark.parametrize("packaged", [True, False], ids=["packaged", "development"])
def test_a_restart_successor_restores_the_window_its_predecessor_showed(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    monkeypatch: pytest.MonkeyPatch,
    packaged: bool,
) -> None:
    nonce = "a" * 32
    request = desktop_restart.RestartRequest(
        nonce=nonce,
        server=("nas.lan", 8420),
        location="#settings/desktop",
        placement=desktop_restart.WindowPlacement("normal", 1200, 700, 40, 60),
    )
    desktop_restart.write_restart_request(tmp_path, request, created_at=time.time())
    if packaged:
        contract = {
            "version_file": str(tmp_path / "active-version"),
            "version": "v1",
            "command": [str(tmp_path / "vBot.GUI.exe"), "desktop"],
        }
        monkeypatch.setenv(desktop_restart.RELAUNCH_ENV, json.dumps(contract))
    else:
        monkeypatch.delenv(desktop_restart.RELAUNCH_ENV, raising=False)

    fake_webview = _launch(tmp_path, settings=SAVED_PI)

    kwargs = fake_webview.created_windows[0][1]
    assert _bridge(fake_webview).getDesktopCapabilities()["restart"] is packaged
    if not packaged:
        # Only a packaged launch can be a successor; the request stays untouched.
        assert launch_seams.handoffs == [None]
        assert fake_webview.window.loaded_urls == [_webui_url("pi.lan", 9000)]
        assert (tmp_path / desktop_restart.RESTART_REQUEST_FILE_NAME).exists()
        return
    assert launch_seams.handoffs == [nonce]
    assert fake_webview.window.loaded_urls == [f"{_webui_url('nas.lan', 8420)}#settings/desktop"]
    assert (kwargs["width"], kwargs["height"], kwargs["x"], kwargs["y"]) == (1200, 700, 40, 60)
    assert (kwargs["minimized"], kwargs["maximized"]) == (False, False)
    # A later successor's handoff goes to the restart, never to the focus.
    instance = launch_seams.instance
    assert instance is not None and instance.on_activate is not None
    instance.on_activate({"handoff": "b" * 32})
    assert fake_webview.window.focus_calls == []


def test_page_pushes_reach_the_window_page_until_it_closes(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_sinks: list[Any] = []
    dispatchers = _record_page_events(monkeypatch)

    original_create_voice = desktop_main._create_voice

    def create_voice(args: Any, settings: Any, server_url: str, page_events: Any) -> Any:
        voice_sinks.append(page_events)
        return original_create_voice(args, settings, server_url, page_events)

    monkeypatch.setattr(desktop_main, "_create_voice", create_voice)

    fake_webview = _launch(tmp_path)

    dispatcher = dispatchers[0]
    assert dispatcher.window is fake_webview.window
    assert dispatcher.closed is True
    assert voice_sinks == [dispatcher]
    launch_seams.hotkeys[0].on_press()
    assert dispatcher.requests == [("toggle", "hotkey")]
