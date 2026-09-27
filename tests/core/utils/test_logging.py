"""Tests for shared logging infrastructure."""

import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.utils.logging import (
    DailyFileHandler,
    LogManager,
    ManagedLoggerProxyHandler,
    QuietLogsWebSocketLifecycleFilter,
    is_logs_websocket_lifecycle_record,
    is_routine_websocket_lifecycle_message,
    resolve_daily_log_path,
)


def make_websocket_record(
    *,
    name: str = "websockets.server",
    level: int = logging.INFO,
    message: str,
    path: str | None = None,
    args: tuple[object, ...] = (),
) -> logging.LogRecord:
    """Build a websocket-flavored log record for filter tests."""

    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )
    if path is not None:
        record.websocket = SimpleNamespace(request=SimpleNamespace(path=path))
    return record


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


def test_log_manager_writes_every_vbot_logger_to_the_daily_file(tmp_path: Path) -> None:
    """Managed and direct vbot loggers write the structured format to one daily file."""
    vbot_logger = logging.getLogger("vbot")
    vbot_logger.handlers = []
    manager = LogManager(
        level="INFO",
        data_dir=tmp_path,
        current_date_provider=lambda: date(2026, 5, 10),
    )

    try:
        manager.get_logger("core").warning("Structured warning")
        logging.getLogger("vbot.runtime.direct").info("Inherited handler path")
    finally:
        manager.close()

    log_path = tmp_path / "logs" / "2026-05-10.log"
    assert manager.log_file_path == log_path
    assert resolve_daily_log_path(tmp_path, current_date_provider=lambda: date(2026, 5, 10)) == (
        log_path
    )
    warning, direct = log_path.read_text(encoding="utf-8").splitlines()
    assert warning.endswith("[WARN] vbot.core - Structured warning")
    assert warning[:19].count(":") == 2
    assert warning[4] == "-"
    assert direct.endswith("[INFO] vbot.runtime.direct - Inherited handler path")
    # Closing the manager restores the namespace's propagation.
    assert vbot_logger.propagate is True


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

    assert (tmp_path / "logs" / "2026-05-10.log").read_text(encoding="utf-8").strip() == "first day"
    assert (tmp_path / "logs" / "2026-05-11.log").read_text(
        encoding="utf-8"
    ).strip() == "second day"


_ACCEPTED = '%s - "WebSocket %s" [accepted]'


@pytest.mark.parametrize(
    ("record", "routine"),
    [
        (make_websocket_record(message="connection open", path="/ws/logs?cursor=abc"), True),
        (make_websocket_record(message="connection closed", path="/ws?after_sequence=4"), True),
        (make_websocket_record(message=_ACCEPTED, args=("127.0.0.1", "/ws/logs?cursor=abc")), True),
        # Runtime records without a path: the uvicorn logger marks them as websocket records.
        (make_websocket_record(name="uvicorn.error", message="connection open"), True),
        (
            make_websocket_record(
                name="uvicorn.error", message='127.0.0.1:55090 - "WebSocket /ws" [accepted]'
            ),
            True,
        ),
        # Only INFO lifecycle records are routine: diagnostics and errors stay.
        (
            make_websocket_record(level=logging.DEBUG, message="connection open", path="/ws/logs"),
            False,
        ),
        (
            make_websocket_record(
                level=logging.ERROR, message="opening handshake failed", path="/ws/logs"
            ),
            False,
        ),
        (make_websocket_record(message="keepalive ping timeout", path="/ws"), False),
        (make_websocket_record(message=_ACCEPTED, args=("127.0.0.1", "/ws/other")), False),
        (make_websocket_record(name="some.other.logger", message="connection open"), False),
    ],
    ids=[
        "log-stream-open",
        "app-socket-closed",
        "log-stream-accepted",
        "uvicorn-open-without-path",
        "uvicorn-accepted-without-args",
        "debug-diagnostic",
        "error",
        "non-lifecycle-info",
        "other-websocket-path",
        "other-logger-without-path",
    ],
)
def test_websocket_lifecycle_filter_quiets_only_routine_info_records(
    record: logging.LogRecord, routine: bool
) -> None:
    assert is_logs_websocket_lifecycle_record(record) is routine
    assert QuietLogsWebSocketLifecycleFilter().filter(record) is not routine


def test_logger_proxy_forwards_formatted_records_that_pass_its_filters() -> None:
    # server.main wires this handler and filter into uvicorn's log config.
    handler = ManagedLoggerProxyHandler("vbot.server.uvicorn")
    handler.addFilter(QuietLogsWebSocketLifecycleFilter())
    target_logger = logging.getLogger("vbot.server.uvicorn")
    captured: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    capture_handler = CaptureHandler()
    target_logger.addHandler(capture_handler)
    target_logger.setLevel(logging.INFO)
    try:
        for record in (
            make_websocket_record(name="uvicorn.error", message="Server started"),
            make_websocket_record(message="connection open", path="/ws/logs"),
            make_websocket_record(
                message='%s - "WebSocket %s" [rejected]', args=("127.0.0.1", "/ws"), path="/ws"
            ),
        ):
            handler.handle(record)
    finally:
        target_logger.removeHandler(capture_handler)
        handler.close()

    # Forwarded records keep their level, carry the proxy's logger name and a
    # formatted message; the routine lifecycle record is filtered out.
    assert [(record.name, record.levelno, record.getMessage()) for record in captured] == [
        ("vbot.server.uvicorn", logging.INFO, "Server started"),
        ("vbot.server.uvicorn", logging.INFO, '127.0.0.1 - "WebSocket /ws" [rejected]'),
    ]


def test_persisted_websocket_lifecycle_messages_match_by_level_name() -> None:
    assert (
        is_routine_websocket_lifecycle_message(
            level="INFO",
            logger_name="vbot.server.uvicorn",
            message='127.0.0.1:55090 - "WebSocket /ws" [accepted]',
        )
        is True
    )
    assert (
        is_routine_websocket_lifecycle_message(
            level="ERROR",
            logger_name="vbot.server.uvicorn",
            message='127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" handshake failed',
        )
        is False
    )
