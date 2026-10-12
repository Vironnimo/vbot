"""Terminal screens rendered off the Event Loop (see ``_interpreter_host``).

VT rendering is pure-Python CPU work proportional to program output. Each
screen is a ``TerminalEmulator`` on a renderer worker: a subinterpreter loaded
with only ``terminal_emulator.py`` and pyte. ``inline`` hosts render in the
calling thread instead: deterministic tests, and the fallback when no
subinterpreter can start.
"""

from __future__ import annotations

from typing import Any

from core.tools import terminal_emulator
from core.tools.terminal_emulator import EmulatorUpdate, ScreenObservation, TerminalEmulator

from ._interpreter_host import DEFAULT_WORKERS, HostedObject, InterpreterHost


class TerminalScreen:
    """The rendered screen of one Terminal Session, used from the Event Loop.

    Calls are applied in the order they are made; awaiting one does not let a
    later call overtake it.
    """

    def __init__(self, emulator: HostedObject, columns: int, rows: int) -> None:
        self._emulator = emulator
        self.columns = columns
        self.rows = rows

    async def feed(self, text: str) -> EmulatorUpdate:
        update: EmulatorUpdate = await self._emulator.call("feed", text)
        return update

    async def resize(self, columns: int, rows: int) -> None:
        self.columns = columns
        self.rows = rows
        await self._emulator.call("resize", columns, rows)

    async def observe(self, tail_lines: int) -> ScreenObservation:
        observation: ScreenObservation = await self._emulator.call("observe", tail_lines)
        return observation

    async def screen_text(self) -> str:
        text: str = await self._emulator.call("screen_text")
        return text

    async def page(self, *, start_line: int | None, limit: int) -> dict[str, Any]:
        page: dict[str, Any] = await self._emulator.call("page", start_line=start_line, limit=limit)
        return page

    async def mark_input(self) -> None:
        await self._emulator.call("mark_input")

    async def pattern_text(self, *, start_line: int | None) -> dict[str, Any]:
        found: dict[str, Any] = await self._emulator.call("pattern_text", start_line=start_line)
        return found

    async def ansi_snapshot(self) -> str:
        ansi: str = await self._emulator.call("ansi_snapshot")
        return ansi

    def close(self) -> None:
        """Release the screen; later calls fail."""
        self._emulator.close()


class TerminalRenderHost:
    """Create Terminal screens on renderer workers (or inline)."""

    def __init__(self, *, workers: int = DEFAULT_WORKERS, inline: bool = False) -> None:
        self._host = InterpreterHost(
            (terminal_emulator,), name="terminal render", workers=workers, inline=inline
        )

    @classmethod
    def in_process(cls) -> TerminalRenderHost:
        """A host that renders in the calling thread."""
        return cls(workers=1, inline=True)

    def open_screen(self, columns: int, rows: int, *, scrollback_lines: int) -> TerminalScreen:
        """Open a screen; raises RuntimeError once the host is closed."""
        emulator = self._host.open(
            TerminalEmulator, columns, rows, scrollback_lines=scrollback_lines
        )
        return TerminalScreen(emulator, columns, rows)

    def prepare(self) -> None:
        """Start a renderer worker for the next screen, or wait for the one
        starting; blocks briefly.

        When no subinterpreter can start, the host renders inline.
        """
        self._host.prepare()

    def close(self) -> None:
        """Stop the workers; open screens fail afterwards."""
        self._host.close()


__all__ = ["TerminalRenderHost", "TerminalScreen"]
