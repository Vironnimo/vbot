"""Live Tool arguments as Models write them.

Voice models send arguments as JSON text and use other harnesses' field names
(``prompt``, ``session_id``). :func:`prepare_live_call` turns a call whose
intent is clear into one canonical, validated Live Tool call through the shared
Tool contract machinery, or returns a failure result that says what was wrong
and names the next valid call. The Tool registry already resolved the name.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

from core.model_tasks.live import live_failure
from core.tools.call_syntax import (
    SpellingAliases,
    is_placeholder,
    normalize_call_arguments,
    spelling,
)
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.live import (
    LIVE_KEYS,
    LIVE_TOOL_NAMES,
    LIVE_VIEWS,
    TOOL_END_CALL,
    TOOL_MANAGE_TERMINALS,
    TOOL_OPEN,
    TOOL_OVERVIEW,
    TOOL_READ_OUTPUT,
    TOOL_SEND_MESSAGE,
    TOOL_START_AGENT_SESSION,
    TOOL_START_CODING_TERMINAL,
    TOOL_STOP,
    live_tool_definitions,
)
from server.live._programs import CODING_PROGRAM_ALIASES

JsonObject = dict[str, Any]

_TASK = ("prompt", "message", "instruction", "request", "text", "goal")
_COUNT = ("n", "times", "copies", "number", "instances", "amount", "quantity")
_TARGET = ("session", "session_id", "terminal", "terminal_id", "id", "ref", "to", "recipient")
_FOLDER = ("workdir", "cwd", "directory", "dir", "path", "project_folder")

# Field names other harnesses use, per Tool; an alias applies only when the
# canonical field is absent.
_FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    TOOL_OVERVIEW: {"agent": ("agent_name", "agent_id", "assistant")},
    TOOL_START_AGENT_SESSION: {
        "agent": ("agent_name", "agent_id", "name", "assistant"),
        "task": _TASK,
        "count": _COUNT,
        "project": ("project_name", "project_id"),
    },
    TOOL_START_CODING_TERMINAL: {
        "program": ("cli", "tool", "agent"),
        "task": _TASK,
        "count": _COUNT,
        "folder": (*_FOLDER, "project", "project_name"),
    },
    TOOL_SEND_MESSAGE: {
        "target": (*_TARGET, "agent"),
        "text": ("message", "content", "prompt", "answer", "reply"),
    },
    TOOL_READ_OUTPUT: {"target": (*_TARGET, "agent")},
    TOOL_STOP: {"target": (*_TARGET, "agent")},
    TOOL_OPEN: {"target": (*_TARGET, "agent", "group", "project")},
    TOOL_MANAGE_TERMINALS: {
        "action": ("op",),
        "target": ("terminal", "terminal_id", "id", "ref", "group", "group_id", "group_name"),
        "name": ("new_name",),
        "order": ("terminals", "terminal_ids", "refs"),
    },
    TOOL_END_CALL: {},
}

_KEY_ALIASES = {
    "esc": "escape",
    "return": "enter",
    "arrowup": "up",
    "uparrow": "up",
    "arrowdown": "down",
    "downarrow": "down",
    "arrowleft": "left",
    "leftarrow": "left",
    "arrowright": "right",
    "rightarrow": "right",
    "ctrlc": "ctrl-c",
    "controlc": "ctrl-c",
}

# Examples of a valid call, used in failure messages.
_EXAMPLES = {
    TOOL_OVERVIEW: "{}",
    TOOL_START_AGENT_SESSION: '{"agent": "<Agent name from overview>", "task": "<the task>"}',
    TOOL_START_CODING_TERMINAL: '{"program": "codex", "task": "<the task>"}',
    TOOL_SEND_MESSAGE: '{"target": "s2", "text": "<the message>"}',
    TOOL_READ_OUTPUT: '{"target": "s2"}',
    TOOL_STOP: '{"target": "s2"}',
    TOOL_OPEN: '{"target": "s2"} or {"view": "terminals"}',
    TOOL_MANAGE_TERMINALS: '{"action": "maximize", "target": "t1"}',
    TOOL_END_CALL: "{}",
}


@dataclass(frozen=True)
class PreparedLiveCall:
    """One canonical Live Tool call."""

    name: str
    arguments: JsonObject


def prepare_live_call(name: str, arguments: Any) -> PreparedLiveCall | JsonObject:
    """Return the canonical call the Model clearly meant, or a failure result."""
    if name not in LIVE_TOOL_NAMES:
        return live_failure(
            "unknown_tool",
            f'There is no Live Tool called "{name}". Call one of: {", ".join(LIVE_TOOL_NAMES)}.',
        )
    parsed = _parsed_arguments(arguments)
    if parsed is None:
        return live_failure(
            "invalid_arguments",
            f"The arguments are not one JSON object. Call {name} again with an object such as "
            f"{_EXAMPLES[name]}.",
        )
    try:
        prepared = _normalized(name, parsed)
        _contract(name).validate_arguments(prepared)
    except ToolContractError as error:
        return live_failure(
            "invalid_arguments",
            f"{error} Call {name} again, for example with {_EXAMPLES[name]}.",
        )
    return PreparedLiveCall(name=name, arguments=prepared)


def _parsed_arguments(arguments: Any) -> JsonObject | None:
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    return dict(arguments) if isinstance(arguments, dict) else None


def _normalized(tool: str, arguments: JsonObject) -> JsonObject:
    """Drop fields the call does not use, then apply the owner's spellings and repairs."""

    payload = _payload_spellings(tool)
    cleaned = {
        key: value
        for key, value in arguments.items()
        if not (
            value in ([], {})
            or (_blank(value) if spelling(key) in payload else is_placeholder(value))
        )
    }
    normalized = normalize_call_arguments(
        _contract(tool),
        cleaned,
        enum_fields=("program", "view", "key", "action"),
        field_aliases=_absent_field_aliases(tool, cleaned),
        field_normalizers=_FIELD_NORMALIZERS,
        empty_as_omitted=_TEXT_FIELDS,
    )
    if not isinstance(normalized, dict):
        raise ToolContractError("Provide one JSON argument object.")
    return normalized


