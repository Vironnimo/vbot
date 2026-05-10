"""Structured logging setup for vBot.

Provides a ``LogManager`` that centralizes the ``vbot`` logger tree,
enforces the required ``timestamp [LEVEL] name - message`` format, and
writes to both the console and a daily log file under ``<data_dir>/logs``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

CONSOLE_LOGGING_ENV_VAR = "VBOT_LOG_STDIO"
LOGGER_NAMESPACE = "vbot"


def resolve_daily_log_path(
    data_dir: str | Path,
    *,
    current_date_provider: Callable[[], date] | None = None,
) -> Path:
    """Resolve the active daily log file path for a runtime data directory."""

    resolved_data_dir = Path(data_dir).expanduser()
    active_date = (current_date_provider or date.today)()
    return resolved_data_dir / "logs" / active_date.isoformat()


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
    """Forward third-party log records into a managed vBot logger."""

    def __init__(self, target_logger_name: str) -> None:
        super().__init__()
        self._target_logger_name = target_logger_name

    def emit(self, record: logging.LogRecord) -> None:
        target_logger = get_logger(self._target_logger_name)
        target_logger.log(
            record.levelno,
            record.getMessage(),
            exc_info=record.exc_info,
            stack_info=bool(record.stack_info),
        )


def build_uvicorn_log_config(
    *,
    server_logger_name: str = "vbot.server.uvicorn",
) -> dict[str, object]:
    """Route uvicorn lifecycle logs through the managed vBot pipeline."""

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "vbot_proxy": {
                "class": "core.utils.logging.ManagedLoggerProxyHandler",
                "target_logger_name": server_logger_name,
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


class DailyFileHandler(logging.FileHandler):
    """File handler that writes to one log file per day.

    The active output path is ``<logs_dir>/<YYYY-MM-DD>``. If the date
    changes while the process is running, the handler transparently reopens
    itself against the new daily file before emitting the next record.
    """

    def __init__(
        self,
        logs_dir: str | Path,
        *,
        current_date_provider: Callable[[], date] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        self._logs_dir = Path(logs_dir)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._current_date_provider = current_date_provider or date.today
        self._active_date = self._current_date_provider()
        super().__init__(self._build_path(self._active_date), encoding=encoding)

    @property
    def current_path(self) -> Path:
        """Return the currently open daily log file path."""

        return Path(self.baseFilename)

    def emit(self, record: logging.LogRecord) -> None:
        """Write *record*, reopening the file if the date rolled over."""

        self._rotate_if_needed()
        super().emit(record)

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
        return self._logs_dir / target_date.isoformat()


class _VBotFormatter(logging.Formatter):
    """Formatter that enforces the required vBot log-level labels."""

    LEVEL_LABELS = {
        "WARNING": "WARN",
    }

    def format(self, record: logging.LogRecord) -> str:
        original_label = getattr(record, "vbot_level", None)
        record.vbot_level = self.LEVEL_LABELS.get(record.levelname, record.levelname)
        try:
            return super().format(record)
        finally:
            if original_label is None:
                delattr(record, "vbot_level")
            else:
                record.vbot_level = original_label


class LogManager:
    """Creates and manages per-module structured loggers.

    Each logger returned by :meth:`get_logger` writes through the shared
    ``vbot`` logger tree with a uniform format and shared console/file
    handlers.

    Usage::

        manager = LogManager(level="DEBUG")
        log = manager.get_logger("core")
        log.info("Runtime started")   # -> vbot.core
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
        """Initialise the log manager.

        Args:
            level: Default log level for all loggers created by this
                   manager.  May be an ``int`` (e.g. ``logging.DEBUG``)
                   or a level name string (e.g. ``"INFO"``).
            data_dir: Optional runtime data directory. When provided, the
                manager writes log files under ``<data_dir>/logs``.
            current_date_provider: Optional current-date hook used to
                resolve the active daily log filename.
        """
        self._level: int = self._resolve_level(level)
        self._data_dir = Path(data_dir).expanduser() if data_dir is not None else None
        self._current_date_provider = current_date_provider or date.today
        self._enable_console = (
            self._console_logging_enabled_from_env() if enable_console is None else enable_console
        )
        self._formatter = _VBotFormatter(self._FORMAT, datefmt=self._DATE_FORMAT)
        self._loggers: dict[str, logging.Logger] = {}
        self._handlers: list[logging.Handler] = []
        self._configured = False

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
        self._ensure_configured()
        logger_name = self._normalize_logger_name(name)
        if logger_name not in self._loggers:
            logger = logging.getLogger(logger_name)
            logger.setLevel(self._level)
            logger.propagate = True
            self._loggers[logger_name] = logger
        return self._loggers[logger_name]

    def close(self) -> None:
        """Remove and close handlers managed by this instance."""

        logger = logging.getLogger(LOGGER_NAMESPACE)
        for handler in list(logger.handlers):
            if getattr(handler, self._MANAGED_HANDLER_FLAG, False):
                logger.removeHandler(handler)
                if handler not in self._handlers:
                    handler.close()
        for handler in self._handlers:
            handler.close()
        self._handlers = []
        self._configured = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_configured(self) -> None:
        if self._configured:
            return

        logger = logging.getLogger(LOGGER_NAMESPACE)
        self.close()
        logger.setLevel(self._level)
        logger.propagate = False

        for handler in self._build_handlers():
            setattr(handler, self._MANAGED_HANDLER_FLAG, True)
            logger.addHandler(handler)
            self._handlers.append(handler)

        self._configured = True

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
