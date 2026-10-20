"""Terminal input encoding: exact data, typed text and named keys."""

from __future__ import annotations

import re

from ._terminal_state import (
    TERMINAL_BRACKETED_PASTE_END,
    TERMINAL_BRACKETED_PASTE_START,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
)

# The cursor keys while the program set application cursor mode (DECCKM),
# as xterm sends them: SS3 instead of CSI.
_APPLICATION_CURSOR_KEYS = {
    "up": "\x1bOA",
    "down": "\x1bOB",
    "right": "\x1bOC",
    "left": "\x1bOD",
    "home": "\x1bOH",
    "end": "\x1bOF",
}

# SGR mouse reports (modes 1006 and 1016), the encodings xterm.js delivers as text.
_SGR_MOUSE_REPORT = re.compile(r"\x1b\[<(\d+);\d+;\d+[Mm]")
_MOUSE_MOTION = 32
_MOUSE_NO_BUTTON = 3
_MOUSE_WHEEL = 64


def is_pointer_motion(data: str) -> bool:
    """Whether *data* only reports the mouse moving with no button held.

    A program that tracks every mouse event receives these while a user's
    pointer merely passes over its terminal.
    """
    end = 0
    for report in _SGR_MOUSE_REPORT.finditer(data):
        button = int(report.group(1))
        if (
            report.start() != end
            or not button & _MOUSE_MOTION
            or button & _MOUSE_WHEEL
            or button & 3 != _MOUSE_NO_BUTTON
        ):
            return False
        end = report.end()
    return end > 0 and end == len(data)


def input_chunks(
    *,
    data: str | None,
    text: str | None,
    key: str | None,
    bracketed_paste: bool = False,
    application_cursor: bool = False,
) -> tuple[str, ...]:
    """The writes for one input: exact *data*, or *text* followed by *key*.

    Text is typed as on a keyboard, where each line break is the Enter key;
    with *bracketed_paste* it is pasted as one block instead, its line breaks
    sent as a terminal pastes them. With *application_cursor*, the cursor
    keys send what a terminal sends in that mode.
    """
    if data == "":
        data = None
    if text == "":
        text = None
    if key == "":
        key = None
    if data is not None:
        if text is not None or key is not None:
            raise ValueError("data cannot be combined with text or key")
        if not data:
            raise ValueError("data must be a non-empty string")
        if len(data) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"data must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        return (data,)
    if key is not None and key not in TERMINAL_INPUT_KEY_SEQUENCES:
        raise ValueError(f"Unsupported terminal key: {key}")
    chunks: list[str] = []
    if text:
        if len(text) > TERMINAL_INPUT_MAX_CHARS:
            raise ValueError(f"text must not exceed {TERMINAL_INPUT_MAX_CHARS} characters")
        typed = _typed(text)
        chunks.append(
            f"{TERMINAL_BRACKETED_PASTE_START}{typed}{TERMINAL_BRACKETED_PASTE_END}"
            if bracketed_paste
            else typed
        )
    if key is not None:
        sequence = TERMINAL_INPUT_KEY_SEQUENCES[key]
        if application_cursor:
            sequence = _APPLICATION_CURSOR_KEYS.get(key, sequence)
        chunks.append(sequence)
    return tuple(chunks)


def _typed(text: str) -> str:
    """Text as a terminal sends it: every line break (CR LF, LF or CR) is CR.

    A terminal sends CR for Enter on every platform, and pastes line breaks
    as CR too; a program reading a raw terminal takes a lone LF for Ctrl-J,
    not for Enter.
    """
    return text.replace("\r\n", "\r").replace("\n", "\r")
