"""Tests for server startup argument and port handling."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.database import DataStoreMarker, read_marker
from core.utils.config import Config
from core.utils.logging import ManagedLoggerProxyHandler, QuietLogsWebSocketLifecycleFilter
from server import main as server_main
from server.main import DEFAULT_PORT, main, parse_args, resolve_port, resolve_server_bind

_PORT_SETTINGS = {"format_version": 1, "server_port": 8500}


def test_parse_args_accepts_data_dir_port_and_one_safe_startup_mode() -> None:
    args = parse_args(["--data-dir", "dev-data", "--port", "9000"])

    assert (args.data_dir, args.port) == ("dev-data", 9000)
    assert parse_args(["--verification-only"]).verification_only is True
    assert parse_args(["--test-instance"]).test_instance is True
    with pytest.raises(SystemExit):
        parse_args(["--verification-only", "--test-instance"])


@pytest.mark.parametrize(
    ("settings", "environment", "explicit_port", "port", "source"),
    [
        (_PORT_SETTINGS, {"VBOT_SERVER_PORT": "8600"}, 8700, 8700, "cli"),
        (_PORT_SETTINGS, {"VBOT_SERVER_PORT": "8600"}, None, 8600, "VBOT_SERVER_PORT"),
        (_PORT_SETTINGS, {}, None, 8500, "settings.server_port"),
        (
            {"format_version": 1, "SERVER_PORT": 8700},
            {"PORT": "8600", "SERVER_PORT": "8800"},
            None,
            8700,
            "settings.SERVER_PORT",
        ),
        (
            {**_PORT_SETTINGS, "debug": {"enabled": "yes"}},
            {},
            None,
            8500,
            "settings.server_port",
        ),
        (None, {"PORT": "8600", "SERVER_PORT": "8700"}, None, DEFAULT_PORT, "default"),
        ("{", {}, None, DEFAULT_PORT, "default"),
    ],
    ids=[
        "cli-first",
        "environment-before-settings",
        "settings",
        "settings-uppercase-key-ignores-ambient-environment",
        "valid-port-next-to-invalid-setting",
        "default-ignores-ambient-environment",
        "default-for-unreadable-settings",
    ],
)
def test_server_bind_resolves_cli_then_environment_then_settings_then_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any] | str | None,
    environment: dict[str, str],
    explicit_port: int | None,
    port: int,
    source: str,
) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    if settings is not None:
        text = settings if isinstance(settings, str) else json.dumps(settings)
        (tmp_path / "settings.json").write_text(text, encoding="utf-8")
    config = Config(data_dir=tmp_path)

    assert resolve_server_bind(config, host="0.0.0.0", explicit_port=explicit_port) == {
        "listen_host": "0.0.0.0",
        "listen_port": port,
        "port_source": source,
    }
    assert resolve_port(config, explicit_port) == port


@pytest.mark.parametrize("action", ["shutdown", "restart", "restart_failure"])
def test_main_starts_uvicorn_with_configured_app(tmp_path: Path, monkeypatch, action: str) -> None:
    from cli import server_management

    calls: list[dict[str, Any]] = []
    schedule = MagicMock(return_value=SimpleNamespace(ok=action != "restart_failure"))
    loop = MagicMock()
    monkeypatch.setattr(server_management, "schedule_server_restart", schedule)
    monkeypatch.setattr(server_main.asyncio, "get_running_loop", lambda: loop)

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            self.app = app
            calls.append({"app": app, **kwargs})

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config
            self.should_exit = False

        def run(self) -> None:
            self.config.app["on_ready"]()
            if action == "restart_failure":
                with pytest.raises(RuntimeError):
                    self.config.app["request_restart"]()
                assert not self.should_exit
                loop.call_later.assert_not_called()
            elif action == "restart":
                self.config.app["request_restart"]()
                self.config.app["request_restart"]()
                schedule.assert_called_once()
                instance = schedule.call_args.args[0]
                assert instance.port == 8765 and instance.host == "127.0.0.1"
                assert instance.data_dir == tmp_path / "data"
                assert schedule.call_args.kwargs == {"wait_pid": server_main.os.getpid()}
                assert not self.should_exit
                delay, callback = loop.call_later.call_args.args
                assert delay > 0
                callback()
                assert self.should_exit
            self.config.app["request_shutdown"]()
            assert self.should_exit is True

    monkeypatch.setattr(
        server_main,
        "uvicorn",
        SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )
    monkeypatch.setattr(server_main, "activate_process_containment", lambda: None)

    def create_app(
        *, config, server_bind, shutdown_token, request_shutdown, request_restart, on_ready
    ) -> dict[str, Any]:
        return {
            "config": config,
            "server_bind": server_bind,
            "shutdown_token": shutdown_token,
            "request_shutdown": request_shutdown,
            "request_restart": request_restart,
            "on_ready": on_ready,
        }

    monkeypatch.setattr(server_main, "create_app", create_app)
    # The real hook would freeze the test process heap for the rest of the session.
    heap: list[str] = []
    monkeypatch.setattr(server_main.gc, "collect", lambda: heap.append("collect"))
    monkeypatch.setattr(server_main.gc, "freeze", lambda: heap.append("freeze"))

    main(["--data-dir", str(tmp_path / "data"), "--port", "8765"])

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8765
    assert calls[0]["ws_per_message_deflate"] is False
    assert calls[0]["log_level"] == "info"
    assert calls[0]["access_log"] is False
    assert calls[0]["log_config"]["handlers"]["vbot_proxy"] == {
        "class": "core.utils.logging.ManagedLoggerProxyHandler",
        "target_logger_name": "vbot.server.uvicorn",
        "filters": ["quiet_logs_websocket_lifecycle"],
    }
    assert calls[0]["log_config"]["filters"]["quiet_logs_websocket_lifecycle"] == {
        "()": "core.utils.logging.QuietLogsWebSocketLifecycleFilter",
    }
    assert calls[0]["log_config"]["loggers"]["uvicorn.access"] == {
        "handlers": ["null"],
        "level": "INFO",
        "propagate": False,
    }
    assert calls[0]["log_config"]["loggers"]["websockets.server"] == {
        "handlers": ["vbot_proxy"],
        "level": "INFO",
        "propagate": False,
    }
    assert calls[0]["app"]["server_bind"] == {
        "listen_host": "127.0.0.1",
        "listen_port": 8765,
        "port_source": "cli",
    }
    assert calls[0]["app"]["shutdown_token"]
    # Once ready, the server freezes its collected startup heap.
    assert heap == ["collect", "freeze"]
    assert not (tmp_path / "data" / "runtime" / "server-8765.json").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_main_initializes_only_a_missing_data_directory(
    tmp_path: Path, monkeypatch, existing: bool
) -> None:
    data_dir = tmp_path / "data"
    if existing:
        data_dir.mkdir()
    markers: list[DataStoreMarker | None] = []

    class FakeServer:
        def __init__(self, config: object) -> None:
            self.config = config

        def run(self) -> None:
            markers.append(read_marker(data_dir))

    monkeypatch.setattr(
        server_main,
        "uvicorn",
        SimpleNamespace(Config=lambda app, **_kwargs: app, Server=FakeServer),
    )
    monkeypatch.setattr(server_main, "activate_process_containment", lambda: None)
    monkeypatch.setattr(server_main, "create_app", lambda **kwargs: kwargs)

    main(["--data-dir", str(data_dir), "--port", "8765"])

    assert len(markers) == 1
    if existing:
        # An existing root is never authorized here; Runtime startup decides.
        assert markers[0] is None
        assert not (data_dir / "agents").exists()
    else:
        assert markers[0] is not None
        assert markers[0].databases == {}


def test_uvicorn_log_proxy_forwards_formatted_records_except_websocket_lifecycle_noise() -> None:
    # server.main wires this handler and filter into uvicorn's log config.
    handler = ManagedLoggerProxyHandler("vbot.server.uvicorn")
    handler.addFilter(QuietLogsWebSocketLifecycleFilter())
    target_logger = logging.getLogger("vbot.server.uvicorn")
    captured: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    capture_handler = CaptureHandler()
    original_level, original_propagate = target_logger.level, target_logger.propagate
    target_logger.addHandler(capture_handler)
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = True
    records: list[tuple[str, int, str, tuple[object, ...], str | None]] = [
        ("uvicorn.error", logging.INFO, "Server started", (), None),
        ("websockets.server", logging.INFO, "connection open", (), "/ws/logs"),
        ("websockets.server", logging.INFO, "connection closed", (), "/ws"),
        ("uvicorn.error", logging.INFO, "connection open", (), None),
        ("uvicorn.error", logging.INFO, '127.0.0.1:55090 - "WebSocket /ws" [accepted]', (), None),
        ("websockets.server", logging.ERROR, "opening handshake failed", (), "/ws/logs"),
        ("websockets.server", logging.INFO, "keepalive ping timeout", (), "/ws/logs"),
        (
            "websockets.server",
            logging.INFO,
            '%s - "WebSocket %s" [rejected]',
            ("127.0.0.1", "/ws"),
            "/ws",
        ),
    ]

    try:
        for name, level, message, args, path in records:
            record = logging.LogRecord(name, level, __file__, 1, message, args, None)
            if path is not None:
                record.websocket = SimpleNamespace(request=SimpleNamespace(path=path))
            handler.handle(record)
    finally:
        target_logger.removeHandler(capture_handler)
        target_logger.setLevel(original_level)
        target_logger.propagate = original_propagate
        handler.close()

    assert [(record.name, record.levelno, record.getMessage()) for record in captured] == [
        ("vbot.server.uvicorn", logging.INFO, "Server started"),
        ("vbot.server.uvicorn", logging.ERROR, "opening handshake failed"),
        ("vbot.server.uvicorn", logging.INFO, "keepalive ping timeout"),
        ("vbot.server.uvicorn", logging.INFO, '127.0.0.1 - "WebSocket /ws" [rejected]'),
    ]
