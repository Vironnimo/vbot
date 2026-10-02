"""Tests for shared logging infrastructure."""

import logging
import os
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override

import pytest

from core.utils import logging as logging_module
from core.utils.logging import (
    DailyFileHandler,
    LogManager,
    ManagedLoggerProxyHandler,
    QuietLogsWebSocketLifecycleFilter,
    is_logs_websocket_lifecycle_record,
    is_routine_websocket_lifecycle_message,
    register_log_channel_ids,
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


def _messages(log_path: Path) -> list[str]:
    """Return the ``[LEVEL] name - message`` part of every line in *log_path*."""
    return [line.split(" ", 2)[2] for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_log_manager_writes_every_vbot_logger_to_the_daily_file(tmp_path: Path) -> None:
    """A constructed manager is active at once; a nested one hands back on close."""
    vbot_logger = logging.getLogger("vbot")
    vbot_logger.handlers = []
    outer = LogManager(
        level="INFO",
        data_dir=tmp_path / "outer",
        current_date_provider=lambda: date(2026, 5, 10),
    )

    try:
        logging.getLogger("vbot.runtime.direct").info("Inherited handler path")
        inner = LogManager(
            level="INFO",
            data_dir=tmp_path / "inner",
            current_date_provider=lambda: date(2026, 5, 10),
        )
        inner.get_logger("core").warning("Structured warning")
        inner.close()
        logging.getLogger("vbot.cli").info("Outer pipeline again")
    finally:
        outer.close()

    outer_log = tmp_path / "outer" / "logs" / "2026-05-10.log"
    assert outer.log_file_path == outer_log
    assert resolve_daily_log_path(
        tmp_path / "outer", current_date_provider=lambda: date(2026, 5, 10)
    ) == (outer_log)
    first_line = outer_log.read_text(encoding="utf-8").splitlines()[0]
    assert first_line[:19].count(":") == 2
    assert first_line[4] == "-"
    assert _messages(outer_log) == [
        "[INFO] vbot.runtime.direct - Inherited handler path",
        "[INFO] vbot.cli - Outer pipeline again",
    ]
    assert _messages(tmp_path / "inner" / "logs" / "2026-05-10.log") == [
        "[WARN] vbot.core - Structured warning"
    ]
    # Closing the last manager restores the namespace's propagation.
    assert vbot_logger.propagate is True


def test_log_manager_writes_other_loggers_warnings_once_under_their_own_name(
    tmp_path: Path,
) -> None:
    manager = LogManager(
        level="INFO",
        data_dir=tmp_path,
        enable_console=False,
        current_date_provider=lambda: date(2026, 5, 10),
    )
    third_party = logging.getLogger("tests.third_party")
    try:
        third_party.info("Routine detail")
        third_party.warning("Polling failed")
        # Windows' Proactor reports a peer that went away as a failed callback.
        try:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed")
        except ConnectionResetError as error:
            logging.getLogger("asyncio").error(
                "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
                exc_info=error,
            )
    finally:
        manager.close()
    third_party.warning("After close")

    assert _messages(tmp_path / "logs" / "2026-05-10.log") == [
        "[WARN] tests.third_party - Polling failed"
    ]


def test_log_manager_writes_channel_session_ids_with_pseudonymous_platform_part(
    tmp_path: Path,
) -> None:
    """Platform chat and user ids never reach a line; one conversation keeps one pseudonym."""
    manager = LogManager(
        level="INFO",
        data_dir=tmp_path,
        enable_console=False,
        current_date_provider=lambda: date(2026, 5, 10),
    )
    logger = manager.get_logger("runs")
    try:
        logger.info("Run completed (session=ch-pseudo-tg--1001234567-u424242)")
        register_log_channel_ids(["pseudo-tg"])
        logger.info("Run completed (session=ch-pseudo-tg--1001234567-u424242)")
        logger.info("Run completed (session=ch-pseudo-tg-main)")
        try:
            raise KeyError("ch-pseudo-tg-987654")
        except KeyError:
            logger.exception("Session lookup failed")
    finally:
        manager.close()

    text = (tmp_path / "logs" / "2026-05-10.log").read_text(encoding="utf-8")
    for platform_id in ("1001234567", "424242", "987654"):
        assert platform_id not in text
    unregistered = re.search(r"session=ch-#([0-9a-f]{6})\)", text)
    registered = re.search(r"session=ch-pseudo-tg-#([0-9a-f]{6})\)", text)
    assert unregistered is not None and registered is not None
    assert unregistered.group(1) == registered.group(1)
    assert "session=ch-pseudo-tg-main)" in text
    assert re.search(r"KeyError: 'ch-pseudo-tg-#[0-9a-f]{6}'", text)


def _write_through(handler: DailyFileHandler, *messages: str) -> None:
    """Write *messages* through *handler* alone, then close it (awaiting its sweep)."""
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("tests.daily-file-handler")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for message in messages:
            logger.info(message)
    finally:
        logger.removeHandler(handler)
        handler.close()


def _write_files(logs_dir: Path, sizes: dict[str, int]) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    for name, size in sizes.items():
        (logs_dir / name).write_bytes(b"x" * size)


def _retention_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, dict[str, str]]]:
    """Return the level and ``key=value`` fields of every retention record."""
    return [
        (record.levelno, dict(re.findall(r"(\w+)=([^\s)]+)", record.getMessage())))
        for record in caplog.records
        if record.name == "vbot.logging"
    ]


