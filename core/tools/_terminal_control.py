"""Bounded VT control-string parser and activity signal scanner."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from typing import Any, override

import pyte

from ._terminal_cells import _sgr_attributes
from ._terminal_vt_types import (
    _BRACKETED_PASTE_MODE,
    _CSI_C1,
    _CSI_PREFIX_CHARS,
    _MAX_CSI_PARAMETERS,
    _MAX_CSI_VALUE,
    _OSC9_COMMAND,
    _OSC_C1,
    _OSC_END,
    _OSC_PAYLOAD_MAX_CHARS,
    _OSC_PREFIX_CHARS,
    _PLAIN_TEXT_RUN,
    _SCAN_CSI_END,
    _SCAN_CSI_PARAMETERS,
    _SCAN_GROUND,
    _SCAN_OSC_END,
    _SCAN_STRING_END,
    _STRING_C1,
    _STRING_END,
    _STRING_ESCAPES,
    QueryResponder,
)


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
