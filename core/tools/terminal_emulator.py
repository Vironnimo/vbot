"""VT emulation for Terminal Sessions: rendered screen, history and replies.

The module imports only the standard library, pyte and ``_terminal_image``
(itself standard library only), so a Terminal renderer host can load it into
an isolated subinterpreter without vBot's packages
(``_terminal_render_host.py``). Keep it free of other ``core`` imports.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import re
import unicodedata
from collections import deque
from collections.abc import Callable, Generator, Mapping
from typing import Any, NamedTuple, cast, override

import pyte
from pyte import charsets, graphics
from pyte import modes as screen_modes
from pyte.screens import Cursor, Savepoint, wcwidth

from core.tools._terminal_image import paint_screen

_ALTERNATE_SCREEN_MODES = frozenset({47, 1047, 1049})
_BRACKETED_PASTE_MODE = 2004
_FOCUS_REPORTING_MODE = 1004
_REVERSE_SCREEN_MODE = 5
# The report that the terminal lost the focus: vBot's terminals never have it.
FOCUS_OUT_REPORT = "\x1b[O"
# DECCKM: cursor keys send ESC O instead of CSI while it is set.
_APPLICATION_CURSOR_MODE = 1
# Private modes a soft reset (DECSTR) turns off, as xterm does: cursor keys,
# origin, keypad, reverse wrap, focus reports, bracketed paste.
_SOFT_RESET_MODES = frozenset({1, 6, 45, 66, _FOCUS_REPORTING_MODE, _BRACKETED_PASTE_MODE})
TERMINAL_TITLE_MAX_CHARS = 160
# A repeat request (REP) draws at most one screen of characters.
_MAX_REPEAT_SCREENS = 1
# Zero-width characters (combining marks, joiners) one cell keeps at most.
_MAX_CELL_MARKS = 16
# Output the parser reads character by character: C0 controls, DEL and C1
# controls. Everything else is drawn in runs.
_PLAIN_TEXT_RUN = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")
# What ends an OSC payload, and what ends a DCS, SOS, PM or APC string.
_OSC_END = re.compile(r"[\x07\x18\x1a\x1b\x80-\x9f]")
_STRING_END = re.compile(r"[\x18\x1a\x1b\x9c]")
# The C1 controls that open a CSI, an OSC, a DCS and an ignored string.
_CSI_C1 = "\x9b"
_OSC_C1 = "\x9d"
_STRING_C1 = frozenset("\x90\x98\x9e\x9f")
_STRING_ESCAPES = {"P": "\x90", "X": "\x98", "^": "\x9e", "_": "\x9f"}
# CSI parameters beyond this many are ignored; values are capped like pyte's.
_MAX_CSI_PARAMETERS = 32
_MAX_CSI_VALUE = 9999
# An OSC payload is kept up to this many characters (a title needs far fewer).
_OSC_PAYLOAD_MAX_CHARS = 4096
# What the signal scanner looks for between control strings: a whole CSI
# (read in one step), a bell, or the start of another escape sequence.
_SCAN_GROUND = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])|\x07|\x1b")
_SCAN_CSI_END = re.compile(r"[@-~\x1b\x18\x1a]")
_SCAN_CSI_PARAMETERS = re.compile(r"[0-?]*")
# A CSI split across chunks is kept up to this many characters; longer ones
# set no mode the scanner reads.
_CSI_PREFIX_CHARS = 32
_SCAN_OSC_END = re.compile(r"[\x07\x1b\x18\x1a]")
_SCAN_STRING_END = re.compile(r"[\x1b\x18\x1a]")
# ConEmu's numbered OSC 9 commands; any other OSC 9 text is a notification.
_OSC9_COMMAND = re.compile(r"\d+(?:;|$)")
# What the Agent's screen text puts around text shown in reverse video, such
# as a selection or a drawn cursor: characters programs practically never print.
HIGHLIGHT_START = "⟦"
HIGHLIGHT_END = "⟧"
# ... and around faint text right of the visible cursor on its row: a
# suggestion such as PSReadLine's prediction or a placeholder, not input.
SUGGESTION_START = "⟪"
SUGGESTION_END = "⟫"
# Erase in Display mode 3: forget the saved lines (scrollback), keep the screen.
_ERASE_SAVED_LINES = 3
# An OSC payload is classified by its start; longer payloads (images,
# clipboard data) are not kept.
_OSC_PREFIX_CHARS = 64
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
    """pyte's character cell plus the dim (faint) and concealed attributes pyte lacks."""

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
    conceal: bool = False


