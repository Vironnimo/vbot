"""Terminal screen operations, alternate buffers and Unicode cells."""

from __future__ import annotations

import copy
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, cast, override

import pyte
from pyte import charsets, graphics
from pyte import modes as screen_modes
from pyte.screens import Cursor, Savepoint, wcwidth

from ._terminal_cells import _is_wide
from ._terminal_vt_types import (
    _ALTERNATE_SCREEN_MODES,
    _BRACKETED_PASTE_MODE,
    _ERASE_SAVED_LINES,
    _FOCUS_REPORTING_MODE,
    _MAX_CELL_MARKS,
    _MAX_REPEAT_SCREENS,
    _REVERSE_SCREEN_MODE,
    _SCREEN_STATE_FIELDS,
    _SOFT_RESET_MODES,
    _Cell,
)


class _EmulatorScreen(pyte.Screen):
    """pyte's screen plus history, alternate screen, dim and soft-wrap tracking.

    A row the cursor left by auto-wrap carries ``wrapped = True``: its text
    continues on the next row. Explicit line feeds and erasing the row's end
    clear the mark, so it names exactly the rows a long line was folded into.
    ``on_clear`` runs when the whole primary screen is erased or reset;
    ``on_clear_history`` when the program erases the saved lines (ED 3).
    """

    def __init__(
        self,
        columns: int,
        lines: int,
        on_scroll: Callable[[Any], None],
        restore_history: Callable[[int], list[Any]],
        on_clear: Callable[[], None],
        on_clear_history: Callable[[], None],
    ) -> None:
        self._on_scroll = on_scroll
        self._restore_history = restore_history
        self._on_clear = on_clear
        self._on_clear_history = on_clear_history
        self._primary_state: dict[str, Any] | None = None
        self._alternate_modes: set[int] = set()
        self._private_modes: set[int] = set()
        self._last_graphic = ""
        self._drawing = False
        self.alternate_exit_revision = 0
        self.bracketed_paste_enabled = False
        # Times a program enabled focus reporting.
        self.focus_requests = 0
        super().__init__(columns, lines)

    # The screen-wide reverse mode (DECSCNM) only changes how the whole screen
    # is displayed: cells keep their own reverse attribute.
    default_char: Any = _Cell(data=" ")

    @override
    def reset(self) -> None:
        """RIS: a full reset, which also leaves the alternate screen and every mode."""
        super().reset()
        self.cursor.attrs = self.default_char
        self.g1_charset = charsets.LAT1_MAP
        self.savepoints = []
        self._private_modes.clear()
        self.bracketed_paste_enabled = False
        if self._primary_state is not None:
            self._primary_state = None
            self._alternate_modes.clear()
            self.alternate_exit_revision += 1
        self._on_clear()

    def soft_reset(self) -> None:
        """DECSTR, as xterm does it: modes and attributes reset, the screen stays."""
        self.cursor.hidden = False
        self.margins = None
        self.cursor.attrs = self.default_char
        self.mode.difference_update({screen_modes.IRM, *(mode << 5 for mode in _SOFT_RESET_MODES)})
        self.mode.add(screen_modes.DECAWM)
        self._private_modes.difference_update(_SOFT_RESET_MODES)
        self.bracketed_paste_enabled = False
        self.charset = 0
        self.g0_charset = self.g1_charset = charsets.LAT1_MAP
        self.savepoints = [
            Savepoint(
                Cursor(0, 0, self.default_char),
                charsets.LAT1_MAP,
                charsets.LAT1_MAP,
                0,
                False,
                True,
            )
        ]

    def next_line(self) -> None:
        """NEL: the start of the next line."""
        self.carriage_return()
        self.linefeed()

    @override
    def define_charset(self, code: str, mode: str) -> None:
        # Like xterm.js, DEC Special Graphics (``0``) and US ASCII for every other set.
        chosen = charsets.VT100_MAP if code == "0" else charsets.LAT1_MAP
        if mode == "(":
            self.g0_charset = chosen
        elif mode == ")":
            self.g1_charset = chosen

    @override
    def save_cursor(self) -> None:
        # Like xterm, one saved cursor: saving again replaces it.
        self.savepoints.clear()
        super().save_cursor()

    @override
    def restore_cursor(self) -> None:
        # Like xterm, restoring keeps the saved cursor for the next restore.
        if not self.savepoints:
            super().restore_cursor()
            return
        saved = self.savepoints[-1]
        self.savepoints = [saved._replace(cursor=copy.copy(saved.cursor))]
        super().restore_cursor()
        # pyte always clamps a restored row to the scroll region, even when
        # origin mode is off and the saved cursor is outside that region.
        self.cursor.y = saved.cursor.y
        self.ensure_vbounds()
        self.savepoints = [saved]

    def _shift_saved_cursor(self, rows: int) -> None:
        """Keep the saved cursor on its line as the screen moves over history."""
        for saved in self.savepoints:
            saved.cursor.y += rows

    def save_cursor_ansi(self, *_params: int, private: bool = False) -> None:
        """CSI s: save the cursor (CSI ? s saves modes, which is not supported)."""
        if not private:
            self.save_cursor()

    def restore_cursor_ansi(self, *_params: int, private: bool = False) -> None:
        """CSI u: restore the cursor."""
        if not private:
            self.restore_cursor()

    def tab_forward(self, *params: int) -> None:
        """CHT: move forward to the next tab stop, *count* times."""
        for _ in range(min(params[0] if params and params[0] else 1, self.columns)):
            self.tab()

    def tab_backward(self, *params: int) -> None:
        """CBT: move back to the previous tab stop, *count* times."""
        for _ in range(min(params[0] if params and params[0] else 1, self.columns)):
            x = min(self.cursor.x, self.columns - 1)
            self.cursor.x = max((stop for stop in self.tabstops if stop < x), default=0)

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
            if data.isascii() and data.isprintable():
                super().draw(data)
                if data:
                    self._last_graphic = data[-1]
            else:
                self._draw_text(data)
        finally:
            self._drawing = False

    def _draw_text(self, data: str) -> None:
        """Draw text that can hold wide characters and characters taking no cell.

        pyte stops drawing at the first character taking no cell that is no
        combining mark, such as a variation selector (U+FE0F), a zero-width
        joiner or a zero-width space, and drops the rest of the text. Like a
        terminal, a zero-width character joins the character before it and
        drawing goes on; a character that cannot be printed is skipped. A
        wide character that does not fit on the row is handled as xterm
        does (``_draw_wide_at_end``); pyte would cut it into the last cell.
        """
        columns = self.columns
        autowrap = screen_modes.DECAWM in self.mode
        x = self.cursor.x
        start = 0
        for index, character in enumerate(data):
            width = wcwidth(character)
            if width > 0:
                self._last_graphic = character
                if width == 2 and (x == columns - 1 or (x >= columns and not autowrap)):
                    if start < index:
                        super().draw(data[start:index])
                    start = index + 1
                    self._draw_wide_at_end(character, autowrap=autowrap)
                    x = self.cursor.x
                    continue
                if x >= columns:
                    x = 0 if autowrap else columns - width
                x = min(x + width, columns)
                continue
            if start < index:
                super().draw(data[start:index])
            start = index + 1
            if width == 0:
                self._join_previous(character)
            x = self.cursor.x
        if start < len(data):
            super().draw(data[start:])

    def _draw_wide_at_end(self, character: str, *, autowrap: bool) -> None:
        """A wide character reaching the last column: like xterm, the last
        cell stays blank in the current attributes and the character starts
        the next row; without auto-wrap it is dropped."""
        if not autowrap:
            self.cursor.x = self.columns - 1
            return
        if self.cursor.x < self.columns:
            self.buffer[self.cursor.y][self.cursor.x] = self.cursor.attrs._replace(data=" ")
            self.dirty.add(self.cursor.y)
            self.cursor.x = self.columns
        super().draw(character)

    def _join_previous(self, mark: str) -> None:
        """Add a zero-width character to the character drawn before the cursor.

        At the start of a row that character ends the row above only when
        that row wrapped into this one; otherwise there is none, and the mark
        is dropped. A cell keeps at most ``_MAX_CELL_MARKS`` such characters.
        """
        x, y = self.cursor.x, self.cursor.y
        if x == 0:
            if y == 0 or not getattr(self.buffer[y - 1], "wrapped", False):
                return
            x, y = self.columns, y - 1
        line = self.buffer[y]
        x = min(x, self.columns) - 1
        if not line[x].data and x > 0 and _is_wide(line[x - 1].data):
            # The second cell of a wide character: join the character itself.
            x -= 1
        cell = line[x]
        if not cell.data or len(cell.data) > _MAX_CELL_MARKS:
            return
        line[x] = cell._replace(data=unicodedata.normalize("NFC", cell.data + mark))
        self.dirty.add(y)

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
        if how == _ERASE_SAVED_LINES:
            # Like xterm, ED 3 erases the saved lines and leaves the screen
            # as it is; pyte would erase the screen instead.
            self._on_clear_history()
            return
        if how in (0, 2):
            first = self.cursor.y + 1 if how == 0 else 0
            for y in range(first, self.lines):
                line: Any = self.buffer.get(y)
                if line is not None and getattr(line, "wrapped", False):
                    line.wrapped = False
        super().erase_in_display(how, *args, **kwargs)
        if how == 2 and self._primary_state is None:
            self._on_clear()

    @override
    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        # Like xterm, only a region that starts at the top row scrolls lines
        # into history; a region lower on the screen discards them.
        if self.cursor.y == bottom and top == 0 and self._primary_state is None:
            self._on_scroll(self.buffer[top])
            self._shift_saved_cursor(-1)
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
        # pyte has no dim or concealed attribute. Find 2 (dim), 22 (normal
        # intensity), 8 (conceal) and 28 (reveal) outside the operands of
        # extended colors, apply them here and pass every other attribute
        # to pyte.
        changes: dict[str, bool] = {}
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
            if attr in (2, 8):
                changes["dim" if attr == 2 else "conceal"] = True
            elif attr == 28:
                changes["conceal"] = False
            else:
                if attr == 0:
                    changes.update(dim=False, conceal=False)
                elif attr == 22:
                    changes["dim"] = False
                remaining.append(attr)
            index += 1
        if remaining or not attrs:
            super().select_graphic_rendition(*remaining)
        if changes:
            # The cursor attributes are always a _Cell (see default_char).
            self.cursor.attrs = cast(Any, self.cursor.attrs)._replace(**changes)

    @override
    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        private = kwargs.get("private")
        alternate = _ALTERNATE_SCREEN_MODES.intersection(modes) if private else set()
        if alternate and self._primary_state is None:
            # Unlike 47/1047, 1049 also replaces the saved primary cursor.
            if 1049 in alternate:
                self.save_cursor()
            self._primary_state = self._capture_state()
            super().reset()
            self.cursor.attrs = self.default_char
            # A terminal keeps its character sets on the alternate screen.
            for field_name in ("charset", "g0_charset", "g1_charset"):
                setattr(self, field_name, self._primary_state[field_name])
        if private:
            self._private_modes.update(modes)
            if _BRACKETED_PASTE_MODE in modes:
                self.bracketed_paste_enabled = True
            if _FOCUS_REPORTING_MODE in modes:
                self.focus_requests += 1
            modes = self._without_reverse_screen(modes, enable=True)
        self._alternate_modes.update(alternate)
        super().set_mode(*modes, **kwargs)

    def _without_reverse_screen(self, modes: tuple[int, ...], *, enable: bool) -> tuple[int, ...]:
        """Track DECSCNM as a flag; pyte would flip every cell's reverse attribute."""
        if _REVERSE_SCREEN_MODE not in modes:
            return modes
        if enable:
            self.mode.add(screen_modes.DECSCNM)
        else:
            self.mode.discard(screen_modes.DECSCNM)
        return tuple(mode for mode in modes if mode != _REVERSE_SCREEN_MODE)

    @override
    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        private = kwargs.get("private")
        alternate = _ALTERNATE_SCREEN_MODES.intersection(modes) if private else set()
        if private:
            self._private_modes.difference_update(modes)
            if _BRACKETED_PASTE_MODE in modes:
                self.bracketed_paste_enabled = False
            modes = self._without_reverse_screen(modes, enable=False)
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
                self._shift_saved_cursor(-dropped)
            for y in range(lines, self.lines):
                self.buffer.pop(y, None)
        elif keep_history and self.cursor.y == self.lines - 1:
            # Like xterm, grow above a cursor on the bottom row while older
            # lines remain; with rows below the cursor, grow below it instead.
            restored = self._restore_history(lines - self.lines)
            if restored:
                count = len(restored)
                rows = {y + count: line for y, line in self.buffer.items()}
                self.buffer.clear()
                self.buffer.update(rows)
                self.buffer.update(enumerate(restored))
                self.cursor.y += count
                self._shift_saved_cursor(count)
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
