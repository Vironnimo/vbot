"""Private PTY/ConPTY transport, VT rendering, and process-tree control."""

from __future__ import annotations

import codecs
import contextlib
import copy
import os
import shutil
import signal
import subprocess
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pyte

from core.tools.process_manager import subprocess_creation_flags

_HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
_ALTERNATE_SCREEN_MODES = frozenset({47, 1047, 1049})
_SCREEN_STATE_FIELDS = (
    "savepoints",
    "columns",
    "lines",
    "buffer",
    "dirty",
    "margins",
    "mode",
    "title",
    "icon_name",
    "charset",
    "g0_charset",
    "g1_charset",
    "tabstops",
    "cursor",
    "saved_columns",
)
_ANSI_COLOR_CODES = {
    "black": 30,
    "red": 31,
    "green": 32,
    "brown": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "brightblack": 90,
    "brightred": 91,
    "brightgreen": 92,
    "brightbrown": 93,
    "brightblue": 94,
    "brightmagenta": 95,
    "brightcyan": 96,
    "brightwhite": 97,
}


class TerminalAdapter(Protocol):
    """Blocking terminal-process adapter used through worker threads."""

    @property
    def pid(self) -> int: ...

    def read(self, size: int) -> str: ...

    def write(self, text: str) -> None: ...

    def resize(self, rows: int, columns: int) -> None: ...

    def is_alive(self) -> bool: ...

    def exit_code(self) -> int | None: ...

    def terminate(self) -> None: ...


TerminalAdapterFactory = Callable[
    [Sequence[str], Path, Mapping[str, str], int, int], TerminalAdapter
]


