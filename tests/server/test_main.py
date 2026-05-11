"""Tests for server startup argument and port handling."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from core.utils.config import Config
from core.utils.logging import ManagedLoggerProxyHandler, QuietLogsWebSocketLifecycleFilter
from server import main as server_main
from server.main import DEFAULT_PORT, main, parse_args, resolve_port, resolve_server_bind


def test_parse_args_accepts_data_dir_and_port() -> None:
    args = parse_args(["--data-dir", "dev-data", "--port", "9000"])

    assert args.data_dir == "dev-data"
    assert args.port == 9000


def test_resolve_port_priority_explicit_then_environment_then_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"server_port": 8500}), encoding="utf-8")
    monkeypatch.setenv("VBOT_SERVER_PORT", "8600")
    config = Config(data_dir=tmp_path)

    assert resolve_port(config, 8700) == 8700
    assert resolve_port(config) == 8600


def test_resolve_port_uses_settings_then_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

    assert resolve_port(Config(data_dir=tmp_path)) == 8500
    assert resolve_port(Config(data_dir=tmp_path / "missing")) == DEFAULT_PORT


def test_resolve_port_ignores_ambient_port_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    monkeypatch.setenv("PORT", "8600")
    monkeypatch.setenv("SERVER_PORT", "8700")

    assert resolve_port(Config(data_dir=tmp_path)) == DEFAULT_PORT


def test_resolve_port_accepts_port_keys_from_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    monkeypatch.setenv("PORT", "8600")
    monkeypatch.setenv("SERVER_PORT", "8800")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"SERVER_PORT": 8700}), encoding="utf-8")

    assert resolve_port(Config(data_dir=tmp_path)) == 8700


def test_resolve_server_bind_tracks_host_port_and_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    (tmp_path / "settings.json").write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

    assert resolve_server_bind(Config(data_dir=tmp_path), host="0.0.0.0") == {
        "listen_host": "0.0.0.0",
        "listen_port": 8500,
        "port_source": "settings.server_port",
    }


def test_resolve_server_bind_uses_explicit_port_before_environment_and_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VBOT_SERVER_PORT", "8600")
    (tmp_path / "settings.json").write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

    assert resolve_server_bind(Config(data_dir=tmp_path), host="127.0.0.1", explicit_port=8700) == {
        "listen_host": "127.0.0.1",
        "listen_port": 8700,
        "port_source": "cli",
    }


def test_main_starts_uvicorn_with_configured_app(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(
        app,
        *,
        host: str,
        port: int,
        log_level: str,
        access_log: bool,
        log_config: dict[str, object],
    ) -> None:
        calls.append(
            {
                "app": app,
                "host": host,
                "port": port,
                "log_level": log_level,
                "access_log": access_log,
                "log_config": log_config,
            }
        )

    monkeypatch.setattr(server_main, "uvicorn", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        server_main,
        "create_app",
        lambda *, config, server_bind: {"config": config, "server_bind": server_bind},
    )

    main(["--data-dir", str(tmp_path / "data"), "--port", "8765"])

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8765
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


def test_managed_logger_proxy_handler_routes_records_into_vbot_namespace() -> None:
    handler = ManagedLoggerProxyHandler("vbot.server.uvicorn")
    logger = logging.getLogger("uvicorn.error")
    target_logger = logging.getLogger("vbot.server.uvicorn")
    captured_records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    capture_handler = CaptureHandler()
    original_target_level = target_logger.level
    original_target_propagate = target_logger.propagate
    logger.handlers = []
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    target_logger.addHandler(capture_handler)
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = True

    try:
        logger.info("Server started")
    finally:
        target_logger.removeHandler(capture_handler)
        target_logger.setLevel(original_target_level)
        target_logger.propagate = original_target_propagate
        logger.removeHandler(handler)
        handler.close()
        capture_handler.close()

    assert any(
        record.name == "vbot.server.uvicorn" and record.getMessage() == "Server started"
        for record in captured_records
    )


def test_managed_logger_proxy_handler_suppresses_logs_websocket_lifecycle_noise() -> None:
    handler = ManagedLoggerProxyHandler("vbot.server.uvicorn")
    handler.addFilter(QuietLogsWebSocketLifecycleFilter())
    logger = logging.getLogger("websockets.server")
    target_logger = logging.getLogger("vbot.server.uvicorn")
    captured_records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    capture_handler = CaptureHandler()
    original_target_level = target_logger.level
    original_target_propagate = target_logger.propagate
    logger.handlers = []
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    target_logger.addHandler(capture_handler)
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = True

    record = logger.makeRecord(
        name="websockets.server",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="connection open",
        args=(),
        exc_info=None,
    )
    record.websocket = SimpleNamespace(request=SimpleNamespace(path="/ws/logs"))

    try:
        logger.handle(record)
    finally:
        target_logger.removeHandler(capture_handler)
        target_logger.setLevel(original_target_level)
        target_logger.propagate = original_target_propagate
        logger.removeHandler(handler)
        handler.close()
        capture_handler.close()

    assert captured_records == []


def test_managed_logger_proxy_handler_keeps_logs_websocket_errors() -> None:
    handler = ManagedLoggerProxyHandler("vbot.server.uvicorn")
    handler.addFilter(QuietLogsWebSocketLifecycleFilter())
    logger = logging.getLogger("websockets.server")
    target_logger = logging.getLogger("vbot.server.uvicorn")
    captured_records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    capture_handler = CaptureHandler()
    original_target_level = target_logger.level
    original_target_propagate = target_logger.propagate
    logger.handlers = []
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    target_logger.addHandler(capture_handler)
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = True

    record = logger.makeRecord(
        name="websockets.server",
        level=logging.ERROR,
        fn=__file__,
        lno=1,
        msg="opening handshake failed",
        args=(),
        exc_info=None,
    )
    record.websocket = SimpleNamespace(request=SimpleNamespace(path="/ws/logs"))

    try:
        logger.handle(record)
    finally:
        target_logger.removeHandler(capture_handler)
        target_logger.setLevel(original_target_level)
        target_logger.propagate = original_target_propagate
        logger.removeHandler(handler)
        handler.close()
        capture_handler.close()

    assert any(
        record.name == "vbot.server.uvicorn" and record.getMessage() == "opening handshake failed"
        for record in captured_records
    )


def test_managed_logger_proxy_handler_keeps_non_lifecycle_logs_websocket_info() -> None:
    handler = ManagedLoggerProxyHandler("vbot.server.uvicorn")
    handler.addFilter(QuietLogsWebSocketLifecycleFilter())
    logger = logging.getLogger("websockets.server")
    target_logger = logging.getLogger("vbot.server.uvicorn")
    captured_records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    capture_handler = CaptureHandler()
    original_target_level = target_logger.level
    original_target_propagate = target_logger.propagate
    logger.handlers = []
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    target_logger.addHandler(capture_handler)
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = True

    record = logger.makeRecord(
        name="websockets.server",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="keepalive ping timeout",
        args=(),
        exc_info=None,
    )
    record.websocket = SimpleNamespace(request=SimpleNamespace(path="/ws/logs"))

    try:
        logger.handle(record)
    finally:
        target_logger.removeHandler(capture_handler)
        target_logger.setLevel(original_target_level)
        target_logger.propagate = original_target_propagate
        logger.removeHandler(handler)
        handler.close()
        capture_handler.close()

    assert any(
        record.name == "vbot.server.uvicorn" and record.getMessage() == "keepalive ping timeout"
        for record in captured_records
    )