class EmulatorUpdate(NamedTuple):
    """What one fed chunk changed that the Terminal Session acts on."""

    # Replies to terminal queries, to write back to the program.
    responses: str
    title: str
    bracketed_paste: bool
    alternate_screen: bool
    # The chunk left the alternate screen (the primary screen came back).
    alternate_exited: bool
    # What the program declared about its work in this chunk, in order (see
    # ``SignalScanner``).
    signals: tuple[str, ...] = ()
    # The program asked for focus reports (CSI ? 1004 h) and still wants them.
    focus_reporting: bool = False
    # The chunk erased the whole primary screen: the history line number its
    # first row had then, which the next lines drawn there get again.
    cleared_line: int | None = None
    # Screen operations or sequences of this chunk that failed and were
    # skipped, with the first failure's description (see ``_GuardedScreen``).
    skipped: int = 0
    skipped_error: str = ""
    # Cursor keys send their application form (DECCKM, CSI ? 1 h).
    application_cursor: bool = False


class ScreenObservation(NamedTuple):
    """The visible screen at one moment, as the Agent is shown it.

    ``screen`` is every row through the last non-blank one: reverse-video
    text between ``HIGHLIGHT_START`` and ``HIGHLIGHT_END``, faint text right
    of the visible cursor on its row between ``SUGGESTION_START`` and
    ``SUGGESTION_END``, concealed text (SGR 8) blank. ``cursor`` is the
    visible cursor's row and column, counted from 1 (None while the program
    hides it). ``screen_start_line`` numbers the first screen row as a history
    line; ``first_line`` is the oldest retained history line. ``cursor_line``
    is the history line of the primary screen's cursor row, hidden or not:
    where the program's next line of output goes.
    """

    signature: str
    screen: str
    cursor: tuple[int, int] | None
    screen_start_line: int
    first_line: int
    cursor_line: int