_TEXT_FIELDS = ("agent", "task", "target", "text", "folder", "project", "name", "action")
# Fields whose value vBot passes on or writes into the app as given: a task, a
# message, a new name. Their text is never rewritten; "None" or "n/a" there is
# what the user said. Only a field that looks something up or picks a choice
# treats such a word as a stand-in for leaving the field out.
_PAYLOAD_FIELDS = ("task", "text", "name")


@cache
def _payload_spellings(tool: str) -> frozenset[str]:
    """The spellings under which this Tool's call carries a payload field."""

    properties = _contract(tool).input_schema.get("properties", {})
    aliases = _FIELD_ALIASES[tool]
    return frozenset(
        spelling(name)
        for field in _PAYLOAD_FIELDS
        if field in properties
        for name in (field, *aliases.get(field, ()))
    )


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _absent_field_aliases(tool: str, arguments: JsonObject) -> SpellingAliases:
    """The Tool's field aliases, without those whose canonical field the call sets."""

    return SpellingAliases(
        {field: names for field, names in _FIELD_ALIASES[tool].items() if field not in arguments}
    )


def _single_text(value: Any) -> Any:
    """Accept a one-element list where one text is expected."""

    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    return value


def _program(value: Any) -> Any:
    value = _single_text(value)
    if isinstance(value, str):
        return CODING_PROGRAM_ALIASES.get(spelling(value), value)
    return value


def _key(value: Any) -> Any:
    value = _single_text(value)
    if isinstance(value, str):
        key = spelling(value)
        if key in _KEY_ALIASES:
            return _KEY_ALIASES[key]
        return next((name for name in LIVE_KEYS if spelling(name) == key), value)
    return value


_VIEW_ALIASES = {
    "session": "chat",
    "sessions": "chat",
    "crons": "cron",
    "cronjobs": "cron",
    "stats": "statistics",
    "usage": "statistics",
}


def _view(value: Any) -> Any:
    value = _single_text(value)
    if isinstance(value, str):
        key = spelling(value)
        if key in _VIEW_ALIASES:
            return _VIEW_ALIASES[key]
        for view in LIVE_VIEWS:
            if key in {spelling(view), spelling(view).removesuffix("s")}:
                return view
    return value


_FIELD_NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    **dict.fromkeys(_TEXT_FIELDS, _single_text),
    "program": _program,
    "key": _key,
    "view": _view,
}


@cache
def _contract(tool: str) -> ToolContract:
    definition = next(item for item in live_tool_definitions() if item["name"] == tool)
    return compile_tool_contract(
        name=tool, input_schema=definition["parameters"], require_closed_input=False
    )


__all__ = ["PreparedLiveCall", "prepare_live_call"]
