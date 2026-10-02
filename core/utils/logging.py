"""Structured logging setup for vBot.

Provides a ``LogManager`` that centralizes the ``vbot`` logger tree,
enforces the required ``timestamp [LEVEL] name - message`` format, and
writes to both the console and a daily log file under ``<data_dir>/logs``.
WARNING and higher records of other libraries' loggers reach the same
outputs under their own logger names. Every written line shows Channel-derived
Session ids with a pseudonym in place of their platform chat or user id.
Daily log files are kept for ``LOG_RETENTION_DAYS`` and at most
``LOG_RETENTION_MAX_BYTES`` in total; older ones are deleted in the background.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, timedelta
from io import TextIOWrapper
from pathlib import Path
from typing import override

from core.utils.log_conditions import LoggedConditions

CONSOLE_LOGGING_ENV_VAR = "VBOT_LOG_STDIO"
LOGGER_NAMESPACE = "vbot"
DAILY_LOG_FILE_SUFFIX = ".log"
# Daily log files older than this many days are deleted ...
LOG_RETENTION_DAYS = 90
# ... and, oldest first, while all daily log files of a directory together
# exceed this size. The current day's file is never deleted.
LOG_RETENTION_MAX_BYTES = 500 * 1024 * 1024
# Retention only ever deletes files named exactly like the ones DailyFileHandler
# writes; everything else in the directory stays untouched.
_DAILY_LOG_FILE_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}" + re.escape(DAILY_LOG_FILE_SUFFIX)
)
# A closing handler waits at most this long for the retention sweep it started.
_RETENTION_CLOSE_WAIT_SECONDS = 5.0
UVICORN_LOGGER_NAME = "vbot.server.uvicorn"
# Every websocket route the server owns: /ws, /ws/logs, /ws/terminals/{terminal_id}
# and /ws/live/{call_id}. The Live path carries a Provider call id.
SERVER_WEBSOCKET_PATH_PATTERN = re.compile(r"/ws(?:/logs|/terminals/[^/]+|/live/[^/]+)?")
LIVE_WEBSOCKET_PATH_PATTERN = re.compile(r"(/ws/live/)[^\s\"?/]+")
ROUTINE_WEBSOCKET_LIFECYCLE_MESSAGES = frozenset({"connection open", "connection closed"})
UVICORN_WEBSOCKET_LOGGER_NAMES = frozenset({"uvicorn.error", UVICORN_LOGGER_NAME})
WEBSOCKET_ACCEPTED_MESSAGE_PATTERN = re.compile(
    r'"WebSocket (?P<path>/[^"\s]*)[^\"]*" \[accepted\]'
)
# uvicorn's line for a websocket handshake answered without accepting it (e.g. 403).
WEBSOCKET_HANDSHAKE_RESPONSE_PATTERN = re.compile(r'"WebSocket /[^"]*" \d{3}$')
# A Channel-derived Session id is ``ch-<channel id>-<platform part>`` (``core/channels``);
# only ``ch-<channel id>-main`` carries no platform chat or user id.
_CHANNEL_SESSION_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])ch-([A-Za-z0-9_-]+)")
_CHANNEL_SESSION_PSEUDONYM_SALT = b"vbot-log-channel-session:"
# 24 bits: collisions within one installation stay negligible, while every
# pseudonym matches hundreds of possible platform ids and cannot be reversed.
_CHANNEL_SESSION_PSEUDONYM_LENGTH = 6
_known_channel_ids: frozenset[str] = frozenset()
_known_channel_ids_lock = threading.Lock()


def _normalize_websocket_path(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    return path.split("?", 1)[0]


def extract_websocket_path_from_message(message: object) -> str | None:
    if not isinstance(message, str) or not message:
        return None

    message_match = WEBSOCKET_ACCEPTED_MESSAGE_PATTERN.search(message)
    if message_match is None:
        return None
    return _normalize_websocket_path(message_match.group("path"))


def is_server_websocket_path(path: str | None) -> bool:
    """Return whether *path* (without query) is one of the server's websocket routes."""

    return path is not None and SERVER_WEBSOCKET_PATH_PATTERN.fullmatch(path) is not None


