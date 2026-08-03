"""Private PTY/ConPTY transport, VT rendering, and Codex launch integration."""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pyte

from core.tools.process_manager import subprocess_creation_flags
from core.tools.terminal_hook_sink import TERMINAL_EVENT_FILE_ENV, TERMINAL_EVENT_NONCE_ENV

_HOOK_COMMAND = "python -m core.tools.terminal_hook_sink"
_CODEX_FEATURE = "default_mode_request_user_input"
_HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)


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


class _TerminalScreen(pyte.Screen):
    def __init__(self, columns: int, lines: int, on_scroll: Callable[[str], None]) -> None:
        self._on_scroll = on_scroll
        super().__init__(columns, lines)

    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom:
            self._on_scroll(_render_buffer_line(self.buffer[top], self.columns))
        super().index()


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


def prepare_codex_launch(
    argv: Sequence[str], env: dict[str, str], event_path: Path, nonce: str
) -> list[str]:
    python_dir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = os.pathsep.join(part for part in (python_dir, env.get("PATH", "")) if part)
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (package_root, env.get("PYTHONPATH", "")) if part
    )
    env[TERMINAL_EVENT_FILE_ENV] = str(event_path)
    env[TERMINAL_EVENT_NONCE_ENV] = nonce
    return _with_codex_hooks(argv)


def is_codex_executable(command: str) -> bool:
    name = Path(command).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name == "codex"


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
    codex_node_argv = _windows_codex_node_argv(prepared, env)
    if codex_node_argv is not None:
        return codex_node_argv
    if Path(resolved).suffix.lower() not in {".bat", ".cmd"}:
        return prepared
    command_processor = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return [command_processor, "/d", "/s", "/c", subprocess.list2cmdline(prepared)]


def _windows_codex_node_argv(argv: Sequence[str], env: Mapping[str, str]) -> list[str] | None:
    launcher = Path(argv[0])
    if launcher.stem.lower() != "codex" or launcher.suffix.lower() != ".cmd":
        return None
    script = launcher.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if not script.is_file():
        return None
    bundled_node = launcher.parent / "node.exe"
    node = (
        str(bundled_node) if bundled_node.is_file() else shutil.which("node", path=env.get("PATH"))
    )
    return [node, str(script), *argv[1:]] if node is not None else None


def _with_codex_hooks(argv: Sequence[str]) -> list[str]:
    command = json.dumps(_HOOK_COMMAND)
    handler = (
        '[{hooks=[{type="command",command='
        + command
        + ",command_windows="
        + command
        + ",timeout=5}]}]"
    )
    question_handler = (
        '[{matcher="^request_user_input$",hooks=[{type="command",command='
        + command
        + ",command_windows="
        + command
        + ",timeout=5}]}]"
    )
    injected = [
        "--dangerously-bypass-hook-trust",
        "--enable",
        _CODEX_FEATURE,
        "-c",
        "check_for_update_on_startup=false",
        "-c",
        "suppress_unstable_features_warning=true",
        "-c",
        "hooks.Stop=" + handler,
        "-c",
        "hooks.PermissionRequest=" + handler,
        "-c",
        "hooks.PreToolUse=" + question_handler,
    ]
    if "--no-alt-screen" not in argv[1:]:
        injected.insert(0, "--no-alt-screen")
    return [argv[0], *injected, *argv[1:]]


def _render_buffer_line(line: Any, columns: int) -> str:
    return "".join(line[column].data for column in range(columns)).rstrip()


__all__ = [
    "TerminalAdapter",
    "TerminalAdapterFactory",
    "TerminalRenderer",
    "is_codex_executable",
    "prepare_codex_launch",
    "spawn_terminal_adapter",
    "terminate_process_tree",
]
