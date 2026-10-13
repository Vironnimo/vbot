"""Terminal input encoding: exact data, typed text and named keys."""

from __future__ import annotations

from ._terminal_state import (
    TERMINAL_BRACKETED_PASTE_END,
    TERMINAL_BRACKETED_PASTE_START,
    TERMINAL_INPUT_KEY_SEQUENCES,
    TERMINAL_INPUT_MAX_CHARS,
)


def input_chunks(
    *,
    data: str | None,
    text: str | None,
    key: str | None,
    bracketed_paste: bool = False,
) -> tuple[str, ...]:
    """The writes for one input: exact *data*, or *text* followed by *key*.

    Text is typed as on a keyboard, where each line break is the Enter key;
    with *bracketed_paste* it is pasted as one block instead, its line breaks
    sent as a terminal pastes them.
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
        chunks.append(TERMINAL_INPUT_KEY_SEQUENCES[key])
    return tuple(chunks)


def _typed(text: str) -> str:
    """Text as a terminal sends it: every line break (CR LF, LF or CR) is CR.

    A terminal sends CR for Enter on every platform, and pastes line breaks
    as CR too; a program reading a raw terminal takes a lone LF for Ctrl-J,
    not for Enter.
    """
    return text.replace("\r\n", "\r").replace("\n", "\r")