def redact_live_websocket_path(message: str) -> str:
    """Replace the Provider call id of a Live websocket path in *message*."""

    return LIVE_WEBSOCKET_PATH_PATTERN.sub(r"\1{call_id}", message)


def is_routine_websocket_lifecycle_message(
    *,
    level: int | str,
    logger_name: str,
    message: str,
    websocket_path: str | None = None,
) -> bool:
    """Return whether a log message is routine lifecycle noise of a server websocket."""

    normalized_level = (
        level if isinstance(level, int) else logging.getLevelNamesMapping().get(level.upper())
    )
    if normalized_level != logging.INFO:
        return False

    normalized_path = _normalize_websocket_path(websocket_path)
    if normalized_path is None:
        normalized_path = extract_websocket_path_from_message(message)

    if message in ROUTINE_WEBSOCKET_LIFECYCLE_MESSAGES:
        return (
            is_server_websocket_path(normalized_path)
            or logger_name in UVICORN_WEBSOCKET_LOGGER_NAMES
        )

    return (
        is_server_websocket_path(normalized_path)
        and '"WebSocket ' in message
        and "[accepted]" in message
    )


def _extract_websocket_path(record: logging.LogRecord) -> str | None:
    websocket = getattr(record, "websocket", None)
    request = getattr(websocket, "request", None)
    request_path = _normalize_websocket_path(getattr(request, "path", None))
    if request_path is not None:
        return request_path

    arguments = record.args
    if isinstance(arguments, tuple):
        values = arguments
    elif isinstance(arguments, dict):
        values = tuple(arguments.values())
    else:
        values = ()

    for value in values:
        candidate_path = _normalize_websocket_path(value)
        if candidate_path is not None and candidate_path.startswith("/"):
            return candidate_path

    return extract_websocket_path_from_message(record.getMessage())


def is_logs_websocket_lifecycle_record(record: logging.LogRecord) -> bool:
    """Return whether *record* is routine lifecycle noise of a server websocket."""

    return is_routine_websocket_lifecycle_message(
        level=record.levelno,
        logger_name=record.name,
        message=record.getMessage(),
        websocket_path=_extract_websocket_path(record),
    )


class QuietLogsWebSocketLifecycleFilter(logging.Filter):
    """Suppress routine INFO lifecycle records of the server's websocket routes."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return not is_logs_websocket_lifecycle_record(record)


def resolve_daily_log_path(
    data_dir: str | Path,
    *,
    current_date_provider: Callable[[], date] | None = None,
) -> Path:
    """Resolve the active daily log file path for a runtime data directory."""

    resolved_data_dir = Path(data_dir).expanduser()
    active_date = (current_date_provider or date.today)()
    return resolved_data_dir / "logs" / _daily_log_file_name(active_date)


def _daily_log_file_name(target_date: date) -> str:
    """Return the canonical file name for one day's managed log file."""

    return f"{target_date.isoformat()}{DAILY_LOG_FILE_SUFFIX}"


