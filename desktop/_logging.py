"""Desktop-local daily logs and native crash dumps, independent of the server."""

from __future__ import annotations

import faulthandler
import logging
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import override

from desktop.settings import config_dir

logger = logging.getLogger("vbot.desktop")
DESKTOP_LOG_DIRECTORY_NAME = "logs"
DESKTOP_LOG_FILE_SUFFIX = ".log"
_DESKTOP_LOG_HANDLER_FLAG = "_vbot_desktop_log_handler"
_DESKTOP_LOG_FORMAT = "%(asctime)s [%(vbot_level)s] %(name)s - %(message)s"
_DESKTOP_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Fatal errors (a native crash, a fatal interpreter error) bypass logging; the
# fault handler writes them to this file beside the daily logs instead.
DESKTOP_CRASH_LOG_NAME = "desktop-crash.log"
# Checked at each start: a larger crash log moves to its single previous generation.
_DESKTOP_CRASH_LOG_ROTATE_BYTES = 1024 * 1024


class _DesktopLogFormatter(logging.Formatter):
    """Render the Desktop process with the same structured labels as vBot logs."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        original_label = getattr(record, "vbot_level", None)
        record.vbot_level = "WARN" if record.levelname == "WARNING" else record.levelname
        try:
            return super().format(record)
        finally:
            if original_label is None:
                delattr(record, "vbot_level")
            else:
                record.vbot_level = original_label


class _DesktopDailyFileHandler(logging.FileHandler):
    """Write the standalone Desktop process to per-user daily log files."""

    def __init__(
        self,
        logs_directory: Path,
        *,
        current_date_provider: Callable[[], date] = date.today,
    ) -> None:
        self._logs_directory = logs_directory
        self._logs_directory.mkdir(parents=True, exist_ok=True)
        self._current_date_provider = current_date_provider
        self._active_date = current_date_provider()
        self.original_logger_level = logging.NOTSET
        self.original_logger_propagate = True
        super().__init__(self._path_for(self._active_date), encoding="utf-8")

    @override
    def emit(self, record: logging.LogRecord) -> None:
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
            self.baseFilename = os.fspath(self._path_for(current_date))
            self.stream = self._open()
        finally:
            self.release()

    def _path_for(self, target_date: date) -> Path:
        return self._logs_directory / f"{target_date.isoformat()}{DESKTOP_LOG_FILE_SUFFIX}"


def configure_desktop_logging(
    desktop_config_directory: Path | None = None,
) -> logging.Handler | None:
    """Attach structured per-user file logging for the standalone Desktop process."""

    vbot_logger = logging.getLogger("vbot")
    for handler in vbot_logger.handlers:
        if getattr(handler, _DESKTOP_LOG_HANDLER_FLAG, False):
            return handler
    try:
        handler = _DesktopDailyFileHandler(
            (desktop_config_directory or config_dir()) / DESKTOP_LOG_DIRECTORY_NAME
        )
    except OSError:
        return None
    setattr(handler, _DESKTOP_LOG_HANDLER_FLAG, True)
    handler.original_logger_level = vbot_logger.level
    handler.original_logger_propagate = vbot_logger.propagate
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        _DesktopLogFormatter(_DESKTOP_LOG_FORMAT, datefmt=_DESKTOP_LOG_DATE_FORMAT)
    )
    vbot_logger.setLevel(logging.INFO)
    vbot_logger.propagate = False
    vbot_logger.addHandler(handler)
    return handler


def close_desktop_logging(handler: logging.Handler | None) -> None:
    """Detach the file handler created for this Desktop process."""

    if handler is None:
        return
    vbot_logger = logging.getLogger("vbot")
    vbot_logger.removeHandler(handler)
    handler.close()
    original_level = getattr(handler, "original_logger_level", logging.NOTSET)
    original_propagate = getattr(handler, "original_logger_propagate", True)
    vbot_logger.setLevel(original_level)
    vbot_logger.propagate = original_propagate


def enable_desktop_crash_log(desktop_config_directory: Path | None = None) -> None:
    """Record this process's fatal errors in the crash log beside its daily logs.

    Native libraries (WebView2 through .NET, PortAudio, the wake word and speech
    models) can end the process without a Python traceback. Each start appends
    one line in the log format, so a crashed process's dump (every thread's
    Python stack, and the C stack where the platform provides one) follows the
    line of its own start. The fault handler keeps the file object referenced,
    so it stays open until the interpreter has finalized.
    """

    path = (
        (desktop_config_directory or config_dir())
        / DESKTOP_LOG_DIRECTORY_NAME
        / DESKTOP_CRASH_LOG_NAME
    )
    started = time.strftime(_DESKTOP_LOG_DATE_FORMAT)
    header = f"{started} [INFO] vbot.desktop - Desktop process started (pid={os.getpid()})\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A missing file needs nothing; a file another Desktop process holds open
        # cannot move and keeps growing until a later start.
        with suppress(OSError):
            if path.stat().st_size > _DESKTOP_CRASH_LOG_ROTATE_BYTES:
                os.replace(path, path.with_name(f"{path.name}.1"))
        crash_log = path.open("ab", buffering=0)
        try:
            crash_log.write(header.encode("utf-8"))
        except OSError:
            crash_log.close()
            raise
    except OSError as exc:
        logger.warning("Opening the crash log failed (path=%s error=%s)", path, exc)
        return
    faulthandler.enable(file=crash_log, all_threads=True, c_stack=True)