class SignalScanner:
    """Find what a program declares about its work in its output, across chunks.

    Signals, by the standard terminal sequences that carry them:

    - ``attention``: the program asks for the user: a bell outside control
      strings, or a desktop notification (OSC 9 with text, OSC 777;notify,
      OSC 99).
    - ``progress`` / ``progress_end``: a progress indicator (OSC 9;4) shows
      work (state 1 or 3), or is cleared, failed or paused (state 0, 2 or 4).
    - ``prompt``: a shell shows its prompt or ended a command (OSC 133/633 A
      or D, OSC 9;12), or a line editor starts reading input (it turns
      bracketed paste on, CSI ? 2004 h).
    - ``command``: a shell starts running a command line (OSC 133/633 C), or
      a line editor stops reading input (it turns bracketed paste off).

    Line editors (readline, zle, fish, prompt_toolkit, Python's REPL) turn
    bracketed paste on while they read a line and off while it runs; only a
    change of the mode is a signal, as full-screen programs set it once.
    A sequence split across chunks is recognized once it completes.
    """

    def __init__(self) -> None:
        self._state = "ground"
        self._payload: list[str] = []
        self._payload_chars = 0
        self._csi: list[str] = []
        self._csi_chars = 0
        self._bracketed_paste = False

    def scan(self, text: str) -> list[str]:
        signals: list[str] = []
        index = 0
        length = len(text)
        while index < length:
            state = self._state
            if state == "ground":
                match = _SCAN_GROUND.search(text, index)
                if match is None:
                    break
                index = match.end()
                token = match.group()
                if token == "\x07":
                    signals.append("attention")
                elif token == "\x1b":
                    self._state = "escape"
                else:
                    self._dispatch_csi(match.group(1), match.group(2), signals)
            elif state == "escape":
                character = text[index]
                index += 1
                if character == "]":
                    self._state = "osc"
                    self._payload = []
                    self._payload_chars = 0
                elif character == "[":
                    self._state = "csi"
                    self._csi = []
                    self._csi_chars = 0
                elif character in "P^_X":
                    self._state = "string"
                elif " " <= character <= "/":
                    self._state = "escape_intermediate"
                elif character != "\x1b":
                    self._state = "ground"
            elif state == "escape_intermediate":
                character = text[index]
                index += 1
                if character == "\x1b":
                    self._state = "escape"
                elif not " " <= character <= "/":
                    self._state = "ground"
            elif state == "csi":
                match = _SCAN_CSI_END.search(text, index)
                end = length if match is None else match.start()
                if self._csi_chars < _CSI_PREFIX_CHARS:
                    part = text[index : min(end, index + _CSI_PREFIX_CHARS - self._csi_chars)]
                    self._csi.append(part)
                    self._csi_chars += len(part)
                if match is None:
                    break
                index = match.end()
                final = match.group()
                if final == "\x1b":
                    self._state = "escape"
                    continue
                self._state = "ground"
                if final not in "\x18\x1a" and self._csi_chars < _CSI_PREFIX_CHARS:
                    parameters = _SCAN_CSI_PARAMETERS.match("".join(self._csi))
                    self._dispatch_csi(parameters.group() if parameters else "", final, signals)
            elif state == "osc":
                match = _SCAN_OSC_END.search(text, index)
                end = length if match is None else match.start()
                if self._payload_chars < _OSC_PREFIX_CHARS:
                    part = text[index : min(end, index + _OSC_PREFIX_CHARS - self._payload_chars)]
                    self._payload.append(part)
                    self._payload_chars += len(part)
                if match is None:
                    break
                index = match.end()
                terminator = match.group()
                if terminator == "\x07":
                    self._dispatch(signals)
                    self._state = "ground"
                elif terminator == "\x1b":
                    self._state = "osc_escape"
                else:
                    self._state = "ground"
            elif state == "osc_escape":
                if text[index] == "\\":
                    index += 1
                    self._dispatch(signals)
                    self._state = "ground"
                else:
                    # The escape starts another sequence and cancels the OSC.
                    self._state = "escape"
            elif state == "string":
                match = _SCAN_STRING_END.search(text, index)
                if match is None:
                    break
                index = match.end()
                self._state = "string_escape" if match.group() == "\x1b" else "ground"
            else:  # string_escape
                if text[index] == "\\":
                    index += 1
                    self._state = "ground"
                else:
                    # Another escape sequence inside the string (tmux passthrough
                    # doubles them) is read as one.
                    self._state = "escape"
        return signals

    def _dispatch(self, signals: list[str]) -> None:
        signal = _osc_signal("".join(self._payload))
        if signal is not None:
            signals.append(signal)

    def _dispatch_csi(self, parameters: str, final: str, signals: list[str]) -> None:
        if final not in "hl" or not parameters.startswith("?"):
            return
        if str(_BRACKETED_PASTE_MODE) not in parameters[1:].split(";"):
            return
        enabled = final == "h"
        if enabled != self._bracketed_paste:
            self._bracketed_paste = enabled
            signals.append("prompt" if enabled else "command")


def _osc_signal(payload: str) -> str | None:
    """The signal an OSC payload carries, if any."""
    head, _, rest = payload.partition(";")
    if head == "9":
        if rest.startswith("4;"):
            state = rest[2:].partition(";")[0]
            if state in {"1", "3"}:
                return "progress"
            return "progress_end" if state in {"0", "2", "4"} else None
        if rest == "12" or rest.startswith("12;"):
            return "prompt"
        if _OSC9_COMMAND.match(rest) or not rest.strip():
            return None
        return "attention"
    if head == "777":
        return "attention" if rest == "notify" or rest.startswith("notify;") else None
    if head == "99":
        return "attention"
    if head in {"133", "633"}:
        mark = rest[:1]
        if mark in {"A", "D"}:
            return "prompt"
        if mark == "C":
            return "command"
    return None