def normalize_logger_name(name: str) -> str:
    """Return a logger name under the shared ``vbot`` namespace."""

    normalized_name = name.strip()
    if normalized_name == LOGGER_NAMESPACE or normalized_name.startswith(f"{LOGGER_NAMESPACE}."):
        return normalized_name
    return f"{LOGGER_NAMESPACE}.{normalized_name}"


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger that inherits the managed vBot handlers."""

    return logging.getLogger(normalize_logger_name(name))


class ManagedLoggerProxyHandler(logging.Handler):
    """Forward uvicorn log records into a managed vBot logger.

    uvicorn's own INFO lines describe server mechanics (process start, lifespan
    steps, shutdown waits) that the server's single start and stop lines
    replace, so they are forwarded at DEBUG. A websocket handshake answered
    without accepting it (e.g. 403) keeps its level, and a Live websocket path
    never carries its Provider call id into the log.
    """

    def __init__(self, target_logger_name: str) -> None:
        super().__init__()
        self._target_logger_name = target_logger_name

    @override
    def emit(self, record: logging.LogRecord) -> None:
        message = redact_live_websocket_path(record.getMessage())
        level = record.levelno
        if level == logging.INFO and WEBSOCKET_HANDSHAKE_RESPONSE_PATTERN.search(message) is None:
            level = logging.DEBUG
        target_logger = get_logger(self._target_logger_name)
        target_logger.log(
            level,
            message,
            exc_info=record.exc_info,
            stack_info=bool(record.stack_info),
        )


def build_uvicorn_log_config(
    *,
    server_logger_name: str = UVICORN_LOGGER_NAME,
) -> dict[str, object]:
    """Route uvicorn logs through the managed vBot pipeline.

    uvicorn hands its ``uvicorn.error`` logger to the websocket protocol as
    well, so that logger also carries the websocket lifecycle lines.
    """

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "quiet_logs_websocket_lifecycle": {
                "()": "core.utils.logging.QuietLogsWebSocketLifecycleFilter",
            },
        },
        "handlers": {
            "vbot_proxy": {
                "class": "core.utils.logging.ManagedLoggerProxyHandler",
                "target_logger_name": server_logger_name,
                "filters": ["quiet_logs_websocket_lifecycle"],
            },
            "null": {
                "class": "logging.NullHandler",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["vbot_proxy"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["vbot_proxy"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["null"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


_LOGGER = get_logger("logging")
# Retention sweeps run on this thread: never on the thread whose record opened a
# daily file (often the Event Loop) and never inside that record's emit, so a
# sweep that logs its outcome cannot block or re-enter the handler. A plain
# executor, because ``core.utils.workers`` measures through ``core.performance``,
# which logs through this module. Its thread is joined at interpreter exit, so a
# short-lived CLI process still finishes a sweep it started.
_RETENTION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vbot-log-retention")
# The day each logs directory was last swept for in this process: nested managers
# and every handler writing one directory sweep it once per day.
_retention_days: dict[str, date] = {}
_retention_days_lock = threading.Lock()
# Directories whose sweeps currently fail, so a failure streak warns once.
_retention_failures = LoggedConditions(limit=16)


@dataclass(slots=True)
class _RetentionOutcome:
    """What one retention sweep deleted and what it could not."""

    # (day, bytes, reason) of every deleted file, oldest first.
    deleted: list[tuple[date, int, str]] = field(default_factory=list)
    # (file name, error) of every file that could not be deleted.
    failed: list[tuple[str, OSError]] = field(default_factory=list)
    listing_error: OSError | None = None


def _sweep_daily_logs(logs_dir: Path, today: date, key: str) -> None:
    """Apply retention to one logs directory and log the outcome; on the retention thread."""

    try:
        outcome = _prune_daily_logs(logs_dir, today)
    except Exception:
        _LOGGER.exception("Daily log retention failed unexpectedly")
        return
    if outcome.deleted:
        reasons = dict.fromkeys(reason for _day, _size, reason in outcome.deleted)
        _LOGGER.info(
            "Deleted old daily log files (count=%d bytes=%d reason=%s oldest=%s newest=%s)",
            len(outcome.deleted),
            sum(size for _day, size, _reason in outcome.deleted),
            ",".join(reasons),
            outcome.deleted[0][0].isoformat(),
            outcome.deleted[-1][0].isoformat(),
        )
    if outcome.listing_error is not None:
        if _retention_failures.started(key, "list"):
            error = outcome.listing_error
            _LOGGER.warning(
                "Could not list daily log files for retention, retrying at the next sweep "
                "(error=%s: %s)",
                type(error).__name__,
                error.strerror or error,
            )
    elif outcome.failed:
        if _retention_failures.started(key, "delete"):
            file_name, error = outcome.failed[0]
            _LOGGER.warning(
                "Could not delete old daily log files, retrying at the next sweep "
                "(count=%d first=%s error=%s: %s)",
                len(outcome.failed),
                file_name,
                type(error).__name__,
                error.strerror or error,
            )
    elif _retention_failures.ended(key):
        _LOGGER.info("Daily log retention recovered")


def _prune_daily_logs(logs_dir: Path, today: date) -> _RetentionOutcome:
    """Delete daily files older than the retention, then the oldest beyond the size cap.

    Files dated ``today`` or later are never deleted. A file another process
    removed meanwhile counts as gone; one that cannot be deleted (on Windows,
    held open elsewhere) stays and is retried by the next sweep.
    """

    outcome = _RetentionOutcome()
    try:
        files = _list_daily_logs(logs_dir)
    except FileNotFoundError:
        return outcome
    except OSError as error:
        outcome.listing_error = error
        return outcome

    total = sum(size for _day, _path, size in files)
    age_cutoff = today - timedelta(days=LOG_RETENTION_DAYS)
    for day, path, size in files:
        if day >= today:
            break
        if day < age_cutoff:
            reason = "age"
        elif total > LOG_RETENTION_MAX_BYTES:
            reason = "size"
        else:
            # Oldest first: every later file is younger and the total only shrinks.
            break
        try:
            os.unlink(path)
        except FileNotFoundError:
            total -= size
            continue
        except OSError as error:
            outcome.failed.append((os.path.basename(path), error))
            continue
        total -= size
        outcome.deleted.append((day, size, reason))
    return outcome


def _list_daily_logs(logs_dir: Path) -> list[tuple[date, str, int]]:
    """Return ``(day, path, size)`` of the directory's daily log files, oldest first."""

    files: list[tuple[date, str, int]] = []
    with os.scandir(logs_dir) as listing:
        for entry in listing:
            if _DAILY_LOG_FILE_PATTERN.fullmatch(entry.name) is None:
                continue
            try:
                day = date.fromisoformat(entry.name.removesuffix(DAILY_LOG_FILE_SUFFIX))
            except ValueError:
                continue
            try:
                # Listing data: no per-file system call on Windows, one stat elsewhere.
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                continue
            except OSError:
                # Its size stays unknown; deleting it still reports a real failure.
                size = 0
            files.append((day, entry.path, size))
    files.sort()
    return files


