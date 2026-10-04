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
        chunks.append(
            f"{TERMINAL_BRACKETED_PASTE_START}{text}{TERMINAL_BRACKETED_PASTE_END}"
            if bracketed_paste
            else text
        )
    if key is not None:
        chunks.append(TERMINAL_INPUT_KEY_SEQUENCES[key])
    return tuple(chunks)
