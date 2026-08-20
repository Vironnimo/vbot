"""Probe: operator launch command survives early user input (race fix).

The WebUI takes control of a freshly started manual terminal immediately.
Before the fix, that first typed input cancelled the shared initial-input
task and the launch command was never entered into the shell (or was
corrupted mid-line, e.g. "xopencode2"). This probe proves that a command
started with user input arriving right after start still launches.

Uses a real TUI (opencode2) through ConPTY and checks that its
alternate-screen UI comes up.

Usage: python scripts/probe_terminal_command_race.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.tools.terminal_manager import TerminalManager


async def eventually(predicate: object, *, attempts: int = 150) -> None:
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
            command="opencode2",
            arguments=[],
            cwd=Path.home(),
        )
        terminal_id = result["terminal_id"]
        session = manager._sessions[terminal_id]

        # Reproduce the WebUI flow: user types immediately after start.
        await manager.send_operator_input(terminal_id, "x")

        await eventually(
            lambda: (
                "shift+tab" in session.renderer.screen_text()
                or "ctrl+p" in session.renderer.screen_text()
            )
        )
        screen = session.renderer.screen_text()
        print("--- screen tail ---")
        print(repr(screen[-250:]))
        print("PASS: opencode2 TUI launched despite immediate user input")
        return 0
    finally:
        await manager.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
