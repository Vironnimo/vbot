"""Tests for Desktop probing primitives and the controller-wired launch."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
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


class FakeWindow:
    """Live-window double recording the navigation the controller drives."""

    def __init__(self) -> None:
        self.loaded_urls: list[str] = []
        self.loaded_html: list[str] = []

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def load_html(self, content: str) -> None:
        self.loaded_html.append(content)


class FakeWebview:
    """pywebview double honoring the create-before-start / func-after-start order.

    ``create_window`` returns the :class:`FakeWindow` the controller later
    navigates; ``start`` invokes the post-loop ``func`` (the controller's
    ``auto_connect``) exactly as pywebview does once the GUI loop is live, so the
    test exercises the real navigation path without a GUI.
    """

    def __init__(self) -> None:
        self.created_windows: list[tuple[str, dict[str, Any]]] = []
        self.window = FakeWindow()
        self.start_calls: list[dict[str, Any]] = []
        self.start_func: Callable[[], Any] | None = None

    def create_window(self, title: str, **kwargs: Any) -> FakeWindow:
        self.created_windows.append((title, kwargs))
        return self.window

    def start(self, func: Callable[[], Any] | None = None, **kwargs: Any) -> None:
        self.start_calls.append(kwargs)
        self.start_func = func
        if func is not None:
            func()


@dataclass
class FakeMenuAction:
    title: str
    function: Callable[[], Any]


@dataclass
class FakeMenuSeparator:
    pass


@dataclass
class FakeMenu:
    title: str
    items: list[Any]


@dataclass
class FakeMenuModule:
    """Stand-in for ``webview.menu`` so the launch needs no GUI package."""

    Menu: Callable[..., Any] = field(default=lambda title, items: FakeMenu(title, items))
    MenuAction: Callable[..., Any] = field(
        default=lambda title, function: FakeMenuAction(title, function)
    )
    MenuSeparator: Callable[..., Any] = field(default=lambda: FakeMenuSeparator())


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


def test_desktop_main_keeps_out_of_server_lifecycle_management() -> None:
    source = Path(desktop_main.__file__).read_text(encoding="utf-8")

    assert "server start" not in source.lower()
    assert "server stop" not in source.lower()
    assert "server restart" not in source.lower()


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
    with pytest.raises(ValueError, match="not a URL"):
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
        menu_module=FakeMenuModule(),
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert len(fake_webview.created_windows) == 1
    title, kwargs = fake_webview.created_windows[0]
    assert title == desktop_main.WINDOW_TITLE
    # The window opens on the neutral connection screen (no URL pre-loop), and
    # the same bridge object is its single js_api for both screen and WebUI.
    assert "url" not in kwargs
    assert "Connect to a server" in kwargs["html"]
    assert kwargs["text_select"] is True
    assert kwargs["js_api"] is not None
    assert hasattr(kwargs["js_api"], "connect")
    assert hasattr(kwargs["js_api"], "getWakewordStatus")


def test_launch_runs_auto_connect_as_post_loop_func(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"
    _write_servers(settings_file, [{"host": "pi.lan", "port": 9000}])

    desktop_main.launch_desktop(
        [],
        settings_file=settings_file,
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        menu_module=FakeMenuModule(),
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # start() received auto_connect as func; running it navigated the live window
    # to the saved server's WebUI with the accessor marker.
    assert fake_webview.start_func is not None
    assert fake_webview.window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


def test_launch_first_run_shows_connection_screen_via_auto_connect(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        menu_module=FakeMenuModule(),
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # No saved server: auto_connect renders the connection screen, never a URL.
    assert fake_webview.window.loaded_urls == []
    assert len(fake_webview.window.loaded_html) == 1
    assert "Connect to a server" in fake_webview.window.loaded_html[0]


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
        menu_module=FakeMenuModule(),
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
        menu_module=FakeMenuModule(),
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
        menu_module=FakeMenuModule(),
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
        menu_module=FakeMenuModule(),
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
        menu_module=FakeMenuModule(),
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
        menu_module=FakeMenuModule(),
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


def test_launch_attaches_server_menu_to_start(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        menu_module=FakeMenuModule(),
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert len(fake_webview.start_calls) == 1
    menu = fake_webview.start_calls[0]["menu"]
    assert len(menu) == 1
    assert menu[0].title == "Server"
    assert "icon" not in fake_webview.start_calls[0]


def test_launch_passes_icon_only_when_icon_exists(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    icon_file = tmp_path / "icon.png"
    icon_file.write_bytes(b"fake-icon")

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
        webview_module=fake_webview,
        menu_module=FakeMenuModule(),
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
        menu_module=FakeMenuModule(),
        app_icon_path=tmp_path / "missing-icon.png",
    )

    # Proof the controller drove *the created window*: that exact FakeWindow saw
    # the navigation. (If attach_window were skipped, the controller would have
    # no window and raise.)
    assert fake_webview.window.loaded_urls == ["http://pi.lan:9000/?accessor=desktop"]


def test_launch_stops_worker_even_when_start_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    # Pre-enable wakeword so the bridge starts the worker before the loop; the
    # finally must still stop it when start() raises.
    settings_file.write_text(json.dumps({"wakeword": {"enabled": True}}), encoding="utf-8")
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

        bridge = DesktopBridge(
            settings_path=settings,
            worker_factory=lambda _bridge: RecordingWorker(),
            connection=controller,
        )
        bridge._start_worker()
        return bridge

    monkeypatch.setattr(desktop_main, "_create_wakeword_bridge", fake_bridge)

    with pytest.raises(RuntimeError, match="gui loop crashed"):
        desktop_main.launch_desktop(
            [],
            settings_file=settings_file,
            probe=lambda target: DesktopProbeResult(desktop_main.PROBE_WEBUI_AVAILABLE, target),
            webview_module=StartRaisesWebview(),
            menu_module=FakeMenuModule(),
            app_icon_path=tmp_path / "missing-icon.png",
        )

    assert stopped == [True]
