"""VT emulation for Terminal Sessions: rendered screen, history and replies.

The module imports only the standard library and pyte, so a Terminal renderer
host can load it into an isolated subinterpreter without vBot's packages
(``_terminal_render_host.py``). Keep it free of ``core`` imports.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import re
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple, cast, override

import pyte
from pyte import graphics

_ALTERNATE_SCREEN_MODES = frozenset({47, 1047, 1049})
_BRACKETED_PASTE_MODE = 2004
_APPLICATION_CURSOR_MODE = 1
TERMINAL_TITLE_MAX_CHARS = 160
# A repeat request (REP) draws at most one screen of characters.
_MAX_REPEAT_SCREENS = 1
# pyte ignores the ``<``/``>``/``=`` CSI prefixes and dispatches the payload
# as a normal sequence. A TUI enabling xterm keyboard modes (for example
# ``CSI > 4 ; 1 m`` for modifyOtherKeys) is then misread as SGR
# "underscore + bold", and every blank cell written afterwards inherits
# those attributes - the viewer draws white underlines under blank rows.
# pyte cannot act on any of these keyboard/DA sequences, so they are
# dropped before they reach it; only the secondary device attributes query
# (``CSI > c``) stays for the query replies.
_XT_KEYBOARD_MODE_COMPLETE = re.compile(r"\x1b\[(?!>[0-9;]*c)[<>=][0-9;:]*[ -/]*[@-~]")
# pyte ends a CSI at its first ``:`` and draws the rest as text, so colon
# sub-parameters (``38:2::R:G:B``, ``4:3`` curly underline) would leave
# digits on the screen. SGR keeps their meaning in semicolon form; other
# sequences with sub-parameters are dropped.
_COLON_SEQUENCE = re.compile(r"\x1b\[([0-9;]*:[0-9;:]*)([ -/]*)([@-~])")
# An incomplete escape or CSI at the end of a chunk is held back until the
# next chunk completes it.
_INCOMPLETE_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*)?$")
_TERMINAL_QUERY = re.compile(r"\x1b\[([?>]?)([0-9;]*)(\$?)([cntp])")
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
    "_private_modes",
    "bracketed_paste_enabled",
    "_last_graphic",
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


class _Cell(NamedTuple):
    """pyte's character cell plus the dim (faint) attribute pyte lacks."""

    data: str
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False
    dim: bool = False


class EmulatorUpdate(NamedTuple):
    """What one fed chunk changed that the Terminal Session acts on."""

    # Replies to terminal queries, to write back to the program.
    responses: str
    title: str
    bracketed_paste: bool
    alternate_screen: bool
    # The chunk left the alternate screen (the primary screen came back).
    alternate_exited: bool
    # Transcript lines that became final in this chunk (transcript screens only).
    transcript: tuple[str, ...] = ()


class ScreenObservation(NamedTuple):
    """The visible screen at one moment: change signature and plain-text tail."""

    signature: str
    tail: str


class _EmulatorStream(pyte.Stream):
    csi = {
        **pyte.Stream.csi,
        "S": "scroll_up",
        "T": "scroll_down",
        "b": "repeat_character",
    }


