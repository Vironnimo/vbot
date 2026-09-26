"""Program presence in a process tree: what guarded Terminal input relies on."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

from core.utils.processes import _names_program, process_tree_runs

PYTHON = Path(sys.executable).name.casefold().removesuffix(".exe")
SLEEPER = "import time; time.sleep(30)"


def _shell_running_python() -> subprocess.Popen[bytes]:
    """A shell that waits for a Python child, then runs something else.

    As in a Terminal, the shell outlives the program it started.
    """
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        script = f'"{sys.executable}" -c "{SLEEPER}" & ping -n 30 127.0.0.1 >nul'
        return subprocess.Popen(f'"{comspec}" /d /s /c "{script}"')
    return subprocess.Popen(["/bin/sh", "-c", f'"{sys.executable}" -c "{SLEEPER}"; sleep 30'])


def _eventually(predicate) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _python_children(shell: subprocess.Popen[bytes]) -> list[psutil.Process]:
    # A virtual environment's launcher starts the real interpreter as its own child.
    return [
        child
        for child in psutil.Process(shell.pid).children(recursive=True)
        if child.name().casefold().removesuffix(".exe") == PYTHON
    ]


def _end(shell: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(psutil.NoSuchProcess):
        for child in psutil.Process(shell.pid).children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
    shell.kill()
    shell.wait(timeout=15)


def test_a_program_counts_while_it_runs_below_the_shell() -> None:
    shell = _shell_running_python()
    try:
        assert _eventually(lambda: process_tree_runs(shell.pid, PYTHON))
        assert not process_tree_runs(shell.pid, "codex")
        for child in _python_children(shell):
            child.kill()
        assert _eventually(lambda: not process_tree_runs(shell.pid, PYTHON))
        assert shell.poll() is None
    finally:
        _end(shell)
    # A Terminal whose process tree is gone runs nothing.
    assert not process_tree_runs(shell.pid, PYTHON)


def test_a_stopped_program_does_not_count() -> None:
    shell = _shell_running_python()
    try:
        assert _eventually(lambda: process_tree_runs(shell.pid, PYTHON))
        children = _python_children(shell)
        for child in children:
            child.suspend()
        assert _eventually(lambda: not process_tree_runs(shell.pid, PYTHON))
        for child in children:
            child.resume()
        assert _eventually(lambda: process_tree_runs(shell.pid, PYTHON))
    finally:
        _end(shell)


@pytest.mark.parametrize(
    ("name", "exe", "cmdline", "program"),
    [
        # Native executables, as Claude Code and Codex install them.
        ("claude.exe", r"C:\Users\u\.local\bin\claude.exe", ["claude"], "claude"),
        ("codex", "/usr/local/bin/codex", ["codex", "--yolo"], "codex"),
        # A versioned binary started through a link keeps the name it was started as.
        ("claude", "/home/u/.local/share/claude/versions/2.1.280", ["claude"], "claude"),
        ("codex-x86_64-unknown-linux-musl", "", [], "codex"),
        # npm installs: node runs the program's script.
        (
            "node.exe",
            r"C:\Program Files\nodejs\node.exe",
            [
                r"C:\Program Files\nodejs\node.exe",
                r"C:\Users\u\AppData\Roaming\npm\node_modules\@openai\codex\bin\codex.js",
            ],
            "codex",
        ),
        (
            "node",
            "/usr/bin/node",
            ["node", "--no-warnings", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
            "claude",
        ),
    ],
)
def test_names_that_run_a_program(name: str, exe: str, cmdline: list[str], program: str) -> None:
    assert _names_program(name, exe, cmdline, program)


@pytest.mark.parametrize(
    ("name", "exe", "cmdline", "program"),
    [
        # A shell whose command line mentions the program is still the shell.
        ("pwsh.exe", r"C:\Program Files\PowerShell\7\pwsh.exe", ["pwsh", "-c", "codex"], "codex"),
        ("bash", "/bin/bash", ["bash", "/home/u/codex/run.sh"], "codex"),
        # Another script of node, and names that only start with the program's letters.
        ("node", "/usr/bin/node", ["node", "/home/u/codex/build.js"], "codex"),
        ("codexer", "/usr/bin/codexer", ["codexer"], "codex"),
        ("claudette.exe", "", ["claudette"], "claude"),
    ],
)
def test_names_that_do_not_run_a_program(
    name: str, exe: str, cmdline: list[str], program: str
) -> None:
    assert not _names_program(name, exe, cmdline, program)