QueryResponder = Callable[[str, tuple[int, ...], str, str], None]


class _EmulatorStream(pyte.Stream):
    """pyte's stream with a parser that reads output as xterm does.

    pyte's own parser raises on parameters it cannot convert (``CSI ² m``, a
    4,400-digit value), draws the payload of DCS, SOS, PM and APC strings
    and the final of sequences with intermediates (``CSI ! p``), reads OSC
    codes by their first character only, and ignores character sets.
    This parser follows xterm's state machine instead: CSI parameters
    with colon sub-parameters, prefixes and intermediates; OSC payloads by
    their numeric code, of which only titles (0, 1, 2) are kept; DCS, SOS,
    PM and APC strings skipped; C1 controls; CAN and SUB abort a sequence,
    and ESC ends one and starts the next. The device queries go to
    *respond*. A sequence the parser cannot handle is skipped and reported
    to *on_error*; the parser starts over on the next character.
    """

    csi = {
        **pyte.Stream.csi,
        "S": "scroll_up",
        "T": "scroll_down",
        "b": "repeat_character",
        "s": "save_cursor_ansi",
        "u": "restore_cursor_ansi",
        "I": "tab_forward",
        "Z": "tab_backward",
        "`": "cursor_to_column",
    }

    def __init__(
        self,
        screen: Any,
        *,
        respond: QueryResponder,
        on_error: Callable[[str, Exception], None],
    ) -> None:
        self._respond = respond
        self._on_error = on_error
        super().__init__(screen)

    @override
    def feed(self, data: str) -> None:
        listener = self.listener
        if listener is None:
            raise RuntimeError("Listener is not set")
        draw = listener.draw
        match_text = _PLAIN_TEXT_RUN.match
        parser = self._parser
        assert parser is not None
        # True: plain text is drawn directly; None: the parser takes the next
        # character; a pattern: it takes the text up to the pattern's next match.
        state: Any = self._taking_plain_text
        length = len(data)
        offset = 0
        while offset < length:
            if state is True:
                match = match_text(data, offset)
                if match is not None:
                    end = match.end()
                    draw(data[offset:end])
                    offset = end
                    continue
                part = data[offset]
                offset += 1
            elif state is None:
                part = data[offset]
                offset += 1
            else:
                match = state.search(data, offset)
                end = length if match is None else match.start()
                if end == offset:
                    end += 1
                part = data[offset:end]
                offset = end
            try:
                state = parser.send(part)
            except Exception as error:
                self._on_error("parser", error)
                self._initialize_parser()
                parser = self._parser
                assert parser is not None
                state = self._taking_plain_text
        self._taking_plain_text = state

    def reset_parser(self) -> None:
        """Start reading again as if at the start of plain text."""
        self._initialize_parser()

    @override
    def _parser_fsm(self) -> Generator[Any, str]:  # noqa: C901, PLR0912, PLR0915
        listener: Any = self.listener
        assert listener is not None
        draw = listener.draw
        controls: dict[str, Callable[[], Any]] = {
            "\x07": listener.bell,
            "\x08": listener.backspace,
            "\t": listener.tab,
            "\n": listener.linefeed,
            "\x0b": listener.linefeed,
            "\x0c": listener.linefeed,
            "\r": listener.carriage_return,
            "\x0e": listener.shift_out,
            "\x0f": listener.shift_in,
        }
        c1_controls: dict[str, Callable[[], Any]] = {
            "\x84": listener.index,
            "\x85": listener.next_line,
            "\x88": listener.set_tab_stop,
        }
        escapes: dict[str, Callable[[], Any]] = {
            "c": listener.reset,
            "D": listener.index,
            "E": listener.next_line,
            "M": listener.reverse_index,
            "H": listener.set_tab_stop,
            "7": listener.save_cursor,
            "8": listener.restore_cursor,
        }
        csi_dispatch = {final: getattr(listener, name) for final, name in self.csi.items()}
        private_dispatch = {
            "h": listener.set_mode,
            "l": listener.reset_mode,
            "J": listener.erase_in_display,
            "K": listener.erase_in_line,
        }
        respond = self._respond
        pending: str | None = None
        while True:
            if pending is None:
                char = yield True
            else:
                char, pending = pending, None
            if char == "\x1b":
                char = yield None
                while char == "\x1b" or char in controls:
                    if char != "\x1b":
                        controls[char]()
                    char = yield None
                if char == "[":
                    char = _CSI_C1
                elif char == "]":
                    char = _OSC_C1
                elif char in _STRING_ESCAPES:
                    char = _STRING_ESCAPES[char]
                elif " " <= char <= "/":
                    intermediates = char
                    while True:
                        char = yield None
                        if " " <= char <= "/":
                            intermediates = (intermediates + char)[:4]
                        elif "0" <= char <= "~":
                            if intermediates in ("(", ")"):
                                listener.define_charset(char, intermediates)
                            elif intermediates == "#" and char == "8":
                                listener.alignment_display()
                            break
                        elif char in controls:
                            controls[char]()
                        elif char == "\x1b" or char >= "\x80":
                            pending = char
                            break
                        elif char in "\x18\x1a":
                            break
                    continue
                elif "0" <= char <= "~":
                    handler = escapes.get(char)
                    if handler is not None:
                        handler()
                    continue
                elif char in "\x18\x1a\x7f" or char < " ":
                    continue
                else:
                    # A C1 control or a printable character after ESC.
                    pending = char
                    continue
            if char == _CSI_C1:
                prefix = ""
                intermediates = ""
                groups: list[list[int]] = []
                values: list[int] = []
                value = -1
                valid = True
                while True:
                    char = yield None
                    if "0" <= char <= "9":
                        if intermediates:
                            valid = False
                        value = min(max(value, 0) * 10 + ord(char) - 48, 99999)
                    elif char == ";" or char == ":":
                        if intermediates:
                            valid = False
                        values.append(max(value, 0))
                        value = -1
                        if char == ";":
                            if len(groups) < _MAX_CSI_PARAMETERS:
                                groups.append(values)
                            values = []
                    elif "<" <= char <= "?":
                        if prefix or groups or values or value >= 0 or intermediates:
                            valid = False
                        else:
                            prefix = char
                    elif " " <= char <= "/":
                        intermediates = (intermediates + char)[:4]
                    elif "@" <= char <= "~":
                        if valid:
                            values.append(max(value, 0))
                            if len(groups) < _MAX_CSI_PARAMETERS:
                                groups.append(values)
                            self._dispatch_csi(
                                (prefix, intermediates, char),
                                groups,
                                listener,
                                csi_dispatch,
                                private_dispatch,
                                respond,
                            )
                        break
                    elif char in controls:
                        controls[char]()
                    elif char == "\x1b" or "\x80" <= char <= "\x9f":
                        pending = char
                        break
                    elif char in "\x18\x1a" or char >= "\xa0":
                        # CAN and SUB abort the sequence; so does a character
                        # no sequence can hold, which is not drawn either.
                        break
                continue
            if char == _OSC_C1:
                payload: list[str] = []
                size = 0
                while True:
                    part = yield _OSC_END
                    if part == "\x07" or part == "\x9c":
                        self._dispatch_osc("".join(payload), listener)
                        break
                    if part == "\x1b":
                        # ESC ends the OSC and starts the next sequence (ST is ESC \).
                        self._dispatch_osc("".join(payload), listener)
                        pending = part
                        break
                    if part == "\x18" or part == "\x1a":
                        break
                    if len(part) == 1 and "\x80" <= part <= "\x9f":
                        pending = part
                        break
                    if size < _OSC_PAYLOAD_MAX_CHARS:
                        payload.append(part[: _OSC_PAYLOAD_MAX_CHARS - size])
                        size += len(payload[-1])
                continue
            if char in _STRING_C1:
                # DCS, SOS, PM and APC strings carry nothing a screen shows.
                while True:
                    part = yield _STRING_END
                    if part == "\x1b":
                        pending = part
                        break
                    if part in ("\x9c", "\x18", "\x1a"):
                        break
                continue
            handler = controls.get(char) or c1_controls.get(char)
            if handler is not None:
                handler()
            elif char >= " " and not "\x7f" <= char <= "\x9f":
                draw(char)

    @staticmethod
    def _dispatch_csi(
        sequence: tuple[str, str, str],
        groups: list[list[int]],
        listener: Any,
        csi_dispatch: Mapping[str, Callable[..., Any]],
        private_dispatch: Mapping[str, Callable[..., Any]],
        respond: QueryResponder,
    ) -> None:
        prefix, intermediates, final = sequence
        params = tuple(min(values[0], _MAX_CSI_VALUE) for values in groups)
        if intermediates:
            if intermediates == "!" and final == "p" and not prefix:
                listener.soft_reset()
            elif intermediates == "$" and final == "p":
                respond(prefix, params, intermediates, final)
            return
        if final in "nct":
            respond(prefix, params, intermediates, final)
            return
        if prefix == "?":
            handler = private_dispatch.get(final)
            if handler is not None:
                handler(*params, private=True)
            return
        if prefix:
            # Keyboard protocol and other xterm extensions (CSI > 4 ; 1 m,
            # CSI < u, CSI = 1 u) that a screen does not show.
            return
        if final == "m" and any(len(values) > 1 for values in groups):
            params = tuple(min(value, _MAX_CSI_VALUE) for value in _sgr_attributes(groups))
            if not params:
                return
        handler = csi_dispatch.get(final)
        if handler is not None:
            handler(*params)

    @staticmethod
    def _dispatch_osc(payload: str, listener: Any) -> None:
        code, _, text = payload.partition(";")
        if code in ("0", "1"):
            listener.set_icon_name(text)
        if code in ("0", "2"):
            listener.set_title(text)


