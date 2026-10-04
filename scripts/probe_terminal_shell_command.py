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
from collections.abc import Callable
from pathlib import Path

# Import this checkout's packages first; from a linked worktree `core` would
# otherwise resolve through the editable install to the main checkout's code.
_CHECKOUT_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[:1] != [_CHECKOUT_ROOT]:
    sys.path.insert(0, _CHECKOUT_ROOT)

from core.tools.terminal_manager import TerminalManager  # noqa: E402

PYTHON_REPL = (
    "import sys; print('READY', flush=True); "
    "[print('ECHO:' + line.rstrip(), flush=True) for line in sys.stdin]"
)


async def screen_until(
    manager: TerminalManager,
    terminal_id: str,
    predicate: Callable[[str], bool],
    *,
    attempts: int = 100,
) -> str:
    """Return the rendered screen once *predicate* accepts it."""
    for _ in range(attempts):
        screen = str((await manager.read_for_operator(terminal_id))["screen"])
        if predicate(screen):
            return screen
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
        print(f"launched: shell={result['command']!r} launch_command={result['launch_command']!r}")

        await screen_until(manager, terminal_id, lambda screen: "READY" in screen, attempts=120)
        print("PASS: the shell ran the command and its output appears")

        await manager.send_operator_input(terminal_id, "hello-shell\r")
        await screen_until(manager, terminal_id, lambda screen: "ECHO:hello-shell" in screen)
        print("PASS: the program still accepts interactive input")

        await manager.send_operator_input(terminal_id, "\x03")
        screen = await screen_until(
            manager, terminal_id, lambda screen: screen.endswith((">", "$"))
        )
        print("--- screen after Ctrl+C ---")
        print(repr(screen))
        print("---------------------------")
        print("PASS: Ctrl+C ended the foreground program; the shell prompt is back")
        await manager.send_operator_input(terminal_id, "echo SHELL-ALIVE\r")
        await screen_until(manager, terminal_id, lambda screen: "SHELL-ALIVE" in screen)
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
