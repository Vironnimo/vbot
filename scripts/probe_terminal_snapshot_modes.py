"""Probe: terminal snapshots re-emit alternate screen and terminal modes.

A late WebUI viewer (reconnect after tab switch or network gap) receives the
authoritative ANSI snapshot from the server. This probe proves that a live TUI
which enabled the alternate screen, application cursor keys, mouse reporting,
and bracketed paste produces a snapshot carrying those mode sequences, so the
browser xterm restores the interactive state instead of showing dead text.

The mode sequences travel through stdout from a real child program, exactly
like a TUI (opencode, claude code) emits them; the harness itself is a file so
the sequences are never passed through the shell's own line editor.

Usage: python scripts/probe_terminal_snapshot_modes.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from core.tools.terminal_manager import TerminalManager

TUI_HARNESS_SOURCE = (
    "import sys,time\n"
    "sys.stdout.write('\\x1b[?1049h\\x1b[?1h\\x1b[?1000h\\x1b[?2004h"
    "\\x1b[2J\\x1b[H')\n"
    "sys.stdout.write('TUI-READY\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


async def eventually(predicate: object, *, attempts: int = 120) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not reached")


async def main() -> int:
    manager = TerminalManager(sweep_interval_seconds=3600)
    manager.start()
    try:
        harness = Path(tempfile.gettempdir()) / "vbot_probe_tui_modes.py"
        harness.write_text(TUI_HARNESS_SOURCE, encoding="utf-8")
        result = await manager.spawn_for_operator(
            command=sys.executable,
            arguments=[str(harness)],
            cwd=Path.home(),
        )
        terminal_id = result["terminal_id"]
        session = manager._sessions[terminal_id]

        await eventually(lambda: "TUI-READY" in session.renderer.screen_text())
        snapshot = session.renderer.ansi_snapshot()

        expected = {
            "\x1b[?1049h": "alternate screen",
            "\x1b[?1h": "application cursor keys",
            "\x1b[?1000h": "mouse reporting",
            "\x1b[?2004h": "bracketed paste",
        }
        failed = False
        for sequence, label in expected.items():
            if sequence in snapshot:
                print(f"PASS: snapshot carries {label} ({sequence!r})")
            else:
                print(f"FAIL: snapshot misses {label} ({sequence!r})")
                failed = True
        print(f"snapshot length: {len(snapshot)} chars")
        return 1 if failed else 0
    finally:
        await manager.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
