"""VT terminal views, history, queries and ANSI snapshots."""

from __future__ import annotations

import copy
import hashlib
import itertools
from collections import deque
from typing import Any, NamedTuple

import pyte
from pyte import charsets

from ._terminal_cells import (
    _cell_style,
    _marked_row,
    _normalize_terminal_title,
    _render_buffer_line,
    _style_sequence,
    _visible_cells,
)
from ._terminal_control import SignalScanner as SignalScanner
from ._terminal_control import _EmulatorStream, _GuardedScreen
from ._terminal_vt_screen import _EmulatorScreen
from ._terminal_vt_types import (
    _ALTERNATE_SCREEN_MODES,
    _APPLICATION_CURSOR_MODE,
    _BRACKETED_PASTE_MODE,
    _FOCUS_REPORTING_MODE,
)
from ._terminal_vt_types import FOCUS_IN_REPORT as FOCUS_IN_REPORT
from ._terminal_vt_types import FOCUS_OUT_REPORT as FOCUS_OUT_REPORT
from ._terminal_vt_types import HIGHLIGHT_END as HIGHLIGHT_END
from ._terminal_vt_types import HIGHLIGHT_START as HIGHLIGHT_START
from ._terminal_vt_types import SUGGESTION_END as SUGGESTION_END
from ._terminal_vt_types import SUGGESTION_START as SUGGESTION_START
from ._terminal_vt_types import TERMINAL_TITLE_MAX_CHARS as TERMINAL_TITLE_MAX_CHARS
from ._terminal_vt_types import EmulatorUpdate as EmulatorUpdate
from ._terminal_vt_types import ScreenObservation as ScreenObservation
from ._terminal_vt_types import ViewChange as ViewChange


class _HistoryLine(NamedTuple):
    number: int
    # A wrapped row keeps its trailing spaces: its text continues on the next row.
    text: str
    wrapped: bool = False


