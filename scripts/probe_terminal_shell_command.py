"""Probe: operator terminal commands run inside the interactive shell.

Live-verifies that a manual terminal started with a command behaves like a
normal terminal: the command runs inside the default shell, the shell echoes
interactive input, Ctrl+C stops only the foreground program, and the shell
prompt keeps the Terminal Session alive afterwards.

Usage: python scripts/probe_terminal_shell_command.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from core.tools.terminal_manager import TerminalManager

PYTHON_REPL = (
    "import sys; print('READY', flush=True); "
    "[print('ECHO:' + line.rstrip(), flush=True) for line in sys.stdin]"
)


async def eventually(predicate: object, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not reached")


async def main() -> int:
    manager = TerminalManager(sweep_interval_seconds=3600)
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command=sys.executable,
            arguments=["-u", "-c", PYTHON_REPL],
            cwd=Path.home(),
        )
        terminal_id = result["terminal_id"]
        session = manager._sessions[terminal_id]
        print(f"launched: shell={result['command']!r} launch_command={result['launch_command']!r}")

        await eventually(
            lambda: "READY" in session.renderer.screen_text(),
            attempts=120,
        )
        print("PASS: command was entered into the shell and its output appears")

        await manager.send_operator_input(terminal_id, "hello-shell\r")
        await eventually(lambda: "ECHO:hello-shell" in session.renderer.screen_text())
        print("PASS: the shell session still accepts interactive input")

        await manager.send_operator_input(terminal_id, "\x03")
        await eventually(
            lambda: (
                session.renderer.screen_text().endswith(">")
                or session.renderer.screen_text().endswith("$")
            )
        )
        print("--- screen after Ctrl+C ---")
        print(repr(session.renderer.screen_text()))
        print("---------------------------")
        print("PASS: Ctrl+C ended the foreground program; the shell prompt is back")
        await manager.send_operator_input(terminal_id, "echo SHELL-ALIVE\r")
        await eventually(lambda: "SHELL-ALIVE" in session.renderer.screen_text())
        print("PASS: the shell accepts further commands after Ctrl+C")

        summary = manager.list_for_operator()[0]
        print(
            f"final: state={summary['state']} command={summary['command']!r} "
            f"launch_command={summary['launch_command']!r}"
        )
        if summary["state"] in {"exited", "error"}:
            print("FAIL: the Terminal Session ended although the shell is alive")
            return 1
        print("PASS: the Terminal Session is still live like a normal terminal")
        return 0
    finally:
        await manager.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