class _EmulatorScreen(pyte.Screen):
    """pyte's screen plus history, alternate screen, dim and soft-wrap tracking.

    A row the cursor left by auto-wrap carries ``wrapped = True``: its text
    continues on the next row. Explicit line feeds and erasing the row's end
    clear the mark, so it names exactly the rows a long line was folded into.
    ``on_clear`` runs when the whole primary screen is erased or reset.
    """

    def __init__(
        self,
        columns: int,
        lines: int,
        on_scroll: Callable[[Any], None],
        on_clear: Callable[[], None],
    ) -> None:
        self._on_scroll = on_scroll
        self._on_clear = on_clear
        self._primary_state: dict[str, Any] | None = None
        self._alternate_modes: set[int] = set()
        self._private_modes: set[int] = set()
        self._last_graphic = ""
        self._drawing = False
        self.alternate_exit_revision = 0
        self.bracketed_paste_enabled = False
        # Top rows of the primary screen already emitted to the transcript.
        self.committed_rows = 0
        super().__init__(columns, lines)

    @property
    @override
    def default_char(self) -> Any:
        reverse = pyte.modes.DECSCNM in self.mode
        return _Cell(data=" ", reverse=reverse)

    @override
    def reset(self) -> None:
        super().reset()
        self.cursor.attrs = self.default_char
        if self._primary_state is None:
            self._on_clear()

    @property
    def alternate_active(self) -> bool:
        """Whether the rendered screen is the alternate screen buffer."""
        return self._primary_state is not None

    @property
    def primary_cursor_row(self) -> int:
        """The cursor's row on the primary screen, also while the alternate one shows."""
        if self._primary_state is None:
            return int(self.cursor.y)
        return int(self._primary_state["cursor"].y)

    @property
    def private_modes(self) -> frozenset[int]:
        """Private modes currently enabled by the foreground program."""
        return frozenset(self._private_modes)

    @override
    def draw(self, data: str) -> None:
        # pyte auto-wraps inside draw by a carriage return and a line feed.
        self._drawing = True
        try:
            super().draw(data)
        finally:
            self._drawing = False
        if data:
            self._last_graphic = data[-1]

    @override
    def linefeed(self) -> None:
        line: Any = self.buffer[self.cursor.y]
        if self._drawing:
            line.wrapped = True
        elif getattr(line, "wrapped", False):
            line.wrapped = False
        super().linefeed()

    @override
    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        if how == 2 or (how == 0 and self.cursor.x < self.columns):
            line: Any = self.buffer[self.cursor.y]
            if getattr(line, "wrapped", False):
                line.wrapped = False
        super().erase_in_line(how, private)

    @override
    def erase_in_display(self, how: int = 0, *args: Any, **kwargs: Any) -> None:
        if how in (0, 2, 3):
            first = self.cursor.y + 1 if how == 0 else 0
            for y in range(first, self.lines):
                line: Any = self.buffer.get(y)
                if line is not None and getattr(line, "wrapped", False):
                    line.wrapped = False
            if self._primary_state is None:
                # Erased rows the transcript already holds are gone; what is
                # drawn there next is new content.
                self.committed_rows = min(self.committed_rows, self.cursor.y if how == 0 else 0)
        super().erase_in_display(how, *args, **kwargs)
        if how in (2, 3) and self._primary_state is None:
            self._on_clear()

    @override
    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        # Like xterm, only a region that starts at the top row scrolls lines
        # into history; a region lower on the screen discards them.
        if self.cursor.y == bottom and top == 0 and self._primary_state is None:
            self._on_scroll(self.buffer[top])
        super().index()

    def scroll_up(self, *params: int) -> None:
        """SU: scroll the region up, as if the cursor indexed at its bottom."""
        if len(params) > 1:
            return
        count = min(params[0] if params and params[0] else 1, self.lines)
        _top, bottom = self.margins or (0, self.lines - 1)
        x, y = self.cursor.x, self.cursor.y
        self.cursor.y = bottom
        for _ in range(count):
            self.index()
        self.cursor.x, self.cursor.y = x, y

    def scroll_down(self, *params: int) -> None:
        """SD: scroll the region down; ``CSI T`` with several values is mouse tracking."""
        if len(params) > 1:
            return
        count = min(params[0] if params and params[0] else 1, self.lines)
        top, _bottom = self.margins or (0, self.lines - 1)
        x, y = self.cursor.x, self.cursor.y
        self.cursor.y = top
        for _ in range(count):
            self.reverse_index()
        self.cursor.x, self.cursor.y = x, y

    def repeat_character(self, *params: int) -> None:
        """REP: draw the last graphic character again."""
        if not self._last_graphic:
            return
        count = min(
            params[0] if params and params[0] else 1,
            self.columns * self.lines * _MAX_REPEAT_SCREENS,
        )
        self.draw(self._last_graphic * count)

    @override
    def select_graphic_rendition(self, *attrs: int, **kwargs: Any) -> None:
        if kwargs.get("private"):
            return
        # pyte has no dim attribute. Find 2 (dim) and 22 (normal intensity)
        # outside the operands of extended colors, apply them here and pass
        # every other attribute to pyte.
        dim: bool | None = None
        remaining: list[int] = []
        index = 0
        while index < len(attrs):
            attr = attrs[index]
            if attr in (graphics.FG_256, graphics.BG_256):
                mode = attrs[index + 1] if index + 1 < len(attrs) else None
                operands = 2 if mode == 5 else 4 if mode == 2 else 1
                remaining.extend(attrs[index : index + 1 + operands])
                index += 1 + operands
                continue
            if attr == 2:
                dim = True
            else:
                if attr in (0, 22):
                    dim = False
                remaining.append(attr)
            index += 1
        if not attrs:
            dim = False
        if remaining or not attrs:
            super().select_graphic_rendition(*remaining)
        if dim is not None:
            # The cursor attributes are always a _Cell (see default_char).
            self.cursor.attrs = cast(Any, self.cursor.attrs)._replace(dim=dim)

    @override
    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        private = kwargs.get("private")
        alternate = _ALTERNATE_SCREEN_MODES.intersection(modes) if private else set()
        if alternate and self._primary_state is None:
            self._primary_state = self._capture_state()
            super().reset()
            self.cursor.attrs = self.default_char
        if private:
            self._private_modes.update(modes)
            if _BRACKETED_PASTE_MODE in modes:
                self.bracketed_paste_enabled = True
        self._alternate_modes.update(alternate)
        super().set_mode(*modes, **kwargs)

    @override
    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        private = kwargs.get("private")
        alternate = _ALTERNATE_SCREEN_MODES.intersection(modes) if private else set()
        if private:
            self._private_modes.difference_update(modes)
            if _BRACKETED_PASTE_MODE in modes:
                self.bracketed_paste_enabled = False
        self._alternate_modes.difference_update(alternate)
        if alternate and not self._alternate_modes and self._primary_state is not None:
            primary_state = self._primary_state
            self._primary_state = None
            self._restore_state(primary_state)
            self.alternate_exit_revision += 1
            remaining = tuple(mode for mode in modes if mode not in alternate)
            if remaining:
                super().reset_mode(*remaining, **kwargs)
            return
        super().reset_mode(*modes, **kwargs)

    @override
    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        lines = lines or self.lines
        columns = columns or self.columns
        if self._primary_state is None:
            self._resize_buffer(lines, columns, keep_history=True)
            return
        alternate_state = self._capture_state()
        self._restore_state(self._primary_state)
        self._resize_buffer(lines, columns, keep_history=True)
        self._primary_state = self._capture_state()
        self._restore_state(alternate_state)
        self._resize_buffer(lines, columns, keep_history=False)

    def _resize_buffer(self, lines: int, columns: int, *, keep_history: bool) -> None:
        """Resize like xterm: fewer rows first drop blank rows below the cursor,
        then scroll top rows into history so the cursor row stays visible."""
        if lines == self.lines and columns == self.columns:
            return
        if lines < self.lines:
            dropped = max(0, self.cursor.y - (lines - 1))
            for y in range(dropped):
                if keep_history:
                    self._on_scroll(self.buffer[y])
            if dropped:
                rows = {y - dropped: self.buffer[y] for y in range(dropped, self.lines)}
                self.buffer.clear()
                self.buffer.update(rows)
                self.cursor.y -= dropped
            for y in range(lines, self.lines):
                self.buffer.pop(y, None)
        if columns < self.columns:
            for line in self.buffer.values():
                for x in range(columns, self.columns):
                    line.pop(x, None)
            self.cursor.x = min(self.cursor.x, columns - 1)
        self.dirty.update(range(lines))
        self.lines, self.columns = lines, columns
        self.set_margins()

    def _capture_state(self) -> dict[str, Any]:
        return {
            field_name: copy.deepcopy(getattr(self, field_name))
            for field_name in _SCREEN_STATE_FIELDS
        }

    def _restore_state(self, state: Mapping[str, Any]) -> None:
        for field_name in _SCREEN_STATE_FIELDS:
            setattr(self, field_name, copy.deepcopy(state[field_name]))


