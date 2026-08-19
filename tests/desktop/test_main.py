"""Tests for Desktop probing primitives and the controller-wired launch."""

from __future__ import annotations

import json
import logging
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from desktop import main as desktop_main
from desktop.main import DesktopProbeResult, DesktopTarget


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

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def load_html(self, content: str) -> None:
        self.loaded_html.append(content)


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
        self.shown = FakeEvent()
        self.closing = FakeEvent()


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

    def create_window(self, title: str, **kwargs: Any) -> FakeWindow:
        self.created_windows.append((title, kwargs))
        self.window.width = kwargs["width"]
        self.window.height = kwargs["height"]
        return self.window

    def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
        self.start_calls.append(kwargs)
        self.start_func = func
        if func is not None:
            func()
        self.window.events.shown.emit()


def fake_get_for(
    responses: dict[str, FakeResponse | httpx.RequestError],
) -> desktop_main.HttpGet:
    class FakeGet:
        def __call__(self, url: str, *, timeout: float) -> desktop_main.HttpResponse:
            assert timeout == desktop_main.PROBE_TIMEOUT_SECONDS
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
        logging.getLogger("vbot.desktop.wakeword.worker").warning(
            "Wakeword worker stopped (reason=speech_to_text_unconfigured)"
        )
        handler.flush()

        log_files = list((tmp_path / "logs").glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text(encoding="utf-8")
        assert "[WARN] vbot.desktop.wakeword.worker" in content
        assert "reason=speech_to_text_unconfigured" in content
    finally:
        desktop_main.close_desktop_logging(handler)


def test_desktop_main_logs_normal_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(desktop_main, "configure_desktop_logging", lambda: None)
    monkeypatch.setattr(desktop_main, "close_desktop_logging", lambda _handler: None)
    monkeypatch.setattr(desktop_main, "launch_desktop", lambda _argv: None)

    with caplog.at_level("INFO", logger="vbot.desktop"):
        desktop_main.main([])

    records = [
        record
        for record in caplog.records
        if record.name == "vbot.desktop" and record.levelno == logging.INFO
    ]
    assert len(records) == 1


# -- Probe classification ----------------------------------------------------


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

    def record_get(url: str, *, timeout: float) -> FakeResponse:
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
    assert kwargs["text_select"] is True
    assert kwargs["js_api"] is not None
    assert kwargs["width"] == 1280
    assert kwargs["height"] == 800
    assert kwargs["min_size"] == (800, 600)
    # The window is explicitly placed on the primary screen so DPI scaling and
    # multi-monitor layouts don't push it off-screen.
    assert kwargs["screen"] is not None
    assert hasattr(kwargs["js_api"], "connect")
    assert hasattr(kwargs["js_api"], "getWakewordStatus")


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
    assert fake_webview.window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


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
    assert fake_webview.window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]
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

    expected_url = f"http://{desktop_main.DEFAULT_HOST}:9000/?accessor=desktop"
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

    expected_url = f"http://pi.lan:{desktop_main.DEFAULT_PORT}/?accessor=desktop"
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
    assert fake_webview.window.loaded_urls == ["http://new.lan:9000/?accessor=desktop"]


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
    assert fake_webview.window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


def test_launch_does_not_start_worker_when_gui_fails_before_window_is_shown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"wakeword": {"enabled": True}}), encoding="utf-8")
    created: list[bool] = []
    stopped: list[bool] = []

    class StartRaisesWebview(FakeWebview):
        def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
            raise RuntimeError("gui loop crashed")

    class RecordingWorker:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            stopped.append(True)

        def is_running(self) -> bool:
            return True

    # Pin the worker factory to a recording worker via the public factory hook
    # rather than the real audio stack, so the test stays headless.
    def fake_bridge(args: Any, settings: Any, controller: Any, server_url: str) -> Any:
        from desktop.wakeword.bridge import DesktopBridge

        def create_worker(_bridge: DesktopBridge) -> RecordingWorker:
            created.append(True)
            return RecordingWorker()

        bridge = DesktopBridge(
            settings_path=settings,
            worker_factory=create_worker,
            connection=controller,
        )
        return bridge

    monkeypatch.setattr(desktop_main, "_create_wakeword_bridge", fake_bridge)

    with pytest.raises(RuntimeError, match="gui loop crashed"):
        desktop_main.launch_desktop(
            [],
            settings_file=settings_file,
            probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
            webview_module=StartRaisesWebview(),
            app_icon_path=tmp_path / "missing-icon.png",
        )

    assert created == []
    assert stopped == []


def test_launch_starts_enabled_voice_only_after_window_is_shown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"wakeword": {"enabled": True}}), encoding="utf-8")
    fake_webview = FakeWebview()
    events: list[str] = []

    class RecordingWorker:
        def start(self) -> None:
            assert len(fake_webview.created_windows) == 1
            events.append("started")

        def stop(self) -> None:
            events.append("stopped")

        def is_running(self) -> bool:
            return True

    def fake_bridge(args: Any, settings: Any, controller: Any, server_url: str) -> Any:
        from desktop.wakeword.bridge import DesktopBridge

        return DesktopBridge(
            settings_path=settings,
            worker_factory=lambda _bridge: RecordingWorker(),
            connection=controller,
        )

    monkeypatch.setattr(desktop_main, "_create_wakeword_bridge", fake_bridge)

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert events == ["started", "stopped"]


def test_launch_with_disabled_voice_never_probes_wakeword_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_probed() -> bool:
        raise AssertionError("Voice dependencies must stay lazy while Voice is disabled")

    monkeypatch.setattr(desktop_main, "_real_wakeword_available", fail_if_probed)

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
    fake_webview = FakeWebview()
    probed: list[bool] = []

    def unavailable_after_window_created() -> bool:
        assert len(fake_webview.created_windows) == 1
        probed.append(True)
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

    assert probed == [True]
    bridge = fake_webview.created_windows[0][1]["js_api"]
    assert bridge.getWakewordStatus()["mode"] == "unavailable"
    assert bridge.getWakewordStatus()["error_code"] == "voice_stack_unavailable"