class _GuardedScreen:
    """The screen as the parser calls it: an operation that fails is skipped.

    A terminal ignores a sequence it cannot apply. An error that left the
    parser would end the parser, and the screen would never change again;
    skipped instead, it costs only that operation's effect, and the screen
    goes on following the output.
    """

    def __init__(self, screen: pyte.Screen) -> None:
        self._screen = screen
        self.skipped = 0
        self.skipped_error = ""

    def record(self, name: str, error: Exception) -> None:
        """Count one skipped operation, describing the first of a chunk."""
        if not self.skipped:
            self.skipped_error = f"{name}: {type(error).__name__}: {error}"[:500]
        self.skipped += 1

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._screen, name)
        if not callable(value):
            return value

        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return value(*args, **kwargs)
            except Exception as error:
                self.record(name, error)
                return None

        return guarded


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

    def screenshot(self) -> bytes:
        """The screen as a PNG image with its colors, text styles and visible cursor."""
        screen = self._screen
        cursor = screen.cursor
        return paint_screen(
            [
                _visible_cells(screen.buffer[row], self.columns, conceal=False)
                for row in range(self.rows)
            ],
            columns=self.columns,
            cursor=None if cursor.hidden else (cursor.y, min(cursor.x, self.columns - 1)),
            reverse_screen=screen_modes.DECSCNM in screen.mode,
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


def _sgr_attributes(groups: list[list[int]]) -> list[int]:
    """SGR attributes with colon sub-parameters in the semicolon form pyte reads."""
    attributes: list[int] = []
    for values in groups:
        head = values[0]
        if len(values) == 1:
            attributes.append(head)
        elif head in (38, 48):
            if values[1] == 5 and len(values) >= 3:
                attributes.extend((head, 5, values[2]))
            elif values[1] == 2 and len(values) >= 5:
                # 38:2:<colorspace>:R:G:B or 38:2:R:G:B
                attributes.extend((head, 2, *values[-3:]))
        elif head == 4:
            attributes.append(24 if values[1] == 0 else 4)
        elif head != 58:  # 58: underline color, not rendered
            attributes.append(head)
    return attributes


def _is_wide(data: str) -> bool:
    """Whether a cell's character takes two cells."""
    return bool(data) and not data.isascii() and wcwidth(data[0]) == 2


def _visible_cells(line: Any, columns: int, *, conceal: bool) -> list[tuple[int, Any, str]]:
    """Each cell of a row a viewer sees, with its column and the text it shows.

    pyte keeps a wide character's second cell as an empty one, skipped here.
    Like a real terminal, a wide character a narrow one half overwrote is
    gone: both of its cells show a space instead of pyte's broken pair.
    With *conceal*, concealed cells (SGR 8) show blanks, as on a screen.
    """
    cells: list[tuple[int, Any, str]] = []
    wide = False
    for column in range(columns):
        cell = line[column]
        data = cell.data
        if wide:
            wide = False
            if not data:
                continue
            previous_column, previous, _text = cells[-1]
            cells[-1] = (previous_column, previous, " ")
        if not data:
            cells.append((column, cell, " "))
            continue
        wide = not data.isascii() and wcwidth(data[0]) == 2
        if conceal and getattr(cell, "conceal", False):
            data = "  " if wide else " "
        cells.append((column, cell, data))
    return cells


def _render_buffer_line(line: Any, columns: int, *, strip: bool = True) -> str:
    if strip:
        # Cells never written are blank and would be stripped anyway.
        columns = min(columns, max(line.keys(), default=-1) + 1)
    text = "".join(text for _column, _cell, text in _visible_cells(line, columns, conceal=True))
    return text.rstrip() if strip else text


def _marked_row(line: Any, columns: int, *, suggestion_from: int | None) -> str:
    """A row's text as the Agent reads it, with marks around what it cannot see as text.

    Reverse-video runs stand between the highlight marks. From column
    *suggestion_from* on (the visible cursor's, then zero on its wrapped rows), faint text
    stands between the suggestion marks: a prediction or placeholder that
    Enter does not submit. Blank cells at the end of the row are left out,
    also inside a highlighted run, which keeps at least one.
    """
    # Runs of (suggestion, highlighted, text).
    runs: list[tuple[bool, bool, str]] = []
    for column, cell, text in _visible_cells(line, columns, conceal=True):
        highlighted = bool(cell.reverse)
        suggestion = suggestion_from is not None and column >= suggestion_from and cell.dim
        if runs and runs[-1][:2] == (suggestion, highlighted):
            runs[-1] = (suggestion, highlighted, runs[-1][2] + text)
        else:
            runs.append((suggestion, highlighted, text))
    # Blank faint text shows nothing to mark.
    runs = [
        (suggestion and bool(text.strip()), highlighted, text)
        for suggestion, highlighted, text in runs
    ]
    while runs and not runs[-1][1] and not runs[-1][2].strip():
        runs.pop()
    if not runs:
        return ""
    suggestion, highlighted, text = runs[-1]
    runs[-1] = (suggestion, highlighted, text.rstrip() or " ")
    parts: list[str] = []
    in_suggestion = False
    for suggestion, highlighted, text in runs:
        if suggestion != in_suggestion:
            parts.append(SUGGESTION_START if suggestion else SUGGESTION_END)
            in_suggestion = suggestion
        parts.append(f"{HIGHLIGHT_START}{text}{HIGHLIGHT_END}" if highlighted else text)
    if in_suggestion:
        parts.append(SUGGESTION_END)
    return "".join(parts)


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
        getattr(cell, "conceal", False),
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
    if getattr(cell, "conceal", False):
        codes.append(8)
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
