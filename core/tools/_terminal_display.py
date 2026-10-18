"""Operator display metadata for Terminal Tool calls and results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from core.tools._terminal_arguments import (
    normalize_terminal_arguments,
)
from core.tools.tools import (
    JsonObject,
    ToolDisplayPart,
    display_notice,
    display_results,
    display_text,
)

if TYPE_CHECKING:
    pass

from ._terminal_definition import TERMINAL_ACTIONS

# Display

_ATTACHMENT_TEXT = {
    "here": "attached here",
    "other": "attached to another conversation",
    "no": "not attached",
}


def _attachment_text(item: Mapping[str, Any]) -> str:
    """Who holds a listed terminal, as the user reads it."""
    subagent = item.get("subagent")
    if item.get("attached") == "subagent" and isinstance(subagent, str) and subagent:
        return f"attached to Sub-Agent {subagent}"
    return _ATTACHMENT_TEXT.get(str(item.get("attached")), "")


def _visible_input(data: str) -> str:
    """Spell out control characters, such as Escape, that exact input data can hold."""
    return "".join(
        char
        if char in "\n\t" or (char >= " " and char != "\x7f")
        else char.encode("unicode_escape").decode("ascii")
        for char in data
    )


def _typed_input(arguments: JsonObject) -> str:
    """Return what an input call types, with a named key shown as ``<key>``."""
    data = arguments.get("data")
    if isinstance(data, str) and data:
        return _visible_input(data)
    text = arguments.get("text")
    key = arguments.get("key")
    typed = text if isinstance(text, str) else ""
    if isinstance(key, str) and key:
        typed = f"{typed} <{key}>" if typed else f"<{key}>"
    return typed


def _terminal_state_text(item: Mapping[str, Any]) -> str:
    state = item.get("state")
    exit_code = item.get("exit_code")
    if state == "exited" and isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return f"exited with code {exit_code}"
    return str(state or "")


def _terminal_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    """Show the user what the Agent typed, the terminal's screen and how its program ended.

    Paging requests and notes are for the Agent and stay in the raw result.
    """
    try:
        normalized = normalize_terminal_arguments(arguments)
    except ValueError:
        normalized = arguments
    call = normalized if isinstance(normalized, dict) else {}
    action = call.get("action")
    blocks: list[JsonObject] = []
    typed = _typed_input(call) if action == "input" else ""
    if typed.strip():
        blocks.append(display_text("input", text=typed))
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return blocks
    terminals = data.get("terminals")
    if isinstance(terminals, list):
        listed = [item for item in terminals if isinstance(item, dict)]
        if not listed:
            return [*blocks, display_notice("info", "No terminal is open.")]
        items = [
            {
                "title": " · ".join(
                    str(part)
                    for part in (
                        item.get("call_name"),
                        item.get("program") or item.get("terminal_id"),
                    )
                    if part
                ),
                "meta": " · ".join(
                    part for part in (_terminal_state_text(item), _attachment_text(item)) if part
                ),
            }
            for item in listed
        ]
        return [*blocks, display_results(items)]
    if "history" in data:
        blocks.append(display_text("scrollback", source="result", path=("data", "history")))
    if "screen" in data:
        blocks.append(display_text("screen", source="result", path=("data", "screen")))
    state = data.get("state")
    exit_code = data.get("exit_code")
    if state == "stopped":
        blocks.append(display_notice("info", "The program was stopped."))
    elif state == "failed":
        blocks.append(display_notice("warning", "The terminal stopped with an error."))
    elif state == "exited" and isinstance(exit_code, int) and exit_code != 0:
        blocks.append(display_notice("warning", f"The program exited with code {exit_code}."))
    elif state == "exited":
        blocks.append(display_notice("info", "The program exited."))
    elif data.get("wait_ended") == "timeout":
        blocks.append(display_notice("info", "The program was still running when the wait ended."))
    return blocks


def _terminal_display_parts(arguments: JsonObject) -> tuple[ToolDisplayPart, ...]:
    try:
        normalized = normalize_terminal_arguments(arguments)
    except ValueError:
        normalized = arguments
    if not isinstance(normalized, dict):
        return ()
    arguments = normalized
    action = arguments.get("action")
    if not isinstance(action, str) or action not in TERMINAL_ACTIONS:
        return ()
    parts = [ToolDisplayPart(action, truncate="never", tooltip="none")]
    terminal_id = arguments.get("terminal_id")
    if isinstance(terminal_id, str) and terminal_id:
        parts.append(ToolDisplayPart(terminal_id, kind="identifier", truncate="middle"))
        size = [str(arguments[field]) for field in ("columns", "rows") if field in arguments]
        if action == "resize" and len(size) == 2:
            parts.append(ToolDisplayPart("×".join(size)))
        return tuple(parts)
    command = arguments.get("command")
    if action == "start":
        if isinstance(command, str) and command:
            parts.append(ToolDisplayPart(command, kind="command", copyable=True))
        else:
            parts.append(ToolDisplayPart("default shell", kind="command"))
    return tuple(parts)
