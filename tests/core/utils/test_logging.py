"""Tests for shared logging infrastructure."""

import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from core.utils.logging import (
    DailyFileHandler,
    LogManager,
    QuietLogsWebSocketLifecycleFilter,
    is_logs_websocket_lifecycle_record,
)


class DateSequence:
    """Deterministic date provider for daily log rotation tests."""

    def __init__(self, *values: date) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> date:
        if self._index < len(self._values) - 1:
            value = self._values[self._index]
            self._index += 1
            return value
        return self._values[-1]


def test_log_manager_writes_exact_format_to_daily_log_file(tmp_path: Path) -> None:
    """Managed loggers write the required structured format to the daily file."""
    logging.getLogger("vbot").handlers = []
    manager = LogManager(level="INFO", data_dir=tmp_path)

    try:
        logger = manager.get_logger("core")
        logger.warning("Structured warning")
    finally:
        manager.close()

    log_path = tmp_path / "logs" / date.today().isoformat()
    assert log_path.exists()
    line = log_path.read_text(encoding="utf-8").strip()
    assert line.endswith("[WARN] vbot.core - Structured warning")
    assert line[:19].count(":") == 2
    assert line[4] == "-"


def test_log_manager_configures_vbot_namespace_for_direct_loggers(tmp_path: Path) -> None:
    """Direct vbot loggers inherit the shared handlers and file contract."""
    logging.getLogger("vbot").handlers = []
    manager = LogManager(level="INFO", data_dir=tmp_path)

    try:
        manager.get_logger("core")
        direct_logger = logging.getLogger("vbot.runtime.direct")
        direct_logger.info("Inherited handler path")
    finally:
        manager.close()

    log_path = tmp_path / "logs" / date.today().isoformat()
    contents = log_path.read_text(encoding="utf-8")
    assert "[INFO] vbot.runtime.direct - Inherited handler path" in contents


def test_log_manager_resolves_daily_log_path_from_current_date(tmp_path: Path) -> None:
    """The active file path is derived from the provided current date."""
    logging.getLogger("vbot").handlers = []
    target_date = date(2026, 5, 10)
    manager = LogManager(
        level="INFO",
        data_dir=tmp_path,
        current_date_provider=lambda: target_date,
    )

    try:
        assert manager.log_file_path == tmp_path / "logs" / "2026-05-10"
        logger = manager.get_logger("core")
        logger.info("Daily file resolved")
    finally:
        manager.close()

    assert (tmp_path / "logs" / "2026-05-10").exists()


def test_daily_file_handler_rotates_when_date_changes(tmp_path: Path) -> None:
    """Daily file handler switches files when the day rolls over."""
    dates = DateSequence(date(2026, 5, 10), date(2026, 5, 10), date(2026, 5, 11))
    handler = DailyFileHandler(tmp_path / "logs", current_date_provider=dates)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("tests.daily-file-handler")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)

    try:
        logger.info("first day")
        logger.info("second day")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert (tmp_path / "logs" / "2026-05-10").read_text(encoding="utf-8").strip() == "first day"
    assert (tmp_path / "logs" / "2026-05-11").read_text(encoding="utf-8").strip() == "second day"


def test_logs_websocket_lifecycle_filter_suppresses_info_records_for_log_stream() -> None:
    record = logging.LogRecord(
        name="websockets.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connection open",
        args=(),
        exc_info=None,
    )
    record.websocket = SimpleNamespace(request=SimpleNamespace(path="/ws/logs?cursor=abc"))

    assert is_logs_websocket_lifecycle_record(record) is True
    assert QuietLogsWebSocketLifecycleFilter().filter(record) is False


def test_logs_websocket_lifecycle_filter_suppresses_accepted_handshake_info_for_log_stream() -> (
    None
):
    record = logging.LogRecord(
        name="websockets.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1", "/ws/logs?cursor=abc"),
        exc_info=None,
    )

    assert is_logs_websocket_lifecycle_record(record) is True
    assert QuietLogsWebSocketLifecycleFilter().filter(record) is False


def test_logs_websocket_lifecycle_filter_keeps_debug_diagnostics_for_log_stream() -> None:
    record = logging.LogRecord(
        name="websockets.server",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="connection open",
        args=(),
        exc_info=None,
    )
    record.websocket = SimpleNamespace(request=SimpleNamespace(path="/ws/logs"))

    assert is_logs_websocket_lifecycle_record(record) is False
    assert QuietLogsWebSocketLifecycleFilter().filter(record) is True


def test_logs_websocket_lifecycle_filter_keeps_errors_for_log_stream() -> None:
    record = logging.LogRecord(
        name="websockets.server",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="opening handshake failed",
        args=(),
        exc_info=None,
    )
    record.websocket = SimpleNamespace(request=SimpleNamespace(path="/ws/logs"))

    assert is_logs_websocket_lifecycle_record(record) is False
    assert QuietLogsWebSocketLifecycleFilter().filter(record) is True


def test_logs_websocket_lifecycle_filter_keeps_non_log_stream_websocket_info() -> None:
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1", "/ws"),
        exc_info=None,
    )

    assert is_logs_websocket_lifecycle_record(record) is False
    assert QuietLogsWebSocketLifecycleFilter().filter(record) is True


def test_logs_websocket_lifecycle_filter_keeps_non_lifecycle_info_for_log_stream() -> None:
    record = logging.LogRecord(
        name="websockets.server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="keepalive ping timeout",
        args=(),
        exc_info=None,
    )
    record.websocket = SimpleNamespace(request=SimpleNamespace(path="/ws/logs"))

    assert is_logs_websocket_lifecycle_record(record) is False
    assert QuietLogsWebSocketLifecycleFilter().filter(record) is True
