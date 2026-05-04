"""Tests for Desktop target configuration and local settings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from desktop import main as desktop_main


@dataclass
class FakeResponse:
    status_code: int
    payload: Any = None

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeWebview:
    def __init__(self) -> None:
        self.created_windows: list[tuple[str, dict[str, Any]]] = []
        self.start_calls: list[dict[str, Any]] = []

    def create_window(self, title: str, **kwargs: Any) -> object:
        self.created_windows.append((title, kwargs))
        return object()

    def start(self, **kwargs: Any) -> None:
        self.start_calls.append(kwargs)


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


def test_parse_args_accepts_host_and_port() -> None:
    args = desktop_main.parse_args(["--host", "192.168.1.50", "--port", "9000"])

    assert args.host == "192.168.1.50"
    assert args.port == 9000


def test_resolve_target_uses_defaults_when_settings_are_missing(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"

    target = desktop_main.resolve_target([], settings_file=settings_file)

    assert target.host == desktop_main.DEFAULT_HOST
    assert target.port == desktop_main.DEFAULT_PORT
    assert target.url == "http://127.0.0.1:8420/"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "host": desktop_main.DEFAULT_HOST,
        "port": desktop_main.DEFAULT_PORT,
    }


def test_cli_args_override_saved_settings_per_field(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"host": "10.0.0.8", "port": 8765}), encoding="utf-8")

    host_override = desktop_main.resolve_target(
        ["--host", "localhost"], settings_file=settings_file
    )
    port_override = desktop_main.resolve_target(["--port", "9001"], settings_file=settings_file)

    assert host_override.host == "localhost"
    assert host_override.port == 8765
    assert host_override.url == "http://localhost:8765/"
    assert port_override.host == "localhost"
    assert port_override.port == 9001
    assert port_override.url == "http://localhost:9001/"


def test_settings_can_partially_override_defaults(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"host": "vbot.lan"}), encoding="utf-8")

    target = desktop_main.resolve_target([], settings_file=settings_file)

    assert target.host == "vbot.lan"
    assert target.port == desktop_main.DEFAULT_PORT
    assert target.url == "http://vbot.lan:8420/"


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_parse_args_rejects_invalid_ports(port: str) -> None:
    with pytest.raises(SystemExit):
        desktop_main.parse_args(["--port", port])


@pytest.mark.parametrize("port", [0, 65536, "not-a-port", None])
def test_resolve_target_rejects_invalid_settings_ports(tmp_path: Path, port: object) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"port": port}), encoding="utf-8")

    with pytest.raises(ValueError, match="settings.port"):
        desktop_main.resolve_target([], settings_file=settings_file)


def test_settings_file_lives_next_to_desktop_main() -> None:
    assert (
        desktop_main.settings_path()
        == Path(desktop_main.__file__).resolve().parent / "settings.json"
    )


def test_settings_writes_use_desktop_local_file_not_server_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_dir = tmp_path / "desktop"
    server_data_dir = tmp_path / "server-data"
    settings_file = desktop_main.settings_path(desktop_dir)
    monkeypatch.setenv("VBOT_DATA_DIR", str(server_data_dir))

    target = desktop_main.resolve_target(
        ["--host", "10.1.2.3", "--port", "8500"],
        settings_file=settings_file,
    )

    assert target.url == "http://10.1.2.3:8500/"
    assert settings_file.exists()
    assert not (server_data_dir / "settings.json").exists()
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "host": "10.1.2.3",
        "port": 8500,
    }


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


def test_probe_target_classifies_available_webui() -> None:
    target = desktop_main.DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

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
    target = desktop_main.DesktopTarget("vbot.lan", 9000, "http://vbot.lan:9000/")

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
    target = desktop_main.DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

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
    target = desktop_main.DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

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
    target = desktop_main.DesktopTarget("127.0.0.1", 8420, "http://127.0.0.1:8420/")

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
        FakeResponse(200, ValueError("invalid json")),
        FakeResponse(200, ["ok"]),
    ],
)
def test_probe_target_classifies_non_vbot_server(health_response: FakeResponse) -> None:
    target = desktop_main.DesktopTarget("example.test", 8080, "http://example.test:8080/")

    result = desktop_main.probe_target(
        target,
        get=fake_get_for({"http://example.test:8080/health": health_response}),
    )

    assert result.status == desktop_main.PROBE_NOT_VBOT_SERVER


def test_choose_window_content_returns_url_only_for_available_webui() -> None:
    target = desktop_main.DesktopTarget("10.0.0.5", 9000, "http://10.0.0.5:9000/")

    content = desktop_main.choose_window_content(
        target,
        probe=lambda checked_target: desktop_main.DesktopProbeResult(
            desktop_main.PROBE_WEBUI_AVAILABLE,
            checked_target,
        ),
    )

    assert content.status == desktop_main.PROBE_WEBUI_AVAILABLE
    assert content.url == "http://10.0.0.5:9000/"
    assert content.html is None


@pytest.mark.parametrize(
    ("status", "expected_text"),
    [
        (desktop_main.PROBE_SERVER_UNREACHABLE, "Server unreachable"),
        (desktop_main.PROBE_WEBUI_UNAVAILABLE, "WebUI unavailable"),
        (desktop_main.PROBE_NOT_VBOT_SERVER, "Not a vBot server"),
    ],
)
def test_choose_window_content_returns_inline_html_for_failures(
    status: str,
    expected_text: str,
) -> None:
    target = desktop_main.DesktopTarget("vbot.lan", 8420, "http://vbot.lan:8420/")

    content = desktop_main.choose_window_content(
        target,
        probe=lambda checked_target: desktop_main.DesktopProbeResult(status, checked_target),
    )

    assert content.status == status
    assert content.url is None
    assert content.html is not None
    assert expected_text in content.html
    assert "http://vbot.lan:8420/" in content.html


def test_fallback_html_escapes_target_context() -> None:
    target = desktop_main.DesktopTarget(
        '<script>alert("x")</script>',
        8420,
        'http://<script>alert("x")</script>:8420/',
    )

    fallback_html = desktop_main.build_fallback_html(
        desktop_main.DesktopProbeResult(desktop_main.PROBE_SERVER_UNREACHABLE, target)
    )

    assert '<script>alert("x")</script>' not in fallback_html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in fallback_html


def test_launch_window_creates_url_window_without_js_bridge(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    missing_icon = tmp_path / "missing-icon.png"

    desktop_main.launch_window(
        desktop_main.DesktopWindowContent(
            status=desktop_main.PROBE_WEBUI_AVAILABLE,
            url="http://127.0.0.1:8420/",
        ),
        webview_module=fake_webview,
        app_icon_path=missing_icon,
    )

    assert fake_webview.created_windows == [
        (desktop_main.WINDOW_TITLE, {"url": "http://127.0.0.1:8420/"})
    ]
    assert "js_api" not in fake_webview.created_windows[0][1]
    assert fake_webview.start_calls == [{}]


def test_launch_window_creates_html_window_without_js_bridge(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_window(
        desktop_main.DesktopWindowContent(
            status=desktop_main.PROBE_SERVER_UNREACHABLE,
            html="<p>Server unreachable</p>",
        ),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert fake_webview.created_windows == [
        (desktop_main.WINDOW_TITLE, {"html": "<p>Server unreachable</p>"})
    ]
    assert "js_api" not in fake_webview.created_windows[0][1]
    assert fake_webview.start_calls == [{}]


def test_launch_window_passes_icon_only_when_icon_exists(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    icon_file = tmp_path / "icon.png"
    icon_file.write_bytes(b"fake-icon")

    desktop_main.launch_window(
        desktop_main.DesktopWindowContent(
            status=desktop_main.PROBE_WEBUI_AVAILABLE,
            url="http://vbot.lan:9000/",
        ),
        webview_module=fake_webview,
        app_icon_path=icon_file,
    )

    assert fake_webview.start_calls == [{"icon": str(icon_file)}]


def test_launch_desktop_resolves_probes_and_creates_one_window(tmp_path: Path) -> None:
    fake_webview = FakeWebview()
    settings_file = tmp_path / "settings.json"
    checked_targets: list[desktop_main.DesktopTarget] = []

    def record_available_probe(
        checked_target: desktop_main.DesktopTarget,
    ) -> desktop_main.DesktopProbeResult:
        checked_targets.append(checked_target)
        return desktop_main.DesktopProbeResult(
            desktop_main.PROBE_WEBUI_AVAILABLE,
            checked_target,
        )

    target = desktop_main.launch_desktop(
        ["--host", "10.0.0.10", "--port", "8500"],
        settings_file=settings_file,
        probe=record_available_probe,
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert target.url == "http://10.0.0.10:8500/"
    assert checked_targets == [target]
    assert fake_webview.created_windows == [
        (desktop_main.WINDOW_TITLE, {"url": "http://10.0.0.10:8500/"})
    ]
    assert fake_webview.start_calls == [{}]


def test_launch_desktop_uses_inline_html_for_expected_failures(tmp_path: Path) -> None:
    fake_webview = FakeWebview()

    desktop_main.launch_desktop(
        [],
        settings_file=tmp_path / "settings.json",
        probe=lambda checked_target: desktop_main.DesktopProbeResult(
            desktop_main.PROBE_WEBUI_UNAVAILABLE,
            checked_target,
        ),
        webview_module=fake_webview,
        app_icon_path=tmp_path / "missing-icon.png",
    )

    assert len(fake_webview.created_windows) == 1
    title, window_kwargs = fake_webview.created_windows[0]
    assert title == desktop_main.WINDOW_TITLE
    assert "url" not in window_kwargs
    assert "html" in window_kwargs
    assert "WebUI unavailable" in window_kwargs["html"]


def test_launch_window_rejects_empty_window_content(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires either url or html"):
        desktop_main.launch_window(
            desktop_main.DesktopWindowContent(status=desktop_main.PROBE_WEBUI_AVAILABLE),
            webview_module=FakeWebview(),
            app_icon_path=tmp_path / "missing-icon.png",
        )
