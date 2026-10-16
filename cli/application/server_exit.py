"""How the tray's server process ended when it bypassed its own shutdown.

A server process killed from outside (a Job that ends its process tree, a task
manager) or a native crash writes no stop line. While the tray follows its
server, :class:`ServerExitWatch` therefore holds a handle to the process the
target's control record names, so the process's exit code and exit time outlive
it. When the activity rules find that the server vanished without its shutdown,
:meth:`ServerExitWatch.report` writes one WARNING into the server's daily log,
next to its start line, and returns what it knows for the activity entry.

Only Windows keeps the exit code of a process that is not a child; elsewhere
the watch still names the process, and its exit code is unknown.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any, Protocol

import psutil  # type: ignore[import-untyped]

from cli.server_management import (
    PROCESS_CREATE_TIME_TOLERANCE_SECONDS,
    ServerInstance,
    log_bypassed_stop,
)
from core.utils.server_control import ServerControlRecord, process_started, read_server_control
from core.utils.timestamps import parse_timestamp

_LOGGER = logging.getLogger("vbot.application.server_exit")
#: The logger of the WARNING in the server's daily log.
LOGGER_NAME = "application.server_exit"
#: Larger exit codes are Windows status codes, such as 0xC0000005 for an access violation.
_HEX_EXIT_CODES = 0x10000


@dataclass(frozen=True)
class ServerEnd:
    """What the tray knows about a server process that ended without its shutdown.

    Each field is ``None`` when unknown. ``ended_at`` is the exit time the
    operating system kept; ``uptime`` runs until then, or until the tray noticed.
    """

    pid: int | None
    started_at: datetime | None
    ended_at: datetime | None
    uptime: timedelta | None
    exit_code: int | None

    def log_fields(self) -> str:
        """The ``key=value`` fields of the daily log line; unknown values say so."""

        fields = {
            "pid": None if self.pid is None else str(self.pid),
            "started": None if self.started_at is None else _local_iso(self.started_at),
            "ended": None if self.ended_at is None else _local_iso(self.ended_at),
            "uptime": None if self.uptime is None else _compact_duration(self.uptime),
            "exit_code": None if self.exit_code is None else exit_code_text(self.exit_code),
        }
        return " ".join(f"{key}={value or 'unknown'}" for key, value in fields.items())


class ExitHandle(Protocol):
    """An open handle to one process that keeps its exit facts after it ended."""

    def exit(self) -> tuple[int, datetime | None] | None:
        """The exit code and exit time once the process ended; ``None`` before or if unknown."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _Process:
    """The followed server process: its identity and, on Windows, a handle to it."""

    pid: int
    create_time: float
    handle: ExitHandle | None

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()


class ServerExitWatch:
    """Follow the tray's server process so an end without shutdown can be told.

    ``track`` runs on every connect to the server, ``report`` once the activity
    rules found that the server ended without its shutdown; both on the tray's
    monitor thread. ``close`` releases the handle when the tray stops following.
    """

    def __init__(
        self,
        target: Callable[[], ServerInstance],
        *,
        now: Callable[[], datetime] | None = None,
        open_process: Callable[[int], ExitHandle | None] | None = None,
    ) -> None:
        self._target = target
        self._now = now or (lambda: datetime.now(UTC))
        self._open = open_process or open_exit_handle
        self._lock = threading.Lock()
        self._started_at: datetime | None = None
        self._process: _Process | None = None

    def track(self, started_at: str) -> None:
        """Follow the connected server and the process its control record names.

        ``started_at`` is the server's start time from its hello (canonical
        UTC, empty when it reports none). A record whose process does not match
        the recorded creation time names a reused pid and is not followed.
        """

        target = self._target()
        record = read_server_control(target.data_dir, target.port)
        with self._lock:
            self._started_at = _parse(started_at)
            current = self._process
            if (
                record is not None
                and current is not None
                and (current.pid, current.create_time) == (record.pid, record.process_create_time)
            ):
                return
            self._process = None
        if current is not None:
            current.close()
        if record is None:
            return
        # The open handle keeps the pid from being reused while the identity is checked.
        process = _Process(record.pid, record.process_create_time, self._open(record.pid))
        if not _is_recorded_process(record):
            process.close()
            return
        with self._lock:
            self._process = process

    def report(self) -> ServerEnd:
        """Log the server's end without shutdown in its daily log; return what is known."""

        with self._lock:
            started, process = self._started_at, self._process
            self._started_at, self._process = None, None
        exited: tuple[int, datetime | None] | None = None
        if process is not None and process.handle is not None:
            try:
                exited = process.handle.exit()
            except OSError as error:
                _LOGGER.warning("Could not read how the server process ended: %s", error)
            finally:
                process.close()
        ended = exited[1] if exited is not None else None
        end = ServerEnd(
            pid=process.pid if process is not None else None,
            started_at=started,
            ended_at=ended,
            uptime=None if started is None else max(timedelta(0), (ended or self._now()) - started),
            exit_code=exited[0] if exited is not None else None,
        )
        log_bypassed_stop(
            self._target(),
            logging.WARNING,
            "Server process ended without shutting down (%s)",
            end.log_fields(),
            logger_name=LOGGER_NAME,
        )
        return end

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            process.close()


