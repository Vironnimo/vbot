"""Probe: terminal snapshots re-emit alternate screen and terminal modes.

A late WebUI viewer (reconnect after tab switch or network gap) receives the
authoritative ANSI snapshot from the server. This probe proves that a live TUI
which enabled the alternate screen, application cursor keys, mouse reporting,
and bracketed paste produces a snapshot carrying those mode sequences, so the
browser xterm restores the interactive state instead of showing dead text.

The mode sequences travel through stdout from a real child program, exactly
like a TUI (opencode, claude code) emits them; the harness itself is a file so
the sequences are never passed through the shell's own line editor. The
snapshot is the first event a new viewer's watch receives.

Usage: python scripts/probe_terminal_snapshot_modes.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from pathlib import Path

# Import this checkout's packages first; from a linked worktree `core` would
# otherwise resolve through the editable install to the main checkout's code.
_CHECKOUT_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[:1] != [_CHECKOUT_ROOT]:
    sys.path.insert(0, _CHECKOUT_ROOT)

from core.tools.terminal_manager import TerminalManager  # noqa: E402

TUI_HARNESS_SOURCE = (
    "import sys,time\n"
    "sys.stdout.write('\\x1b[?1049h\\x1b[?1h\\x1b[?1000h\\x1b[?2004h"
    "\\x1b[2J\\x1b[H')\n"
    "sys.stdout.write('TUI-READY\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


async def wait_for_screen(manager: TerminalManager, terminal_id: str, text: str) -> None:
    for _ in range(120):
        if text in str((await manager.read_for_operator(terminal_id))["screen"]):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{text!r} did not appear on the screen")


async def late_viewer_snapshot(manager: TerminalManager, terminal_id: str) -> str:
    """Return the ANSI snapshot a viewer that connects now receives first."""
    async with contextlib.aclosing(manager.watch_for_operator(terminal_id)) as events:
        ready = await anext(events)
    return str(ready["ansi"])


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

        await wait_for_screen(manager, terminal_id, "TUI-READY")
        snapshot = await late_viewer_snapshot(manager, terminal_id)

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
