"""Terminal status presentation and bounded, append-only progress reporting."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import threading
import time
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Literal, TextIO, cast

from cli.formatting import output_mode

Status = Literal["busy", "success", "warning", "error", "info"]
current_progress: ContextVar[ProgressPrinter | None] = ContextVar("cli_progress", default=None)
_MARKERS: dict[Status, tuple[str, str, str]] = {
    "busy": ("…", "WORK", "36"),
    "success": ("✓", "OK", "32"),
    "warning": ("!", "WARN", "33"),
    "error": ("✗", "ERROR", "31"),
    "info": ("·", "INFO", "36"),
}


def operation_progress(message: str) -> None:
    """Report a real operation phase only inside the CLI's active command scope."""
    progress = current_progress.get()
    if progress is not None:
        progress.emit("busy", message)


def _windows_color(stream: TextIO) -> bool:
    """Enable ANSI processing on a real Windows console, otherwise leave it plain."""
    if os.name != "nt":
        return True
    import ctypes

    msvcrt = importlib.import_module("msvcrt")
    windows_ctypes = cast(Any, ctypes)

    try:
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        mode = ctypes.c_ulong()
        kernel = windows_ctypes.windll.kernel32
        return bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 0x0004)
        )
    except (OSError, ValueError):
        return False


def status_line(status: Status, message: str, *, stream: TextIO | None = None) -> str:
    """Keep textual status in pipes; decorate only a capable terminal."""

    stream = stream or sys.stdout
    symbol, label, color = _MARKERS[status]
    terminal = stream.isatty() and os.environ.get("TERM") != "dumb" and output_mode.get() != "plain"
    if terminal:
        try:
            symbol.encode(stream.encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            terminal = False
    marker = f"{symbol} {label}" if terminal else f"[{label}]"
    if terminal and not os.environ.get("NO_COLOR") and _windows_color(stream):
        marker = f"\033[{color}m{marker}\033[0m"
    return f"{marker} {message}"


class ProgressPrinter:
    """Print phase transitions immediately and elapsed time while a phase is busy.

    Live updates are opt-in and restricted to capable terminals. Captures stay append-only.
    The caller owns the scope so the heartbeat always stops before final output.
    """

    def __init__(
        self, *, interval: float = 10.0, stream: TextIO | None = None, live: bool = False
    ) -> None:
        self.messages: set[str] = set()
        self._plain = output_mode.get() == "plain"
        self._stream = stream or sys.stdout
        self._live = (
            live
            and not self._plain
            and self._stream.isatty()
            and os.environ.get("TERM") != "dumb"
            and _windows_color(self._stream)
        )
        self._line_open = False
        self._last_heartbeat = 0.0
        self._interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active: tuple[str, float] | None = None
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)

    def __enter__(self) -> ProgressPrinter:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()
        with self._lock:
            self._finish_line()

    def _finish_line(self) -> None:
        if self._line_open:
            print(file=self._stream, flush=True)
            self._line_open = False

    def _write_busy(self, message: str) -> None:
        if self._live:
            # Keep the transient line within one console row, including its marker.
            width = max(12, shutil.get_terminal_size().columns - 10)
            message = message.replace("\n", " ")
            message = message if len(message) <= width else message[: width - 3] + "..."
            print(
                "\r\033[2K" + status_line("busy", message, stream=self._stream),
                end="",
                file=self._stream,
                flush=True,
            )
            self._line_open = True
        else:
            print(status_line("busy", message, stream=self._stream), file=self._stream, flush=True)

    def emit(self, status: Status, message: str) -> None:
        if self._plain:
            return
        with self._lock:
            self._finish_line()
            self.messages.update(message.splitlines())
            if status == "busy":
                self._active = (message, time.monotonic())
                self._last_heartbeat = self._active[1]
            elif status != "info":
                self._active = None
            if status == "busy":
                self._write_busy(message)
            else:
                print(
                    status_line(status, message, stream=self._stream), file=self._stream, flush=True
                )

    def track(self, message: str) -> None:
        """Track work silently until the first heartbeat; fast reads stay quiet."""
        if self._plain:
            return
        with self._lock:
            self._active = (message, time.monotonic())
            self._last_heartbeat = self._active[1]

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                if self._active is None:
                    continue
                message, started = self._active
                now = time.monotonic()
                if now - self._last_heartbeat < self._interval:
                    continue
                self._last_heartbeat = now
                elapsed = int(now - started)
                self._write_busy(f"{message} ({elapsed}s elapsed)")