class TerminalEmulator:
    """Rendered screen plus bounded history addressed by stable line numbers.

    Line numbers start at 0 and keep naming the same lines as output arrives
    or resizing moves them between history and the screen. The current
    screen starts right after the newest history line; only the oldest
    lines beyond the retention bound disappear.
    """

    def __init__(
        self, columns: int, rows: int, *, scrollback_lines: int, max_rows: int | None = None
    ) -> None:
        if max_rows is not None and rows > max_rows:
            raise ValueError(f"rows must not exceed max_rows ({max_rows})")
        self.columns = columns
        self.rows = rows
        self._max_rows = max_rows
        self._next_line = 0
        self._history: deque[_HistoryLine] = deque(maxlen=scrollback_lines)
        # Only rows that can return to the screen need full cells. Keep the
        # maximum height, not the current one: shrinking must not evict the
        # rows that a later growth restores. Without a height bound every
        # retained row remains restorable.
        cell_rows = scrollback_lines if max_rows is None else min(scrollback_lines, max_rows)
        self._history_cells: deque[Any] = deque(maxlen=cell_rows)
        # Where a wait's pattern starts matching: a row number, and whether
        # the logical line on that row is skipped too. None: every retained line.
        self._match_start: tuple[int, bool] | None = None
        # Where the current chunk first erased the whole primary screen.
        self._cleared_line: int | None = None
        self._screen = _EmulatorScreen(
            columns,
            rows,
            self._remember_line,
            self._restore_history,
            self._cleared,
            self._clear_history,
        )
        self._guarded = _GuardedScreen(self._screen)
        self._stream = _EmulatorStream(
            self._guarded, respond=self._respond_to_query, on_error=self._guarded.record
        )
        self._responses: list[str] = []
        self._signals = SignalScanner()
        # Keys of the view shown now and of the one shown before it.
        self._shown_view: tuple[int | None, int | None] = (None, None)

    def feed(self, text: str) -> EmulatorUpdate:
        """Render program output and report what it changed.

        Output the screen cannot apply is skipped, never raised: the update
        counts it. A program that enables focus reporting is answered that
        the terminal does not have the focus, so it treats its user as away.
        """
        signals = tuple(self._signals.scan(text))
        focus_requests = self._screen.focus_requests
        self._cleared_line = None
        self._guarded.skipped, self._guarded.skipped_error = 0, ""
        alternate_exits = self._screen.alternate_exit_revision
        try:
            self._stream.feed(text)
        except Exception as error:
            # The stream skips what it cannot parse; this is a last resort.
            self._guarded.record("feed", error)
            self._stream.reset_parser()
        if self._screen.focus_requests != focus_requests:
            self._responses.append(FOCUS_OUT_REPORT)
        responses = "".join(self._responses)
        self._responses.clear()
        view = self._view_key()
        shown, before = self._shown_view
        view_change: ViewChange = (
            "same" if view == shown else "returned" if view == before else "new"
        )
        if view != shown:
            self._shown_view = (view, shown)
        return EmulatorUpdate(
            responses=responses,
            title=self.title,
            bracketed_paste=self._screen.bracketed_paste_enabled,
            alternate_screen=self._screen.alternate_active,
            alternate_exited=self._screen.alternate_exit_revision != alternate_exits,
            signals=signals,
            focus_reporting=_FOCUS_REPORTING_MODE in self._screen.private_modes,
            cleared_line=self._cleared_line,
            skipped=self._guarded.skipped,
            skipped_error=self._guarded.skipped_error,
            application_cursor=_APPLICATION_CURSOR_MODE in self._screen.private_modes,
            view_change=view_change,
        )

    def _view_key(self) -> int:
        """Identify what the terminal shows; cheaper than ``_signature``, as
        every chunk needs it. Equal views can differ in key (cells stored in
        another order), never the other way round."""
        screen = self._screen
        buffer = screen.buffer
        cursor = screen.cursor
        return hash(
            (
                tuple(tuple(buffer[row].items()) for row in range(self.rows)),
                cursor.x,
                cursor.y,
                cursor.hidden,
                self._next_line,
                screen.alternate_active,
                self.title,
            )
        )

    def resize(self, columns: int, rows: int) -> None:
        if self._max_rows is not None and rows > self._max_rows:
            raise ValueError(f"rows must not exceed max_rows ({self._max_rows})")
        self._screen.resize(lines=rows, columns=columns)
        self.columns = columns
        self.rows = rows

    @property
    def title(self) -> str:
        """One bounded single-line title announced through the VT stream."""
        return _normalize_terminal_title(self._screen.title or self._screen.icon_name)

    def screen_text(self) -> str:
        """The screen's plain text, through its last non-blank row."""
        return "\n".join(self._screen_lines())

    def observe(self) -> ScreenObservation:
        """Return the screen as the Agent is shown it, with its change signature."""
        screen = self._screen
        cursor = screen.cursor
        suggestion_from = None
        lines = []
        for y in range(screen.lines):
            line = screen.buffer[y]
            if not cursor.hidden and y == cursor.y:
                suggestion_from = cursor.x
            lines.append(_marked_row(line, screen.columns, suggestion_from=suggestion_from))
            if suggestion_from is not None:
                suggestion_from = 0 if getattr(line, "wrapped", False) else None
        while lines and not lines[-1]:
            lines.pop()
        position = None if cursor.hidden else (cursor.y + 1, min(cursor.x + 1, screen.columns))
        return ScreenObservation(
            self._signature(),
            "\n".join(lines),
            position,
            self._next_line,
            self._history[0].number if self._history else self._next_line,
            self._next_line + screen.primary_cursor_row,
        )

    def _signature(self) -> str:
        """Compare visible cells, including selection styles, without cursor blink."""
        digest = hashlib.sha256()
        default = self._screen.default_char
        for row in range(self.rows):
            line = self._screen.buffer[row]
            cells = [
                (column, line[column]) for column in range(self.columns) if line[column] != default
            ]
            if cells:
                digest.update(repr((row, cells)).encode("utf-8"))
        return digest.hexdigest()

    def page(self, *, start_line: int | None, limit: int) -> dict[str, Any]:
        """Return a page of history and screen lines by stable line number.

        Without ``start_line`` the page is the history right above the screen.
        ``previous_start_line`` and ``next_start_line`` address the adjacent
        older and newer pages; the default page has no newer one because the
        screen follows it.
        """
        screen_lines = self._screen_lines()
        first = self._history[0].number if self._history else self._next_line
        screen_start = self._next_line
        total = screen_start + len(screen_lines)
        if start_line is None:
            end = screen_start
            start = max(first, end - limit)
        else:
            start = min(max(start_line, first), total)
            end = min(start + limit, total)
        selected = [
            self._history[number - first].text for number in range(start, min(end, screen_start))
        ]
        selected.extend(screen_lines[max(0, start - screen_start) : max(0, end - screen_start)])
        return {
            "text": "\n".join(selected),
            "first_line": first,
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "screen_start_line": screen_start,
            "previous_start_line": max(first, start - limit) if start > first else None,
            "next_start_line": end if start_line is not None and end < total else None,
        }

    def mark_input(self) -> None:
        """The Agent types now: a wait's pattern matches only lines below the cursor's line.

        So neither earlier output nor the echo of the input matches. On the
        alternate screen the primary screen's cursor counts, where output
        continues once the full-screen view closes.
        """
        self._match_start = (self._next_line + self._screen.primary_cursor_row, True)

    def pattern_text(self, *, start_line: int | None) -> dict[str, Any]:
        """The output a wait's pattern is matched against, and the line to check from next.

        The text is logical lines: rows a long line was auto-wrapped into are
        joined again. Without input every retained line counts; after
        ``mark_input`` only the lines below the input's line, or, once the
        screen was cleared since, the lines from the cleared screen on. A
        full-screen view counts only what it shows. *start_line* skips lines
        an earlier check already read; ``next_line`` is the start of the
        logical line just above the screen, the first that can still change.
        """
        screen_rows = self._screen_rows()
        first = self._history[0].number if self._history else self._next_line
        screen_start = self._next_line

        def wrapped(number: int) -> bool:
            if number < first:
                return False
            if number < screen_start:
                return self._history[number - first].wrapped
            index = number - screen_start
            return index < len(screen_rows) and screen_rows[index][1]

        begin = first
        if self._screen.alternate_active:
            begin = screen_start
        elif self._match_start is not None:
            row, skip_line = self._match_start
            if skip_line:
                while wrapped(row):
                    row += 1
                row += 1
            begin = max(begin, row)
        if start_line is not None:
            begin = max(begin, start_line)
        rows: list[tuple[str, bool]] = [
            (line.text, line.wrapped)
            for line in itertools.islice(self._history, max(0, begin - first), None)
        ]
        rows.extend(screen_rows[max(0, begin - screen_start) :])
        lines: list[str] = []
        parts: list[str] = []
        for text, continues in rows:
            parts.append(text)
            if not continues:
                lines.append("".join(parts))
                parts.clear()
        if parts:
            lines.append("".join(parts))
        next_line = screen_start - 1
        while next_line > first and wrapped(next_line - 1):
            next_line -= 1
        return {"text": "\n".join(lines), "next_line": max(first, next_line)}

    def ansi_snapshot(self) -> str:
        """Serialize bounded history plus the current screen for a late viewer.

        The snapshot starts with a full reset (RIS), so output a viewer has
        not yet drawn cannot land on top of it, then re-emits the tracked
        terminal modes before the screen text so the viewer lands in the same
        state as the live terminal: alternate screen (TUI), application cursor
        keys, mouse reporting, and bracketed paste. Without these a late
        viewer renders the alternate screen as plain text on the primary
        screen, and mouse-dependent TUI controls are dead.
        """
        screen = self._screen
        parts = ["\x1bc", "\x1b[?25l", "\x1b[0m", "\x1b[2J", "\x1b[H"]
        if screen.alternate_active:
            parts.append("\x1b[?1049h")
        for mode in sorted(screen.private_modes):
            if mode in _ALTERNATE_SCREEN_MODES:
                continue
            parts.append(f"\x1b[?{mode}h")
        if screen.bracketed_paste_enabled and _BRACKETED_PASTE_MODE not in screen.private_modes:
            parts.append(f"\x1b[?{_BRACKETED_PASTE_MODE}h")
        if self._history:
            parts.append("\r\n".join(line.text for line in self._history))
            parts.append("\r\n")
        active_style: tuple[Any, ...] | None = None
        for row_index in range(self.rows):
            if row_index:
                parts.append("\r\n")
            # A wide glyph already advances the viewer by two columns; its
            # second cell is skipped, or the snapshot would wrap and scroll.
            for _column, cell, text in _visible_cells(
                screen.buffer[row_index], self.columns, conceal=False
            ):
                style = _cell_style(cell)
                if style != active_style:
                    parts.append(_style_sequence(cell))
                    active_style = style
                parts.append(text)
        cursor = screen.cursor
        parts.extend(
            (
                "\x1b[0m",
                f"\x1b[{cursor.y + 1};{cursor.x + 1}H",
                "\x1b[?25l" if cursor.hidden else "\x1b[?25h",
            )
        )
        # The cells hold their characters already translated, so the character
        # sets in use are designated only after them, for the output to come.
        if screen.g0_charset == charsets.VT100_MAP:
            parts.append("\x1b(0")
        if screen.g1_charset == charsets.VT100_MAP:
            parts.append("\x1b)0")
        if screen.charset:
            parts.append("\x0e")
        return "".join(parts)

    def _screen_lines(self) -> list[str]:
        screen = self._screen
        lines = [_render_buffer_line(screen.buffer[y], screen.columns) for y in range(screen.lines)]
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def _screen_rows(self) -> list[tuple[str, bool]]:
        """Each screen row's text and whether it is wrapped, through the last non-blank row."""
        screen = self._screen
        rows: list[tuple[str, bool]] = []
        for y in range(screen.lines):
            line = screen.buffer[y]
            continues = bool(getattr(line, "wrapped", False))
            rows.append((_render_buffer_line(line, screen.columns, strip=not continues), continues))
        while rows and not rows[-1][0].strip():
            rows.pop()
        return rows

    def _remember_line(self, line: Any) -> None:
        continues = bool(getattr(line, "wrapped", False))
        self._history.append(
            _HistoryLine(
                self._next_line,
                _render_buffer_line(line, self._screen.columns, strip=not continues),
                continues,
            )
        )
        # Cells are immutable, but the screen can still mutate their row.
        self._history_cells.append(copy.copy(line))
        self._next_line += 1

    def _restore_history(self, count: int) -> list[Any]:
        """Move the newest saved rows back to the screen without renumbering them."""
        restored = [self._history.pop() for _ in range(min(count, len(self._history)))]
        if not restored:
            return []
        restored.reverse()
        self._next_line = restored[0].number
        cells = [self._history_cells.pop() for _ in restored]
        cells.reverse()
        return cells

    def _clear_history(self) -> None:
        self._history.clear()
        self._history_cells.clear()

    def _cleared(self) -> None:
        # Everything drawn on a cleared screen is new: after input, the
        # pattern matches from the cleared screen on.
        if self._match_start is not None:
            self._match_start = (self._next_line, False)
        if self._cleared_line is None:
            self._cleared_line = self._next_line

    def _respond_to_query(
        self, prefix: str, params: tuple[int, ...], intermediate: str, final: str
    ) -> None:
        """Answer a device query (DSR, DA, window size, DECRQM) as xterm does."""
        # Invalid or unsupported reports are silent, as on the underlying VT.
        if len(params) != 1:
            return
        mode = params[0]
        response = None
        if final == "n" and not intermediate:
            if mode == 5 and not prefix:
                response = "\x1b[0n"
            elif mode == 6 and prefix in {"", "?"}:
                cursor = self._screen.cursor
                row = cursor.y + 1
                if pyte.modes.DECOM in self._screen.mode and self._screen.margins is not None:
                    row -= self._screen.margins.top
                column = min(cursor.x + 1, self.columns)
                response = f"\x1b[{prefix}{row};{column}R"
        elif final == "c" and not intermediate and mode == 0:
            if not prefix:
                response = "\x1b[?6c"
            elif prefix == ">":
                response = "\x1b[>0;0;0c"
        elif final == "t" and not prefix and not intermediate and mode == 18:
            response = f"\x1b[8;{self.rows};{self.columns}t"
        elif final == "p" and intermediate == "$" and prefix in {"", "?"}:
            tracked = mode << 5 if prefix else mode
            known = (
                mode in {1, 6, 7, 25, 47, 1000, 1002, 1003, 1006, 1047, 1049, 2004}
                if prefix
                else mode in {4, 20}
            )
            enabled = tracked in self._screen.mode
            status = (1 if enabled else 2) if known else 0
            response = f"\x1b[{prefix}{mode};{status}$y"
        if response is not None:
            self._responses.append(response)