def exit_code_text(code: int) -> str:
    """Render an exit code: decimal, or hexadecimal for a Windows status code."""

    return f"0x{code:08X}" if code >= _HEX_EXIT_CODES else str(code)


def _is_recorded_process(record: ServerControlRecord) -> bool:
    try:
        created = process_started(psutil.Process(record.pid))
    except psutil.Error, OSError:
        return False
    return abs(created - record.process_create_time) <= PROCESS_CREATE_TIME_TOLERANCE_SECONDS


def _parse(value: str) -> datetime | None:
    try:
        return parse_timestamp(value) if value else None
    except ValueError:
        return None


def _local_iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _compact_duration(value: timedelta) -> str:
    """Render a duration like the server's stop line does (``2h21m37s``)."""

    total = max(0, int(value.total_seconds()))
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h{minutes:02d}m"
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


if sys.platform == "win32":
    from ctypes import wintypes

    _SYNCHRONIZE = 0x00100000
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _WAIT_OBJECT_0 = 0
    # FILETIME counts 100 ns intervals since 1601-01-01 UTC.
    _FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)

    @cache
    def _kernel32() -> Any:
        """A private kernel32 with full prototypes, leaving ``ctypes.windll`` alone."""

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE
        filetime = ctypes.POINTER(wintypes.FILETIME)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = handle
        kernel.WaitForSingleObject.argtypes = (handle, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.GetExitCodeProcess.argtypes = (handle, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = (handle, filetime, filetime, filetime, filetime)
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (handle,)
        kernel.CloseHandle.restype = wintypes.BOOL
        return kernel

    class _WindowsExitHandle:
        """A query-and-wait handle: the process object and its exit code live while it is open."""

        def __init__(self, handle: int) -> None:
            self._handle: int | None = handle
            self._lock = threading.Lock()

        def exit(self) -> tuple[int, datetime | None] | None:
            kernel = _kernel32()
            with self._lock:
                if self._handle is None:
                    return None
                # A process may exit with STILL_ACTIVE (259) itself; only the wait tells.
                if kernel.WaitForSingleObject(self._handle, 0) != _WAIT_OBJECT_0:
                    return None
                code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                    raise ctypes.WinError(ctypes.get_last_error())
                return int(code.value), self._exit_time(kernel)

        def _exit_time(self, kernel: Any) -> datetime | None:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(self._handle, *(ctypes.byref(time) for time in times)):
                return None
            exited = times[1]
            value = (int(exited.dwHighDateTime) << 32) | int(exited.dwLowDateTime)
            return _FILETIME_EPOCH + timedelta(microseconds=value // 10) if value else None

        def close(self) -> None:
            with self._lock:
                handle, self._handle = self._handle, None
            if handle is not None:
                _kernel32().CloseHandle(handle)

    def open_exit_handle(pid: int) -> ExitHandle | None:
        """Open *pid* so its exit code outlives it; ``None`` when it cannot be opened."""

        handle = _kernel32().OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        return _WindowsExitHandle(handle) if handle else None

else:

    def open_exit_handle(pid: int) -> ExitHandle | None:
        """Only Windows keeps a non-child process's exit code for a handle."""

        return None