class _HistoryLine(NamedTuple):
    number: int
    # A wrapped row keeps its trailing spaces: its text continues on the next row.
    text: str
    wrapped: bool = False


class TerminalEmulator:
    """Rendered screen plus bounded history addressed by stable line numbers.

    Line numbers count every line that ever scrolled into history, starting
    at 0, so a number keeps naming the same line while new output arrives;
    the current screen starts right after the newest history line. Only the
    oldest lines beyond the retention bound disappear.

    With *transcript*, the emulator also yields the complete rendered output
    as logical lines: a line becomes final when it scrolls off the primary
    screen, rows a long line was auto-wrapped into are joined again, and
    ``commit_transcript`` emits the rows still on screen. Redraws (progress
    bars, carriage returns, cursor movement) leave only their final text.
    """

    def __init__(
        self, columns: int, rows: int, *, scrollback_lines: int, transcript: bool = False
    ) -> None:
        self.columns = columns
        self.rows = rows
        self._next_line = 0
        self._history: deque[_HistoryLine] = deque(maxlen=scrollback_lines)
        # Where a wait's pattern starts matching: a row number, and whether
        # the logical line on that row is skipped too. None: every retained line.
        self._match_start: tuple[int, bool] | None = None
        self._screen = _EmulatorScreen(columns, rows, self._remember_line, self._cleared)
        self._stream = _EmulatorStream(self._screen)
        self._held = ""
        self._responses: list[str] = []
        self._transcript = transcript
        self._transcript_lines: list[str] = []
        # Row texts of a logical line whose last row has not become final yet.
        self._transcript_partial: list[str] = []

    def feed(self, text: str) -> EmulatorUpdate:
        """Render program output and report what it changed."""
        text = self._held + text
        self._held = ""
        incomplete = _INCOMPLETE_SEQUENCE.search(text)
        if incomplete:
            self._held = incomplete.group(0)
            text = text[: incomplete.start()]
        alternate_exits = self._screen.alternate_exit_revision
        if text:
            text = _COLON_SEQUENCE.sub(_semicolon_form, _XT_KEYBOARD_MODE_COMPLETE.sub("", text))
            offset = 0
            for match in _TERMINAL_QUERY.finditer(text):
                self._stream.feed(text[offset : match.start()])
                self._respond_to_query(*match.groups())
                offset = match.end()
            self._stream.feed(text[offset:])
        responses = "".join(self._responses)
        self._responses.clear()
        transcript = tuple(self._transcript_lines)
        self._transcript_lines.clear()
        return EmulatorUpdate(
            responses=responses,
            title=self.title,
            bracketed_paste=self._screen.bracketed_paste_enabled,
            alternate_screen=self._screen.alternate_active,
            alternate_exited=self._screen.alternate_exit_revision != alternate_exits,
            transcript=transcript,
        )

    def commit_transcript(self) -> tuple[str, ...]:
        """Emit the transcript through the last non-blank row of the primary screen.

        Emitted rows are not emitted again when they later scroll off. On the
        alternate screen nothing is emitted: a full-screen program's frames
        are not output.
        """
        if not self._transcript:
            return ()
        screen = self._screen
        if screen.alternate_active:
            return ()
        last = -1
        for y in range(screen.lines - 1, screen.committed_rows - 1, -1):
            if _render_buffer_line(screen.buffer[y], screen.columns):
                last = y
                break
        for y in range(screen.committed_rows, last + 1):
            self._transcribe_row(screen.buffer[y])
        if self._transcript_partial:
            self._transcript_lines.append("".join(self._transcript_partial).rstrip())
            self._transcript_partial.clear()
        screen.committed_rows = max(screen.committed_rows, last + 1)
        lines = tuple(self._transcript_lines)
        self._transcript_lines.clear()
        return lines

    def resize(self, columns: int, rows: int) -> None:
        self._screen.resize(lines=rows, columns=columns)
        self.columns = columns
        self.rows = rows

    @property
    def title(self) -> str:
        """One bounded single-line title announced through the VT stream."""
        return _normalize_terminal_title(self._screen.title or self._screen.icon_name)

    def screen_text(self) -> str:
        return "\n".join(self._screen_lines())

    def observe(self, tail_lines: int) -> ScreenObservation:
        """Return the change signature and newest non-blank rows together."""
        lines = self._screen_lines()
        tail = "\n".join(lines[-tail_lines:]) if tail_lines > 0 else ""
        return ScreenObservation(self._signature(), tail)

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
        if _APPLICATION_CURSOR_MODE in screen.mode:
            parts.append("\x1b[?1h")
        if screen.bracketed_paste_enabled and _BRACKETED_PASTE_MODE not in screen.private_modes:
            parts.append(f"\x1b[?{_BRACKETED_PASTE_MODE}h")
        if self._history:
            parts.append("\r\n".join(line.text for line in self._history))
            parts.append("\r\n")
        active_style: tuple[Any, ...] | None = None
        for row_index in range(self.rows):
            if row_index:
                parts.append("\r\n")
            line = screen.buffer[row_index]
            for column_index in range(self.columns):
                cell = line[column_index]
                # pyte stores the second cell of a wide glyph as empty. The
                # glyph already advances the viewer by two columns; emitting a
                # space for its continuation would wrap and scroll the snapshot.
                if not cell.data:
                    continue
                style = _cell_style(cell)
                if style != active_style:
                    parts.append(_style_sequence(cell))
                    active_style = style
                parts.append(cell.data)
        cursor = screen.cursor
        parts.extend(
            (
                "\x1b[0m",
                f"\x1b[{cursor.y + 1};{cursor.x + 1}H",
                "\x1b[?25l" if cursor.hidden else "\x1b[?25h",
            )
        )
        return "".join(parts)

    def _screen_lines(self) -> list[str]:
        lines = [line.rstrip() for line in self._screen.display]
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
        self._next_line += 1
        if not self._transcript:
            return
        if self._screen.committed_rows:
            self._screen.committed_rows -= 1
            return
        self._transcribe_row(line)

    def _cleared(self) -> None:
        # Everything drawn on a cleared screen is new: after input, the
        # pattern matches from the cleared screen on.
        if self._match_start is not None:
            self._match_start = (self._next_line, False)

    def _transcribe_row(self, line: Any) -> None:
        self._transcript_partial.append(
            "".join(line[column].data for column in range(self._screen.columns))
        )
        if getattr(line, "wrapped", False):
            return
        self._transcript_lines.append("".join(self._transcript_partial).rstrip())
        self._transcript_partial.clear()

    def _respond_to_query(
        self, prefix: str, parameters: str, intermediate: str, final: str
    ) -> None:
        values = parameters.split(";")
        # Invalid or unsupported reports are silent, as on the underlying VT.
        if len(values) != 1 or len(values[0]) > 6:
            return
        mode = int(values[0] or "0")
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


