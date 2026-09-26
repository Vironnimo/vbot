"""Tests for Desktop probing primitives and the controller-wired launch."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from desktop import connection as desktop_connection
from desktop import hotkey as desktop_hotkey
from desktop import main as desktop_main
from desktop import page_events as desktop_page_events
from desktop.main import DesktopProbeResult, DesktopTarget

_TEST_DESKTOP_SESSION_ID = "desktop-test-session"


class _FixedUuid:
    hex = _TEST_DESKTOP_SESSION_ID


class FakeDesktopInstance:
    """Single-instance guard double owned by the launch under test."""

    def __init__(self) -> None:
        self.on_activate: Callable[[], None] | None = None
        self.closed = False

    def listen(self, on_activate: Callable[[], None]) -> None:
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
        self.browser_origins: list[tuple[str, ...]] = []
        self.secure_origin_targets: list[list[tuple[str, int]]] = []
        self.microphone_origin: Callable[[], str | None] | None = None
        self.events: list[str] = []
        self.hotkeys: list[FakeLiveHotkey] = []

    def hotkey(self, **kwargs: Any) -> FakeLiveHotkey:
        hotkey = FakeLiveHotkey(self.events, **kwargs)
        self.hotkeys.append(hotkey)
        return hotkey

    def claim(self, config_directory: Path) -> FakeDesktopInstance | None:
        self.claimed.append(config_directory)
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


@pytest.mark.parametrize("missing_module", ["pyopen_wakeword", "sounddevice", "webrtcvad"])
def test_real_wakeword_availability_requires_complete_voice_stack(
    monkeypatch: pytest.MonkeyPatch, missing_module: str
) -> None:
    for module_name in ("pyopen_wakeword", "sounddevice", "webrtcvad"):
        monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
    monkeypatch.setitem(sys.modules, missing_module, None)

    assert desktop_main._real_wakeword_available() is False


def test_real_wakeword_availability_accepts_complete_voice_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in ("pyopen_wakeword", "sounddevice", "webrtcvad"):
        monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))

    assert desktop_main._real_wakeword_available() is True


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
        self.screens = [FakeScreen()]
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


def fake_get_for(
    responses: dict[str, FakeResponse | httpx.RequestError],
) -> desktop_main.HttpGet:
    class FakeGet:
        def __call__(
            self, url: str, *, timeout: float, trust_env: bool
        ) -> desktop_main.HttpResponse:
            assert timeout == desktop_main.PROBE_TIMEOUT_SECONDS
            assert trust_env is False
            response = responses[url]
            if isinstance(response, httpx.RequestError):
                raise response
            return response

    return FakeGet()


def _write_servers(settings_file: Path, servers: list[dict[str, Any]]) -> None:
    settings_file.write_text(json.dumps({"servers": servers}), encoding="utf-8")


# -- Argument parsing --------------------------------------------------------


def test_parse_args_accepts_host_and_port() -> None:
    args = desktop_main.parse_args(["--host", "192.168.1.50", "--port", "9000"])

    assert args.host == "192.168.1.50"
    assert args.port == 9000


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_parse_args_rejects_invalid_ports(port: str) -> None:
    with pytest.raises(SystemExit):
        desktop_main.parse_args(["--port", port])


def test_parse_args_accepts_mock_wakeword_flag() -> None:
    args = desktop_main.parse_args(["--mock-wakeword"])

    assert args.mock_wakeword is True


# -- Module boundaries -------------------------------------------------------


def test_desktop_main_does_not_import_server_or_core_business_logic() -> None:
    source = Path(desktop_main.__file__).read_text(encoding="utf-8")

    assert "from server" not in source
    assert "import server" not in source
    assert "from core" not in source
    assert "import core" not in source


def test_desktop_main_does_not_import_cli_server_management() -> None:
    source = Path(desktop_main.__file__).read_text(encoding="utf-8")

    assert "cli.server_management" not in source
    assert "from cli" not in source
    assert "import cli" not in source


def test_icon_path_selects_the_platform_native_asset(tmp_path: Path) -> None:
    assert desktop_main.icon_path(tmp_path, platform="win32") == tmp_path / "icon.ico"
    assert desktop_main.icon_path(tmp_path, platform="linux") == tmp_path / "icon.png"
    assert desktop_main.icon_path(tmp_path, platform="darwin") == tmp_path / "icon.png"


def test_bundled_windows_icon_is_a_multiresolution_ico() -> None:
    icon_data = desktop_main.icon_path(platform="win32").read_bytes()

    assert icon_data[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(icon_data[4:6], byteorder="little") > 1


def test_desktop_main_keeps_out_of_server_lifecycle_management() -> None:
    source = Path(desktop_main.__file__).read_text(encoding="utf-8")

    assert "server start" not in source.lower()
    assert "server stop" not in source.lower()
    assert "server restart" not in source.lower()


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


def test_desktop_main_logs_normal_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(desktop_main, "configure_desktop_logging", lambda: None)
    monkeypatch.setattr(desktop_main, "close_desktop_logging", lambda _handler: None)
    monkeypatch.setattr(desktop_main, "launch_desktop", lambda _argv: True)

    with caplog.at_level("INFO", logger="vbot.desktop"):
        desktop_main.main([])

    records = [
        record
        for record in caplog.records
        if record.name == "vbot.desktop" and record.levelno == logging.INFO
    ]
    assert len(records) == 1


def test_desktop_main_does_not_report_a_shutdown_when_another_desktop_was_focused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(desktop_main, "configure_desktop_logging", lambda: None)
    monkeypatch.setattr(desktop_main, "close_desktop_logging", lambda _handler: None)
    monkeypatch.setattr(desktop_main, "launch_desktop", lambda _argv: False)

    with caplog.at_level("INFO", logger="vbot.desktop"):
        desktop_main.main([])

    assert [record for record in caplog.records if record.name == "vbot.desktop"] == []


# -- Probe classification ----------------------------------------------------


@pytest.mark.parametrize("root_fails", [False, True])
def test_probe_reuses_one_unproxied_client_and_closes_it(monkeypatch, root_fails):
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
        def __init__(self, **kwargs):
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


def test_probe_target_classifies_available_webui() -> None:
    target = DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

    result = desktop_main.probe_target(
        target,
        get=fake_get_for(
            {
                "http://127.0.0.1:8420/health": FakeResponse(200, {"status": "ok"}),
                "http://127.0.0.1:8420/": FakeResponse(200),
            }
        ),
    )

    assert result.status == desktop_main.PROBE_WEBUI_AVAILABLE


@pytest.mark.parametrize("status_code", [200, 204, 301, 302, 399])
def test_probe_target_accepts_2xx_and_3xx_webui_responses(status_code: int) -> None:
    target = DesktopTarget("vbot.lan", 9000, "http://vbot.lan:9000/")

    result = desktop_main.probe_target(
        target,
        get=fake_get_for(
            {
                "http://vbot.lan:9000/health": FakeResponse(200, {"status": "ok"}),
                "http://vbot.lan:9000/": FakeResponse(status_code),
            }
        ),
    )

    assert result.status == desktop_main.PROBE_WEBUI_AVAILABLE


@pytest.mark.parametrize("status_code", [400, 404, 500])
def test_probe_target_classifies_missing_webui(status_code: int) -> None:
    target = DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

    result = desktop_main.probe_target(
        target,
        get=fake_get_for(
            {
                "http://127.0.0.1:8420/health": FakeResponse(200, {"status": "ok"}),
                "http://127.0.0.1:8420/": FakeResponse(status_code),
            }
        ),
    )

    assert result.status == desktop_main.PROBE_WEBUI_UNAVAILABLE


def test_probe_target_classifies_root_request_error_as_missing_webui() -> None:
    target = DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

    result = desktop_main.probe_target(
        target,
        get=fake_get_for(
            {
                "http://127.0.0.1:8420/health": FakeResponse(200, {"status": "ok"}),
                "http://127.0.0.1:8420/": httpx.ConnectError("connection closed"),
            }
        ),
    )

    assert result.status == desktop_main.PROBE_WEBUI_UNAVAILABLE


def test_probe_target_classifies_unreachable_server() -> None:
    target = DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

    result = desktop_main.probe_target(
        target,
        get=fake_get_for(
            {"http://127.0.0.1:8420/health": httpx.ConnectError("connection refused")}
        ),
    )

    assert result.status == desktop_main.PROBE_SERVER_UNREACHABLE


@pytest.mark.parametrize(
    ("health_response"),
    [
        FakeResponse(503, {"status": "ok"}),
        FakeResponse(200, {"status": "starting"}),
        FakeResponse(200, {"status": "ok", "extra": True}),
        FakeResponse(200, {"status": "ok", "version": "dev"}),
        FakeResponse(200, ValueError("invalid json")),
        FakeResponse(200, ["ok"]),
    ],
)
def test_probe_target_classifies_non_vbot_server(health_response: FakeResponse) -> None:
    target = DesktopTarget("example.test", 8080, "http://example.test:8080/")

    result = desktop_main.probe_target(
        target,
        get=fake_get_for({"http://example.test:8080/health": health_response}),
    )

    assert result.status == desktop_main.PROBE_NOT_VBOT_SERVER


def test_probe_target_classifies_configuration_error_as_invalid_target() -> None:
    target = DesktopTarget("bad host", 8420, "", configuration_error="bad host")

    result = desktop_main.probe_target(target)

    assert result.status == desktop_main.PROBE_INVALID_TARGET


def test_probe_target_has_no_retry_loop() -> None:
    target = DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")
    requested_urls: list[str] = []

    def record_get(url: str, *, timeout: float, trust_env: bool) -> FakeResponse:
        assert trust_env is False
        requested_urls.append(url)
        if url.endswith("/health"):
            return FakeResponse(200, {"status": "ok"})
        return FakeResponse(404)

    result = desktop_main.probe_target(target, get=record_get)

    assert result.status == desktop_main.PROBE_WEBUI_UNAVAILABLE
    assert requested_urls == ["http://127.0.0.1:8420/health", "http://127.0.0.1:8420/"]


# -- Host/port validation and URL building -----------------------------------


@pytest.mark.parametrize("host", ["", "   ", "http://localhost", "bad host", "host/path"])
def test_validate_host_rejects_non_host_values(host: str) -> None:
    with pytest.raises(ValueError):
        desktop_main.validate_host(host)


def test_validate_host_rejects_url_with_clear_message() -> None:
    with pytest.raises(ValueError):
        desktop_main.validate_host("http://localhost", source="settings.host")


@pytest.mark.parametrize("port", [0, 65536, "not-a-port", None])
def test_validate_port_rejects_out_of_range_and_non_numeric(port: object) -> None:
    with pytest.raises(ValueError):
        desktop_main.validate_port(port)


def test_build_target_url_formats_local_and_lan_targets_as_plain_http() -> None:
    assert desktop_main.build_target_url("127.0.0.1", 8420) == "http://127.0.0.1:8420/"
    assert desktop_main.build_target_url("192.168.1.44", 9000) == "http://192.168.1.44:9000/"
    assert desktop_main.build_target_url("vbot.lan", 8500) == "http://vbot.lan:8500/"


# -- Launch wiring -----------------------------------------------------------


def test_launch_creates_window_before_loop_with_html_and_bridge_js_api(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert len(fake_webview.created_windows) == 1
    title, kwargs = fake_webview.created_windows[0]
    assert title == desktop_main.WINDOW_TITLE
    # The window opens on the neutral connection screen (no URL pre-loop), and
    # the same bridge object is its single js_api for both screen and WebUI.
    assert "url" not in kwargs
    assert 'id="connect-form"' in kwargs["html"]
    assert kwargs["background_color"] == desktop_main.APP_BACKGROUND_COLOR
    assert kwargs["text_select"] is True
    assert kwargs["js_api"] is not None
    assert kwargs["width"] == 1280
    assert kwargs["height"] == 800
    assert kwargs["min_size"] == (800, 600)
    # The window is explicitly placed on the primary screen so DPI scaling and
    # multi-monitor layouts don't push it off-screen.
    assert kwargs["screen"] is not None
    assert hasattr(kwargs["js_api"], "connect")
    assert hasattr(kwargs["js_api"], "getVoiceStatus")


def test_resolve_window_layout_uses_screen_aware_first_run_size() -> None:
    screen = FakeScreen(width=1920, height=1080)
    layout = desktop_main.resolve_window_layout(None, screen)

    assert (layout.width, layout.height) == (1440, 864)
    assert (layout.minimum_width, layout.minimum_height) == (800, 600)


def test_resolve_window_layout_keeps_remembered_size_that_fits() -> None:
    screen = FakeScreen(width=1920, height=1080)
    layout = desktop_main.resolve_window_layout((1380, 900), screen)

    assert (layout.width, layout.height) == (1380, 900)


def test_resolve_window_layout_clamps_remembered_size_to_smaller_screen() -> None:
    screen = FakeScreen(width=1280, height=720)
    layout = desktop_main.resolve_window_layout((1800, 1100), screen)

    assert (layout.width, layout.height) == (1280, 720)
    assert (layout.minimum_width, layout.minimum_height) == (800, 600)


def test_resolve_window_layout_clamps_to_work_area_excluding_taskbar() -> None:
    """The work area (Screen.frame) excludes the taskbar, so a saved size that
    fits the full screen bounds but exceeds the work area is clamped down."""
    screen = FakeScreen(width=2048, height=1152, frame=FakeFrame(Width=2048, Height=1104))
    layout = desktop_main.resolve_window_layout((2048, 1152), screen)

    assert (layout.width, layout.height) == (2048, 1104)


def test_resolve_window_layout_passes_screen_through_for_placement() -> None:
    screen = FakeScreen(width=1920, height=1080)
    layout = desktop_main.resolve_window_layout(None, screen)

    assert layout.screen is screen


def test_resolve_window_layout_with_no_screen_uses_fallback_and_no_placement() -> None:
    layout = desktop_main.resolve_window_layout(None, None)

    assert layout.width > 0
    assert layout.height > 0
    assert layout.screen is None


def test_resolve_window_layout_clamps_against_primary_not_first_screen() -> None:
    """screens[0] may be a secondary monitor; the primary contains (0,0)."""
    # Secondary screen on the left, screens[0] by WinForms enumeration order
    secondary = FakeScreen(width=1920, height=1080, x=-1920, y=270)
    primary = FakeScreen(width=2048, height=1152, x=0, y=0, scale=1.25)
    assert (
        desktop_main._primary_screen(types.SimpleNamespace(screens=[secondary, primary])) is primary
    )


def test_primary_screen_falls_back_to_first_when_origin_not_contained() -> None:
    only = FakeScreen(width=1920, height=1080, x=-1920, y=0)
    result = desktop_main._primary_screen(types.SimpleNamespace(screens=[only]))
    assert result is only


def test_primary_screen_returns_none_when_no_screens() -> None:
    assert desktop_main._primary_screen(types.SimpleNamespace(screens=[])) is None


def test_launch_restores_remembered_window_size(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"window": {"width": 1400, "height": 900}}),
        encoding="utf-8",
    )

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    _, kwargs = fake_webview.created_windows[0]
    assert kwargs["width"] == 1400
    assert kwargs["height"] == 900


def test_launch_persists_window_size_when_window_closes(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )
    fake_webview.window.width = 1500
    fake_webview.window.height = 920

    fake_webview.window.events.closing.emit()

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["window"] == {"width": 1500, "height": 920}


def test_launch_runs_auto_connect_after_window_is_shown(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"
    _write_servers(settings_file, [{"host": "pi.lan", "port": 9000}])

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # The shown event navigated the live window to the saved server's WebUI with
    # the accessor marker; start() itself received no eager startup callback.
    assert fake_webview.start_func is None
    assert len(fake_webview.window.events.shown.handlers) == 1
    assert fake_webview.window.loaded_urls == [
        "http://pi.lan:9000/?accessor=desktop&desktop_session=desktop-test-session"
    ]


def test_launch_first_run_shows_connection_screen_via_auto_connect(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # No saved server: auto_connect renders the connection screen, never a URL.
    assert fake_webview.window.loaded_urls == []
    assert len(fake_webview.window.loaded_html) == 1
    assert 'id="connect-form"' in fake_webview.window.loaded_html[0]


def test_launch_does_not_auto_connect_to_default_localhost(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    probed_targets: list[DesktopTarget] = []

    def record_probe(target: DesktopTarget) -> DesktopProbeResult:
        probed_targets.append(target)
        return DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target)

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=record_probe,
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # The old silent 127.0.0.1:8420 default is gone: with nothing saved, nothing
    # is probed and the window never navigates to localhost.
    assert probed_targets == []
    assert fake_webview.window.loaded_urls == []


# -- Launch with explicit --host/--port override -----------------------------


def test_launch_host_port_override_connects_directly_even_on_first_run(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        ["--host", "pi.lan", "--port", "9000"],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # An explicit override is a deliberate target: it connects straight to the
    # WebUI (with the accessor marker), not to the connection screen, even with
    # nothing saved.
    assert fake_webview.window.loaded_urls == [
        "http://pi.lan:9000/?accessor=desktop&desktop_session=desktop-test-session"
    ]
    assert fake_webview.window.loaded_html == []


def test_launch_override_remembers_target_as_last_used(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"

    desktop_main.launch_desktop(
        ["--host", "pi.lan", "--port", "9000"],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["servers"] == [{"host": "pi.lan", "port": 9000}]
    assert stored["last_used"] == {"host": "pi.lan", "port": 9000}


def test_launch_port_only_override_fills_default_host(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        ["--port", "9000"],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    expected_url = (
        f"http://{desktop_main.DEFAULT_HOST}:9000/?accessor=desktop"
        "&desktop_session=desktop-test-session"
    )
    assert fake_webview.window.loaded_urls == [expected_url]


def test_launch_host_only_override_fills_default_port(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        ["--host", "pi.lan"],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    expected_url = (
        f"http://pi.lan:{desktop_main.DEFAULT_PORT}/?accessor=desktop"
        "&desktop_session=desktop-test-session"
    )
    assert fake_webview.window.loaded_urls == [expected_url]


def test_launch_override_takes_precedence_over_saved_last_used(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "servers": [{"host": "old.lan", "port": 8420}],
                "last_used": {"host": "old.lan", "port": 8420},
            }
        ),
        encoding="utf-8",
    )

    desktop_main.launch_desktop(
        ["--host", "new.lan", "--port", "9000"],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # The override wins over the saved last-used target.
    assert fake_webview.window.loaded_urls == [
        "http://new.lan:9000/?accessor=desktop&desktop_session=desktop-test-session"
    ]


def test_resolve_launch_server_url_prefers_override_for_worker(tmp_path: Path) -> None:
    from desktop.connection import ConnectionController

    settings_file = tmp_path / "settings.json"
    _write_servers(settings_file, [{"host": "saved.lan", "port": 8420}])
    controller = ConnectionController(settings_file=settings_file)

    # Override → the worker's server_url targets the override, not last-used, so
    # window and voice point at the same server on an override first-run.
    override_url = desktop_main._resolve_launch_server_url(("pi.lan", 9000), controller)
    assert override_url == "http://pi.lan:9000/"

    # No override → falls back to the controller's last-used resolution.
    fallback_url = desktop_main._resolve_launch_server_url(None, controller)
    assert fallback_url == "http://saved.lan:8420/"

    # No override and nothing saved → empty (worker skips network calls).
    empty_controller = ConnectionController(settings_file=tmp_path / "empty.json")
    assert desktop_main._resolve_launch_server_url(None, empty_controller) == ""


def test_launch_starts_without_native_menu(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert len(fake_webview.start_calls) == 1
    assert "menu" not in fake_webview.start_calls[0]
    assert "icon" not in fake_webview.start_calls[0]


def test_launch_persists_webview_profile_beside_settings(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # The WebView2 profile must survive Desktop restarts: private mode off
    # (pywebview would otherwise delete the user-data folder on close) and a
    # stable storage path next to the Desktop settings file.
    assert fake_webview.start_calls[0]["private_mode"] is False
    assert fake_webview.start_calls[0]["storage_path"] == str(
        tmp_path / desktop_main.WEBVIEW_STORAGE_DIR_NAME
    )


def test_launch_passes_icon_only_when_icon_exists(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    icon_file = tmp_path / "icon.ico"
    icon_file.write_bytes(b"fake-icon")

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=icon_file,
    )

    assert fake_webview.start_calls[0]["icon"] == str(icon_file)


def test_launch_attaches_the_created_window_to_the_controller(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"
    _write_servers(settings_file, [{"host": "pi.lan", "port": 9000}])

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # Proof the controller drove *the created window*: that exact FakeWindow saw
    # the navigation. (If attach_window were skipped, the controller would have
    # no window and raise.)
    assert fake_webview.window.loaded_urls == [
        "http://pi.lan:9000/?accessor=desktop&desktop_session=desktop-test-session"
    ]


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


def test_launch_does_not_start_voice_when_gui_fails_before_window_is_shown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class StartRaisesWebview(FakeWebview):
        def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
            raise RuntimeError("gui loop crashed")

    monkeypatch.setattr(desktop_main, "_create_voice", lambda *_args: RecordingVoice(events))

    with pytest.raises(RuntimeError, match="gui loop crashed"):
        desktop_main.launch_desktop(
            [],
            settings_file=tmp_path / "settings.json",
            probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
            webview_module=StartRaisesWebview(),
            app_icon_path=tmp_path / "missing-icon.png",
        )

    assert events == ["voice.close"]


def test_launch_starts_voice_only_after_window_is_shown_and_follows_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_webview = FakeWebview()
    events: list[str] = []

    def window_exists() -> None:
        assert len(fake_webview.created_windows) == 1
        assert fake_webview.window.loaded_urls  # the window connected first

    voice = RecordingVoice(events, on_start=window_exists)
    monkeypatch.setattr(desktop_main, "_create_voice", lambda *_args: voice)

    desktop_main.launch_desktop(
        ["--host", "pi.lan", "--port", "9000"],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert events == ["voice.start", "voice.close"]
    assert voice.server_urls == ["http://pi.lan:9000/"]


def test_launch_with_disabled_voice_never_probes_wakeword_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_probed() -> bool:
        raise AssertionError("Voice dependencies must stay lazy while Voice is disabled")

    monkeypatch.setattr(desktop_main, "_real_wakeword_available", fail_if_probed)
    monkeypatch.setitem(sys.modules, "desktop.wakeword.capture", None)
    monkeypatch.setitem(sys.modules, "desktop.wakeword.echo", None)

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=FakeWebview(),
        app_icon_path=tmp_path / "missing-icon.png",
    )


def test_enabled_voice_probes_dependencies_only_after_window_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"wakeword": {"enabled": True}}), encoding="utf-8")
    probed = threading.Event()

    class RunningWebview(FakeWebview):
        """Keeps the GUI loop running until Voice reported its start result."""

        def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
            super().start(func, **kwargs)
            bridge = self.created_windows[0][1]["js_api"]
            deadline = time.monotonic() + 5
            while bridge.getVoiceStatus()["state"] != "error" and time.monotonic() < deadline:
                time.sleep(0.01)

    fake_webview = RunningWebview()

    def unavailable_after_window_created() -> bool:
        assert len(fake_webview.created_windows) == 1
        probed.set()
        return False

    monkeypatch.setattr(
        desktop_main,
        "_real_wakeword_available",
        unavailable_after_window_created,
    )

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert probed.is_set()
    status = fake_webview.created_windows[0][1]["js_api"].getVoiceStatus()
    assert status["mode"] == "unavailable"


# -- Single instance, browser arguments, and Live voice integration ----------


def _available(target: DesktopTarget) -> DesktopProbeResult:
    return DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target)


def test_second_launch_focuses_the_running_desktop_without_a_window(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    caplog: pytest.LogCaptureFixture,
) -> None:
    launch_seams.instance = None
    fake_webview = FakeWebview()

    with caplog.at_level("INFO", logger="vbot.desktop"):
        opened = desktop_main.launch_desktop(
            ["--host", "pi.lan", "--port", "9000"],
            settings_file=tmp_path / "settings.json",
            probe=_available,
            webview_module=fake_webview,
            app_icon_path=tmp_path / "missing-icon.png",
        )

    assert opened is False
    assert launch_seams.claimed == [tmp_path]
    assert fake_webview.created_windows == []
    assert fake_webview.start_calls == []
    assert launch_seams.browser_origins == []
    assert not (tmp_path / "settings.json").exists()
    assert "ignored the requested target pi.lan:9000" in caplog.text


def test_launch_owns_the_instance_until_the_window_closes(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    fake_webview = FakeWebview()

    opened = desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=_available,
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    instance = launch_seams.instance
    assert opened is True
    assert instance is not None
    assert instance.closed is True
    # A second launch asks this window to come to the front.
    assert instance.on_activate is not None
    instance.on_activate()
    assert fake_webview.window.focus_calls == ["show"]


def test_launch_closes_the_instance_when_the_gui_loop_fails(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    class StartRaisesWebview(FakeWebview):
        def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
            raise RuntimeError("gui loop crashed")

    with pytest.raises(RuntimeError, match="gui loop crashed"):
        desktop_main.launch_desktop(
            [],
            settings_file=tmp_path / "settings.json",
            probe=_available,
            webview_module=StartRaisesWebview(),
            app_icon_path=tmp_path / "missing-icon.png",
        )

    assert launch_seams.instance is not None
    assert launch_seams.instance.closed is True
    assert launch_seams.events == ["apply_browser_arguments", "hotkey.stop"]


def test_launch_makes_every_known_server_a_secure_origin_before_webview_starts(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    settings_file = tmp_path / "settings.json"
    _write_servers(settings_file, [{"host": "a.lan", "port": 8420}])
    fake_webview = FakeWebview()
    fake_webview.launch_events = launch_seams.events

    desktop_main.launch_desktop(
        ["--host", "pi.lan", "--port", "9000"],
        settings_file=settings_file,
        probe=_available,
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert launch_seams.secure_origin_targets == [[("a.lan", 8420), ("pi.lan", 9000)]]
    assert launch_seams.browser_origins == [("http://a.lan:8420", "http://pi.lan:9000")]
    assert launch_seams.events[:2] == ["apply_browser_arguments", "webview.start"]
    bridge = fake_webview.created_windows[0][1]["js_api"]
    assert bridge.getDesktopCapabilities()["secureOrigins"] == [
        "http://a.lan:8420",
        "http://pi.lan:9000",
    ]


def test_microphone_permission_follows_the_connected_server(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        ["--host", "pi.lan", "--port", "9000"],
        settings_file=tmp_path / "settings.json",
        probe=_available,
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert launch_seams.microphone_origin is not None
    assert launch_seams.microphone_origin() == "http://pi.lan:9000"


def test_microphone_permission_has_no_origin_before_a_server_connects(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=_available,
        webview_module=FakeWebview(),
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # First run shows the connection screen: nothing may use the microphone yet.
    assert launch_seams.microphone_origin is not None
    assert launch_seams.microphone_origin() is None


def test_live_hotkey_runs_only_while_the_window_is_shown(
    tmp_path: Path,
    launch_seams: LaunchSeams,
) -> None:
    fake_webview = FakeWebview()
    fake_webview.launch_events = launch_seams.events

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=_available,
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert launch_seams.events == [
        "apply_browser_arguments",
        "webview.start",
        "hotkey.start",
        "hotkey.stop",
    ]
    assert launch_seams.hotkeys[0].settings_path == tmp_path / "settings.json"
    bridge = fake_webview.created_windows[0][1]["js_api"]
    assert bridge.getDesktopCapabilities()["liveHotkey"] is True


def test_page_pushes_reach_the_window_page_until_it_closes(
    tmp_path: Path,
    launch_seams: LaunchSeams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatchers: list[RecordingDispatcher] = []
    voice_sinks: list[Any] = []

    class RecordingDispatcher:
        def __init__(self) -> None:
            self.window: Any = None
            self.requests: list[tuple[str, str]] = []
            self.closed = False
            dispatchers.append(self)

        def attach_window(self, window: Any) -> None:
            self.window = window

        def request_live(self, action: str, source: str) -> None:
            self.requests.append((action, source))

        def publish_status(self, _status: Any) -> None:
            pass

        def publish_event(self, _event: Any) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    original_create_voice = desktop_main._create_voice

    def create_voice(args: Any, settings: Any, server_url: str, page_events: Any) -> Any:
        voice_sinks.append(page_events)
        return original_create_voice(args, settings, server_url, page_events)

    monkeypatch.setattr(desktop_page_events, "PageEventDispatcher", RecordingDispatcher)
    monkeypatch.setattr(desktop_main, "_create_voice", create_voice)
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=_available,
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    dispatcher = dispatchers[0]
    assert dispatcher.window is fake_webview.window
    assert dispatcher.closed is True
    assert voice_sinks == [dispatcher]
    launch_seams.hotkeys[0].on_press()
    assert dispatcher.requests == [("toggle", "hotkey")]


def test_create_voice_selects_the_mock_mode_from_the_flag(tmp_path: Path) -> None:
    page_events = desktop_page_events.PageEventDispatcher()
    voice = desktop_main._create_voice(
        desktop_main.parse_args(["--mock-wakeword"]),
        tmp_path / "settings.json",
        "http://pi.lan:9000",
        page_events,
    )
    try:
        assert voice.status()["mode"] == "mock"
    finally:
        voice.close()
        page_events.close()


@pytest.mark.parametrize(
    ("window_events", "expected_calls"),
    [
        ([], ["show"]),
        (["minimized"], ["restore", "show"]),
        (["maximized", "minimized"], ["maximize", "show"]),
        (["maximized", "minimized", "restored"], ["show"]),
        (["minimized", "maximized"], ["show"]),
    ],
)
def test_window_focus_restores_a_minimized_window_to_its_previous_size(
    window_events: list[str],
    expected_calls: list[str],
) -> None:
    window = FakeWindow()
    focus = desktop_main._WindowFocus(window)

    for name in window_events:
        getattr(window.events, name).emit()
    focus.bring_to_front()

    assert window.focus_calls == expected_calls