class DailyFileHandler(logging.FileHandler):
    """File handler that writes to one log file per day.

    The active output path is ``<logs_dir>/<YYYY-MM-DD>.log``. If the date
    changes while the process is running, the handler transparently reopens
    itself against the new daily file before emitting the next record. The
    logs directory and file are created with the first record, so a handler
    that never writes leaves no trace on disk.

    Opening a day's file - with the first record, and with the first record of
    each new day - starts a retention sweep of the directory in the background,
    once per directory and day in the process: daily files older than
    ``LOG_RETENTION_DAYS``, then the oldest while all together exceed
    ``LOG_RETENTION_MAX_BYTES``, are deleted; the open day's file never is.
    :meth:`close` waits for the sweep this handler started.
    """

    def __init__(
        self,
        logs_dir: str | Path,
        *,
        current_date_provider: Callable[[], date] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        self._logs_dir = Path(logs_dir)
        self._retention_key = os.path.normcase(os.path.abspath(self._logs_dir))
        self._retention: Future[None] | None = None
        self._current_date_provider = current_date_provider or date.today
        self._active_date = self._current_date_provider()
        super().__init__(self._build_path(self._active_date), encoding=encoding, delay=True)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """Write *record*, reopening the file if the date rolled over."""

        self._rotate_if_needed()
        super().emit(record)

    @override
    def close(self) -> None:
        """Wait for this handler's retention sweep, then close the file."""

        self._await_retention()
        super().close()

    def _await_retention(self) -> None:
        """Wait, bounded, until the sweep this handler started has finished."""

        retention = self._retention
        if retention is not None:
            wait((retention,), timeout=_RETENTION_CLOSE_WAIT_SECONDS)

    @override
    def _open(self) -> TextIOWrapper:
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        stream = super()._open()
        self._start_retention(self._active_date)
        return stream

    def _start_retention(self, day: date) -> None:
        """Hand the directory's sweep for *day* to the retention thread; never waits."""

        with _retention_days_lock:
            if _retention_days.get(self._retention_key) == day:
                return
            _retention_days[self._retention_key] = day
        try:
            self._retention = _RETENTION_EXECUTOR.submit(
                _sweep_daily_logs, self._logs_dir, day, self._retention_key
            )
        except RuntimeError:
            # The interpreter is shutting down; the next process sweeps instead.
            return

    def _rotate_if_needed(self) -> None:
        current_date = self._current_date_provider()
        if current_date == self._active_date:
            return

        self.acquire()
        try:
            if current_date == self._active_date:
                return
            if self.stream is not None:
                self.stream.close()
            self._active_date = current_date
            self.baseFilename = os.fspath(self._build_path(current_date))
            self.stream = self._open()
        finally:
            self.release()

    def _build_path(self, target_date: date) -> Path:
        return self._logs_dir / _daily_log_file_name(target_date)


def register_log_channel_ids(channel_ids: Iterable[str]) -> None:
    """Keep the channel id of these Channels readable in pseudonymized Session ids."""

    global _known_channel_ids
    new_ids = frozenset(channel_ids) - _known_channel_ids
    if not new_ids:
        return
    with _known_channel_ids_lock:
        _known_channel_ids = _known_channel_ids | new_ids


def redact_channel_session_ids(text: str) -> str:
    """Replace the platform part of every Channel-derived Session id in *text*.

    ``ch-<channel id>-<platform part>`` becomes ``ch-<channel id>-#<pseudonym>``,
    or ``ch-#<pseudonym>`` while the channel id is not registered. The pseudonym
    is derived from everything after ``ch-``, so it is the same in both forms,
    in every process and on every day.
    """

    if "ch-" not in text:
        return text
    return _CHANNEL_SESSION_ID_PATTERN.sub(_pseudonymize_channel_session_id, text)


def _pseudonymize_channel_session_id(match: re.Match[str]) -> str:
    remainder = match.group(1)
    channel_id = max(
        (known for known in _known_channel_ids if remainder.startswith(f"{known}-")),
        key=len,
        default=None,
    )
    if remainder.endswith("-main") and (channel_id is None or remainder == f"{channel_id}-main"):
        return match.group(0)
    digest = hashlib.sha256(_CHANNEL_SESSION_PSEUDONYM_SALT + remainder.encode("utf-8"))
    pseudonym = digest.hexdigest()[:_CHANNEL_SESSION_PSEUDONYM_LENGTH]
    return f"ch-{channel_id}-#{pseudonym}" if channel_id else f"ch-#{pseudonym}"


class _VBotFormatter(logging.Formatter):
    """Formatter that enforces the vBot log-level labels and Session id pseudonyms.

    Channel Session ids are pseudonymized in the whole line, tracebacks included.
    """

    LEVEL_LABELS = {
        "WARNING": "WARN",
    }

    @override
    def format(self, record: logging.LogRecord) -> str:
        original_label = getattr(record, "vbot_level", None)
        record.vbot_level = self.LEVEL_LABELS.get(record.levelname, record.levelname)
        try:
            return redact_channel_session_ids(super().format(record))
        finally:
            if original_label is None:
                delattr(record, "vbot_level")
            else:
                record.vbot_level = original_label


def _is_benign_connection_reset(record: logging.LogRecord) -> bool:
    """Return whether *record* is Windows' Proactor noise about a peer that went away.

    When a client drops a connection, the Proactor Event Loop reports the
    ``ConnectionResetError`` [WinError 10054] of its ``_call_connection_lost``
    callback through the ``asyncio`` logger although nothing failed.
    """

    if record.name != "asyncio" or not record.exc_info:
        return False
    return isinstance(record.exc_info[1], ConnectionResetError) and (
        "_call_connection_lost" in record.getMessage()
    )


class _OtherLoggerRouter(logging.Handler):
    """Write WARNING and higher records of non-vBot loggers to the managed outputs.

    Installed on the root logger, it receives records of third-party loggers
    (asyncio, httpx, Channel libraries, ...) that would otherwise fall through
    to ``logging.lastResort`` on stderr. The records keep their own logger
    name. ``vbot`` records never reach the root while a manager is active.
    """

    def __init__(self, targets: list[logging.Handler]) -> None:
        super().__init__(level=logging.WARNING)
        self._targets = targets

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.name == LOGGER_NAMESPACE or record.name.startswith(f"{LOGGER_NAMESPACE}."):
            return
        if _is_benign_connection_reset(record):
            record = copy.copy(record)
            record.levelno, record.levelname = logging.DEBUG, logging.getLevelName(logging.DEBUG)
        for handler in self._targets:
            if record.levelno >= handler.level:
                handler.handle(record)


@dataclass(frozen=True)
class _PipelineState:
    """The logging pipeline a manager replaced when it became active."""

    owner: LogManager | None
    level: int
    propagate: bool


class LogManager:
    """Owns the managed log outputs of one process while it is open.

    Construction activates the pipeline at once: the manager's console and
    daily-file handlers attach to the ``vbot`` logger (which stops propagating
    to the root), and a router on the root logger writes WARNING and higher
    records of other libraries' loggers to the same outputs. Loggers under
    ``vbot`` - whether from :meth:`get_logger` or ``logging.getLogger`` -
    inherit the handlers.

    Managers nest: a newer manager replaces the active one's outputs, and
    closing it restores the manager it replaced, as long as that one is still
    open. :meth:`close` detaches and closes this manager's handlers.

    Usage::

        manager = LogManager(level="DEBUG", data_dir=data_dir)
        log = manager.get_logger("core")
        log.info("Runtime started")   # -> vbot.core
        manager.close()
    """

    _FORMAT = "%(asctime)s [%(vbot_level)s] %(name)s - %(message)s"
    _DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
    _MANAGED_HANDLER_FLAG = "_vbot_managed_handler"

    def __init__(
        self,
        level: int | str = logging.INFO,
        *,
        data_dir: str | Path | None = None,
        current_date_provider: Callable[[], date] | None = None,
        enable_console: bool | None = None,
    ) -> None:
        """Build the manager's outputs and make them the active pipeline.

        Args:
            level: Default log level for all loggers created by this
                   manager.  May be an ``int`` (e.g. ``logging.DEBUG``)
                   or a level name string (e.g. ``"INFO"``).
            data_dir: Optional runtime data directory. When provided, the
                manager writes log files under ``<data_dir>/logs``; the
                directory is created with the first record.
            current_date_provider: Optional current-date hook used to
                resolve the active daily log filename.
            enable_console: Whether to write to stderr as well; defaults to
                the ``VBOT_LOG_STDIO`` environment variable (on unless ``0``).
        """
        self._level: int = self._resolve_level(level)
        self._data_dir = Path(data_dir).expanduser() if data_dir is not None else None
        self._current_date_provider = current_date_provider or date.today
        self._enable_console = (
            self._console_logging_enabled_from_env() if enable_console is None else enable_console
        )
        self._formatter = _VBotFormatter(self._FORMAT, datefmt=self._DATE_FORMAT)
        self._loggers: dict[str, logging.Logger] = {}
        self._handlers = self._build_handlers()
        self._router = _OtherLoggerRouter(self._handlers)
        for handler in (*self._handlers, self._router):
            setattr(handler, self._MANAGED_HANDLER_FLAG, self)
        self._closed = False
        self._replaced = self._activate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def log_file_path(self) -> Path | None:
        """Return the active daily log file path, if file logging is enabled."""

        if self._data_dir is None:
            return None
        return resolve_daily_log_path(
            self._data_dir,
            current_date_provider=self._current_date_provider,
        )

    def get_logger(self, name: str) -> logging.Logger:
        """Return (or create) a logger for *name*.

        Repeated calls with the same *name* return the same logger instance.
        Loggers live under the shared ``vbot`` namespace so unmanaged
        ``logging.getLogger("vbot.*")`` calls inherit the same handlers.

        Args:
            name: Module name **without** the ``vbot.`` prefix
                  (e.g. ``"core"``, ``"server"``).

        Returns:
            A configured :class:`logging.Logger`.
        """
        logger_name = self._normalize_logger_name(name)
        if logger_name not in self._loggers:
            logger = logging.getLogger(logger_name)
            logger.setLevel(self._level)
            logger.propagate = True
            self._loggers[logger_name] = logger
        return self._loggers[logger_name]

    def close(self) -> None:
        """Detach and close this manager's handlers; safe to call repeatedly.

        When this manager is the active pipeline, the manager it replaced
        becomes active again if it is still open; otherwise ``vbot`` records
        propagate normally again. A retention sweep its daily file started is
        awaited first, so the sweep's outcome still reaches these outputs.
        """

        if self._closed:
            return
        self._closed = True
        for handler in self._handlers:
            if isinstance(handler, DailyFileHandler):
                handler._await_retention()
        namespace = logging.getLogger(LOGGER_NAMESPACE)
        root = logging.getLogger()
        active = self._router in root.handlers or any(
            handler in namespace.handlers for handler in self._handlers
        )
        root.removeHandler(self._router)
        for handler in self._handlers:
            namespace.removeHandler(handler)
            handler.close()
        self._router.close()
        if active:
            self._restore_replaced(namespace, root)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _activate(self) -> _PipelineState:
        namespace = logging.getLogger(LOGGER_NAMESPACE)
        root = logging.getLogger()
        replaced_owner: LogManager | None = None
        for logger in (namespace, root):
            for handler in list(logger.handlers):
                owner = getattr(handler, self._MANAGED_HANDLER_FLAG, None)
                if isinstance(owner, LogManager):
                    replaced_owner = owner
                    logger.removeHandler(handler)
        replaced = _PipelineState(
            owner=replaced_owner, level=namespace.level, propagate=namespace.propagate
        )
        namespace.setLevel(self._level)
        namespace.propagate = False
        for handler in self._handlers:
            namespace.addHandler(handler)
        root.addHandler(self._router)
        return replaced

    def _restore_replaced(self, namespace: logging.Logger, root: logging.Logger) -> None:
        state = self._replaced
        # A replaced manager that was closed meanwhile hands back what it replaced.
        while state.owner is not None and state.owner._closed:
            state = state.owner._replaced
        namespace.setLevel(state.level)
        namespace.propagate = state.propagate
        if state.owner is not None:
            for handler in state.owner._handlers:
                namespace.addHandler(handler)
            root.addHandler(state.owner._router)

    def _build_handlers(self) -> list[logging.Handler]:
        handlers: list[logging.Handler] = []

        if self._enable_console:
            stream_handler = logging.StreamHandler()
            stream_handler.setLevel(self._level)
            stream_handler.setFormatter(self._formatter)
            handlers.append(stream_handler)

        if self._data_dir is not None:
            file_handler = DailyFileHandler(
                self._data_dir / "logs",
                current_date_provider=self._current_date_provider,
            )
            file_handler.setLevel(self._level)
            file_handler.setFormatter(self._formatter)
            handlers.append(file_handler)

        return handlers

    def _normalize_logger_name(self, name: str) -> str:
        return normalize_logger_name(name)

    @staticmethod
    def _resolve_level(level: int | str) -> int:
        """Normalise a log level name (string) or int to an int.

        ``logging.getLevelName()`` is annotated ``-> int`` for string
        input but can return a string for unrecognised level names
        (e.g. ``"Level GARBAGE"``).  We guard against that by falling
        back to ``logging.INFO`` when the result is not an ``int``.
        """
        if isinstance(level, int):
            return level
        return logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    @staticmethod
    def _console_logging_enabled_from_env() -> bool:
        raw_value = os.environ.get(CONSOLE_LOGGING_ENV_VAR, "1").strip().lower()
        return raw_value not in {"0", "false", "no", "off"}