def _semicolon_form(match: re.Match[str]) -> str:
    """Rewrite an SGR with colon sub-parameters in semicolon form; drop others."""
    parameters, intermediate, final = match.groups()
    if final != "m" or intermediate:
        return ""
    attributes: list[str] = []
    for group in parameters.split(";"):
        values = group.split(":")
        head = values[0]
        if head in {"38", "48", "58"} and len(values) > 1:
            if head == "58":
                continue  # underline color: not rendered
            if values[1] == "5" and len(values) >= 3:
                attributes.extend((head, "5", values[2] or "0"))
            elif values[1] == "2" and len(values) >= 5:
                # 38:2:<colorspace>:R:G:B or 38:2:R:G:B
                red, green, blue = values[-3:]
                attributes.extend((head, "2", red or "0", green or "0", blue or "0"))
        elif head == "4" and len(values) > 1:
            attributes.append("24" if values[1] == "0" else "4")
        elif head:
            attributes.append(head)
    return f"\x1b[{';'.join(attributes)}m" if attributes else ""


def _render_buffer_line(line: Any, columns: int, *, strip: bool = True) -> str:
    text = "".join(line[column].data for column in range(columns))
    return text.rstrip() if strip else text


def _normalize_terminal_title(value: object) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in str(value or "")
    )
    return " ".join(printable.split())[:TERMINAL_TITLE_MAX_CHARS]


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
        getattr(cell, "dim", False),
    )


def _style_sequence(cell: Any) -> str:
    codes = [0]
    codes.extend(_ansi_color(cell.fg, background=False))
    codes.extend(_ansi_color(cell.bg, background=True))
    if cell.bold:
        codes.append(1)
    if getattr(cell, "dim", False):
        codes.append(2)
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


# Emulators hosted in a renderer subinterpreter, addressed by an integer key
# the host assigns. The host calls these functions by reference.
_HOSTED: dict[int, TerminalEmulator] = {}


def hosted_create(
    key: int, columns: int, rows: int, scrollback_lines: int, transcript: bool = False
) -> None:
    _HOSTED[key] = TerminalEmulator(
        columns, rows, scrollback_lines=scrollback_lines, transcript=transcript
    )


def hosted_call(key: int, method: str, arguments: tuple[Any, ...], options: dict[str, Any]) -> Any:
    return getattr(_HOSTED[key], method)(*arguments, **options)


def hosted_drop(key: int) -> None:
    _HOSTED.pop(key, None)
