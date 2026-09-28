"""Prompt-epoch Tool catalog: Tool-change notes and their Model-facing text.

Within one prompt epoch a Session sends the same Tool definitions on every
request, so the Provider prompt cache survives Tool changes. A change reaches
the Model as a ``[tool-change]`` note instead: a System Reminder that names the
Tool and, when the Model cannot read it from its Tool list, carries its
definition. The note stores a JSON payload, not text, so the dispatch
allowlist and contracts can be rebuilt from Session history, and the wording is
rendered at request time with the Tool's Model-facing name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from core.chat.messages import ChatMessage, JsonObject
from core.sessions import TOOL_CHANGE_NOTE_PREFIX, is_tool_change_note
from core.tools import model_tool_name

TOOL_CHANGE_NOTE_VERSION = 1

ToolChangeKind = Literal["added", "removed", "changed"]
_TOOL_CHANGE_KINDS: frozenset[str] = frozenset({"added", "removed", "changed"})

_ADDED_UNLISTED = (
    "The Tool {name} was enabled for you in this Session. Your Tool list does not show it "
    "because the list stays unchanged until the conversation is compacted. Call {name} by "
    "name with a normal Tool call.\nDescription: {description}\n"
    "Parameters (JSON Schema): {schema}"
)
_ADDED_LISTED = (
    "The Tool {name} was enabled for you in this Session and now appears in your Tool list."
)
_AVAILABLE_AGAIN = "The Tool {name} is available to you again."
_REMOVED = (
    "The Tool {name} was removed from your Tools in this Session. Calls to {name} fail; "
    "use your other Tools instead."
)
_CHANGED = (
    "The Tool {name} changed in this Session. Your Tool list still shows its previous "
    "definition until the conversation is compacted; call it with this definition instead."
    "\nDescription: {description}\nParameters (JSON Schema): {schema}"
)


@dataclass(frozen=True)
class ToolChange:
    """One announced change to the Tools of a prompt epoch.

    ``tool`` is the registry name. ``definition`` is the Provider definition the
    Model is told to use (absent for a removal). ``listed`` says whether the
    Tool appears in the request's Tool list: always for a pinned Tool, and for
    an addition on a route that drops calls to unlisted Tools. ``pinned`` marks a
    Tool of the epoch's pinned list that becomes available again after a
    removal. ``source`` fingerprints the Tool's registered definition so a
    later change is recognized; ``detail`` is optional Tool-authored text.
    """

    change: ToolChangeKind
    tool: str
    epoch: str
    source: str | None = None
    definition: JsonObject | None = None
    listed: bool = False
    pinned: bool = False
    detail: str | None = None

    def note_content(self) -> str:
        """Return the note content that persists this change."""

        definition = self.definition or {}
        payload: JsonObject = {
            "v": TOOL_CHANGE_NOTE_VERSION,
            "epoch": self.epoch,
            "change": self.change,
            "tool": self.tool,
            "source": self.source,
            "description": definition.get("description"),
            "parameters": definition.get("parameters"),
            "listed": self.listed,
            "pinned": self.pinned,
            "detail": self.detail,
        }
        return TOOL_CHANGE_NOTE_PREFIX + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )


def tool_change_from_note(message: ChatMessage) -> ToolChange | None:
    """Parse a ``[tool-change]`` note; ``None`` for other or malformed notes."""

    if not is_tool_change_note(message):
        return None
    content = cast(str, message.content)
    try:
        payload = json.loads(content.removeprefix(TOOL_CHANGE_NOTE_PREFIX))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("v") != TOOL_CHANGE_NOTE_VERSION:
        return None
    change = payload.get("change")
    tool = payload.get("tool")
    epoch = payload.get("epoch")
    if (
        change not in _TOOL_CHANGE_KINDS
        or not isinstance(tool, str)
        or not tool
        or not isinstance(epoch, str)
        or not epoch
        or not _optional_string(payload.get("source"))
        or not _optional_string(payload.get("detail"))
        or not isinstance(payload.get("listed"), bool)
        or not isinstance(payload.get("pinned"), bool)
    ):
        return None
    description = payload.get("description")
    parameters = payload.get("parameters")
    definition: JsonObject | None = None
    if change != "removed":
        if not isinstance(description, str) or not isinstance(parameters, dict):
            return None
        definition = {"name": tool, "description": description, "parameters": parameters}
    return ToolChange(
        change=cast(ToolChangeKind, change),
        tool=tool,
        epoch=epoch,
        source=payload.get("source"),
        definition=definition,
        listed=payload["listed"],
        pinned=payload["pinned"],
        detail=payload.get("detail"),
    )


def render_tool_change(change: ToolChange) -> str:
    """Return the System Reminder text announcing ``change`` to the Model."""

    name = model_tool_name(change.tool)
    if change.change == "removed":
        return _REMOVED.format(name=name)
    if change.change == "added" and change.pinned:
        return _AVAILABLE_AGAIN.format(name=name)
    if change.change == "added" and change.listed:
        return _ADDED_LISTED.format(name=name)
    definition = change.definition or {}
    template = _CHANGED if change.change == "changed" else _ADDED_UNLISTED
    return template.format(
        name=name,
        description=definition.get("description", ""),
        schema=compact_schema(definition.get("parameters", {})),
    )


def compact_schema(parameters: Any) -> str:
    """Serialize a parameter schema compactly, keeping its key order."""

    return json.dumps(parameters, separators=(",", ":"), ensure_ascii=False)


def _optional_string(value: object) -> bool:
    return value is None or isinstance(value, str)