def test_daily_file_handler_rotates_when_date_changes(tmp_path: Path) -> None:
    """Daily file handler switches files when the day rolls over."""
    dates = DateSequence(date(2026, 5, 10), date(2026, 5, 10), date(2026, 5, 11))

    _write_through(
        DailyFileHandler(tmp_path / "logs", current_date_provider=dates), "first day", "second day"
    )

    assert (tmp_path / "logs" / "2026-05-10.log").read_text(encoding="utf-8").strip() == "first day"
    assert (tmp_path / "logs" / "2026-05-11.log").read_text(
        encoding="utf-8"
    ).strip() == "second day"


def test_opening_a_day_deletes_only_daily_log_files_past_the_retention(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """At start and at each rollover, in the background; nothing else in the directory."""
    caplog.set_level(logging.INFO, logger="vbot.logging")
    logs_dir = tmp_path / "logs"
    # 90 days before 2026-09-29 is 2026-07-01; before 2026-09-30 it is 2026-07-02.
    _write_files(logs_dir, {"2026-06-30.log": 30, "2026-07-01.log": 20, "2026-07-02.log": 10})
    unrelated = [
        "2026-06-01",
        "2026-06-01.log.1",
        "2026-06-02.LOG",
        "2026-13-45.log",
        "server-startup.log",
        "notes.txt",
    ]
    _write_files(logs_dir, dict.fromkeys(unrelated, 1))
    (logs_dir / "2026-06-03.log").mkdir()
    dates = DateSequence(date(2026, 9, 29), date(2026, 9, 29), date(2026, 9, 30))

    _write_through(DailyFileHandler(logs_dir, current_date_provider=dates), "start", "next day")

    assert sorted(path.name for path in logs_dir.iterdir()) == sorted(
        [*unrelated, "2026-06-03.log", "2026-07-02.log", "2026-09-29.log", "2026-09-30.log"]
    )
    assert _retention_lines(caplog) == [
        (
            logging.INFO,
            {
                "count": "1",
                "bytes": "30",
                "reason": "age",
                "oldest": "2026-06-30",
                "newest": "2026-06-30",
            },
        ),
        (
            logging.INFO,
            {
                "count": "1",
                "bytes": "20",
                "reason": "age",
                "oldest": "2026-07-01",
                "newest": "2026-07-01",
            },
        ),
    ]
    # Never on the thread that wrote the record, which may be the Event Loop.
    assert all(
        (record.threadName or "").startswith("vbot-log-retention")
        for record in caplog.records
        if record.name == "vbot.logging"
    )


@pytest.mark.parametrize(
    ("current_day_bytes", "deleted"),
    [
        (30, ["2026-09-26.log", "2026-09-27.log", "2026-09-28.log"]),
        (500, ["2026-09-26.log", "2026-09-27.log", "2026-09-28.log", "2026-09-29.log"]),
    ],
    ids=["oldest-first-until-under-the-cap", "current-day-alone-over-the-cap"],
)
def test_opening_a_day_deletes_the_oldest_daily_log_files_beyond_the_size_cap(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    current_day_bytes: int,
    deleted: list[str],
) -> None:
    caplog.set_level(logging.INFO, logger="vbot.logging")
    monkeypatch.setattr(logging_module, "LOG_RETENTION_MAX_BYTES", 100)
    logs_dir = tmp_path / "logs"
    older = ["2026-09-26.log", "2026-09-27.log", "2026-09-28.log", "2026-09-29.log"]
    _write_files(logs_dir, {**dict.fromkeys(older, 40), "2026-09-30.log": current_day_bytes})

    # Whether the sweep counts the written record or not changes nothing below.
    _write_through(DailyFileHandler(logs_dir, current_date_provider=lambda: date(2026, 9, 30)), "x")

    assert sorted(path.name for path in logs_dir.iterdir()) == [
        *(name for name in older if name not in deleted),
        "2026-09-30.log",
    ]
    assert _retention_lines(caplog) == [
        (
            logging.INFO,
            {
                "count": str(len(deleted)),
                "bytes": str(40 * len(deleted)),
                "reason": "size",
                "oldest": "2026-09-26",
                "newest": deleted[-1].removesuffix(".log"),
            },
        )
    ]


def test_daily_log_retention_skips_files_it_cannot_delete_and_warns_once_per_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each start's outcome reaches that day's log file before its manager closes."""
    logs_dir = tmp_path / "logs"
    _write_files(logs_dir, {"2026-01-01.log": 10, "2026-01-02.log": 20, "2026-01-03.log": 30})
    locked = {"2026-01-01.log"}
    real_unlink = os.unlink

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        name = os.path.basename(path)
        if name in locked:
            # Windows refuses to delete a file that another process holds open.
            raise PermissionError(13, "The file is in use", path)
        if name == "2026-01-02.log":
            # Another process deletes it between the listing and this deletion.
            real_unlink(path)
            raise FileNotFoundError(2, "No such file", path)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)

    days = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
    for day in days:
        if day == days[-1]:
            locked.clear()
        manager = LogManager(
            level="INFO",
            data_dir=tmp_path,
            enable_console=False,
            current_date_provider=DateSequence(day),
        )
        try:
            manager.get_logger("tests").info("Started")
        finally:
            manager.close()

    assert sorted(path.name for path in logs_dir.iterdir()) == [
        f"{day.isoformat()}.log" for day in days
    ]
    retention_lines = [
        [
            (message.split(" ", 1)[0], dict(re.findall(r"(\w+)=([^\s)]+)", message)))
            for message in _messages(logs_dir / f"{day.isoformat()}.log")
            if " vbot.logging - " in message
        ]
        for day in days
    ]
    assert retention_lines == [
        [
            (
                "[INFO]",
                {
                    "count": "1",
                    "bytes": "30",
                    "reason": "age",
                    "oldest": "2026-01-03",
                    "newest": "2026-01-03",
                },
            ),
            ("[WARN]", {"count": "1", "first": "2026-01-01.log", "error": "PermissionError:"}),
        ],
        # The second failing sweep stays silent.
        [],
        [
            (
                "[INFO]",
                {
                    "count": "1",
                    "bytes": "10",
                    "reason": "age",
                    "oldest": "2026-01-01",
                    "newest": "2026-01-01",
                },
            ),
            # The failure streak ended.
            ("[INFO]", {}),
        ],
    ]


_ACCEPTED = '%s - "WebSocket %s" [accepted]'


@pytest.mark.parametrize(
    ("record", "routine"),
    [
        (make_websocket_record(message="connection open", path="/ws/logs?cursor=abc"), True),
        (make_websocket_record(message="connection closed", path="/ws?after_sequence=4"), True),
        (make_websocket_record(message=_ACCEPTED, args=("127.0.0.1", "/ws/logs?cursor=abc")), True),
        (
            make_websocket_record(message=_ACCEPTED, args=("127.0.0.1", "/ws/terminals/term_1")),
            True,
        ),
        (make_websocket_record(message=_ACCEPTED, args=("127.0.0.1", "/ws/live/call-9")), True),
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
        "terminal-accepted",
        "live-call-accepted",
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
        @override
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    capture_handler = CaptureHandler()
    target_logger.addHandler(capture_handler)
    target_logger.setLevel(logging.DEBUG)
    try:
        for record in (
            make_websocket_record(
                name="uvicorn.error", message="Started server process [%d]", args=(42,)
            ),
            make_websocket_record(message="connection open", path="/ws/logs"),
            make_websocket_record(
                name="uvicorn.error",
                message='%s - "WebSocket %s" 403',
                args=("127.0.0.1", "/ws/live/call-9"),
            ),
            make_websocket_record(
                name="uvicorn.error", level=logging.ERROR, message="Exception in ASGI application"
            ),
        ):
            handler.handle(record)
    finally:
        target_logger.removeHandler(capture_handler)
        handler.close()

    # Forwarded records carry the proxy's logger name and a formatted message.
    # uvicorn's own lifecycle lines drop to DEBUG, handshake rejections and
    # failures keep their level, a Live path never carries its call id, and
    # the routine lifecycle record is filtered out.
    assert [(record.name, record.levelno, record.getMessage()) for record in captured] == [
        ("vbot.server.uvicorn", logging.DEBUG, "Started server process [42]"),
        (
            "vbot.server.uvicorn",
            logging.INFO,
            '127.0.0.1 - "WebSocket /ws/live/{call_id}" 403',
        ),
        ("vbot.server.uvicorn", logging.ERROR, "Exception in ASGI application"),
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