def default_terminal_argv(env: Mapping[str, str] | None = None) -> list[str]:
    """Return the host user's default interactive shell command."""
    environment = env or os.environ
    if os.name == "nt":
        return [environment.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"]
    return [environment.get("SHELL") or os.environ.get("SHELL") or "/bin/sh"]


class _TerminalScreen(pyte.Screen):
    def __init__(self, columns: int, lines: int, on_scroll: Callable[[str], None]) -> None:
        self._on_scroll = on_scroll
        self._primary_state: dict[str, Any] | None = None
        self._alternate_modes: set[int] = set()
        super().__init__(columns, lines)

    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom and self._primary_state is None:
            self._on_scroll(_render_buffer_line(self.buffer[top], self.columns))
        super().index()

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        alternate = _ALTERNATE_SCREEN_MODES.intersection(modes) if kwargs.get("private") else set()
        if alternate and self._primary_state is None:
            self._primary_state = self._capture_state()
            super().reset()
        self._alternate_modes.update(alternate)
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        alternate = _ALTERNATE_SCREEN_MODES.intersection(modes) if kwargs.get("private") else set()
        self._alternate_modes.difference_update(alternate)
        if alternate and not self._alternate_modes and self._primary_state is not None:
            primary_state = self._primary_state
            self._primary_state = None
            self._restore_state(primary_state)
            remaining = tuple(mode for mode in modes if mode not in alternate)
            if remaining:
                super().reset_mode(*remaining, **kwargs)
            return
        super().reset_mode(*modes, **kwargs)

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        if self._primary_state is None:
            super().resize(lines=lines, columns=columns)
            return
        alternate_state = self._capture_state()
        primary_state = self._primary_state
        self._restore_state(primary_state)
        super().resize(lines=lines, columns=columns)
        self._primary_state = self._capture_state()
        self._restore_state(alternate_state)
        super().resize(lines=lines, columns=columns)

    def _capture_state(self) -> dict[str, Any]:
        return {
            field_name: copy.deepcopy(getattr(self, field_name))
            for field_name in _SCREEN_STATE_FIELDS
        }

    def _restore_state(self, state: Mapping[str, Any]) -> None:
        for field_name in _SCREEN_STATE_FIELDS:
            setattr(self, field_name, copy.deepcopy(state[field_name]))


@dataclass(frozen=True, slots=True)
class _ScrollbackLine:
    sequence: int
    text: str


class TerminalRenderer:
    """Bounded rendered screen and monotonically addressed scrollback."""

    def __init__(self, columns: int, rows: int, *, scrollback_lines: int) -> None:
        self.columns = columns
        self.rows = rows
        self.revision = 0
        self._next_sequence = 1
        self._scrollback: deque[_ScrollbackLine] = deque(maxlen=scrollback_lines)
        self._screen = _TerminalScreen(columns, rows, self._capture_scrolled_line)
        self._stream = pyte.Stream(self._screen)

    def feed(self, text: str) -> None:
        if not text:
            return
        self._stream.feed(text)
        self.revision += 1

    def resize(self, columns: int, rows: int) -> None:
        self._screen.resize(lines=rows, columns=columns)
        self.columns = columns
        self.rows = rows
        self.revision += 1

    def screen_text(self) -> str:
        lines = [line.rstrip() for line in self._screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def ansi_snapshot(self) -> str:
        """Serialize the current screen into bounded ANSI for a late viewer."""
        parts = ["\x1b[?25l", "\x1b[0m", "\x1b[2J", "\x1b[H"]
        active_style: tuple[Any, ...] | None = None
        for row_index in range(self.rows):
            parts.append(f"\x1b[{row_index + 1};1H")
            line = self._screen.buffer[row_index]
            for column_index in range(self.columns):
                cell = line[column_index]
                style = _cell_style(cell)
                if style != active_style:
                    parts.append(_style_sequence(cell))
                    active_style = style
                parts.append(cell.data or " ")

        cursor = self._screen.cursor
        parts.extend(
            (
                "\x1b[0m",
                f"\x1b[{cursor.y + 1};{cursor.x + 1}H",
                "\x1b[?25l" if cursor.hidden else "\x1b[?25h",
            )
        )
        return "".join(parts)

    def page(self, *, before: int | None, limit: int) -> dict[str, Any]:
        available = list(self._scrollback)
        if before is not None:
            oldest = available[0].sequence if available else self._next_sequence
            if before < oldest:
                raise ValueError("Terminal scrollback cursor has expired")
            available = [line for line in available if line.sequence < before]
        selected = available[-limit:]
        has_more = bool(selected) and any(
            line.sequence < selected[0].sequence for line in self._scrollback
        )
        return {
            "text": "\n".join(line.text for line in selected),
            "line_count": len(selected),
            "has_more": has_more,
            "next_before": selected[0].sequence if has_more else None,
        }

    def _capture_scrolled_line(self, text: str) -> None:
        self._scrollback.append(_ScrollbackLine(self._next_sequence, text))
        self._next_sequence += 1


class _WindowsTerminalAdapter:
    def __init__(self, process: Any) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    def read(self, size: int) -> str:
        return str(self._process.read(size))

    def write(self, text: str) -> None:
        self._process.write(text)

    def resize(self, rows: int, columns: int) -> None:
        self._process.setwinsize(rows, columns)

    def is_alive(self) -> bool:
        return bool(self._process.isalive())

    def exit_code(self) -> int | None:
        value = self._process.exitstatus
        return int(value) if isinstance(value, int) else None

    def terminate(self) -> None:
        self._process.terminate(force=True)


class _PosixTerminalAdapter:
    def __init__(self, process: Any) -> None:
        self._process = process
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    def read(self, size: int) -> str:
        return self._decoder.decode(self._process.read(size), final=False)

    def write(self, text: str) -> None:
        self._process.write(text.encode("utf-8"))

    def resize(self, rows: int, columns: int) -> None:
        self._process.setwinsize(rows, columns)

    def is_alive(self) -> bool:
        return bool(self._process.isalive())

    def exit_code(self) -> int | None:
        value = self._process.exitstatus
        if isinstance(value, int):
            return value
        signal_status = self._process.signalstatus
        return 128 + int(signal_status) if isinstance(signal_status, int) else None

    def terminate(self) -> None:
        self._process.terminate(force=True)


def spawn_terminal_adapter(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], rows: int, columns: int
) -> TerminalAdapter:
    if os.name == "nt":
        from winpty import PtyProcess

        process = PtyProcess.spawn(
            _windows_spawn_argv(argv, env),
            cwd=str(cwd),
            env=dict(env),
            dimensions=(rows, columns),
        )
        return _WindowsTerminalAdapter(process)

    from ptyprocess import PtyProcess

    process = PtyProcess.spawn(list(argv), cwd=str(cwd), env=dict(env), dimensions=(rows, columns))
    return _PosixTerminalAdapter(process)


def terminate_process_tree(adapter: TerminalAdapter) -> None:
    if not adapter.is_alive():
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(adapter.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=subprocess_creation_flags(),
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        kill_process_group = getattr(os, "killpg", None)
        try:
            if kill_process_group is not None:
                kill_process_group(adapter.pid, _HARD_KILL_SIGNAL)
                return
        except (ProcessLookupError, OSError):
            pass
    with contextlib.suppress(ProcessLookupError, OSError):
        adapter.terminate()


def _windows_spawn_argv(argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
    executable = shutil.which(argv[0], path=env.get("PATH"))
    resolved = executable or argv[0]
    prepared = [resolved, *argv[1:]]
    if Path(resolved).suffix.lower() not in {".bat", ".cmd"}:
        return prepared
    command_processor = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return [command_processor, "/d", "/s", "/c", subprocess.list2cmdline(prepared)]


def _render_buffer_line(line: Any, columns: int) -> str:
    return "".join(line[column].data for column in range(columns)).rstrip()


def _cell_style(cell: Any) -> tuple[Any, ...]:
    return (
        cell.fg,
        cell.bg,
        cell.bold,
        cell.italics,
        cell.underscore,
        cell.strikethrough,
        cell.reverse,
        cell.blink,
    )


def _style_sequence(cell: Any) -> str:
    codes = [0]
    codes.extend(_ansi_color(cell.fg, background=False))
    codes.extend(_ansi_color(cell.bg, background=True))
    if cell.bold:
        codes.append(1)
    if cell.italics:
        codes.append(3)
    if cell.underscore:
        codes.append(4)
    if cell.blink:
        codes.append(5)
    if cell.reverse:
        codes.append(7)
    if cell.strikethrough:
        codes.append(9)
    return f"\x1b[{';'.join(str(code) for code in codes)}m"


def _ansi_color(value: Any, *, background: bool) -> list[int]:
    if not isinstance(value, str) or value == "default":
        return [49 if background else 39]
    named = _ANSI_COLOR_CODES.get(value)
    if named is not None:
        return [named + 10 if background else named]
    if len(value) == 6:
        try:
            red = int(value[0:2], 16)
            green = int(value[2:4], 16)
            blue = int(value[4:6], 16)
        except ValueError:
            pass
        else:
            return [48 if background else 38, 2, red, green, blue]
    return [49 if background else 39]


__all__ = [
    "TerminalAdapter",
    "TerminalAdapterFactory",
    "TerminalRenderer",
    "default_terminal_argv",
    "spawn_terminal_adapter",
    "terminate_process_tree",
]
