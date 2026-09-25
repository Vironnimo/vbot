"""Live Tool calls as Models write them: names, argument spellings, and old Tools.

Voice models call Live Tools by other names (``status``, ``delegate``), send
arguments as JSON text, and use other harnesses' field names (``prompt``,
``session_id``). ``prepare_live_call`` turns a call whose intent is clear into
one canonical Live Tool call through the shared Tool contract machinery, or
returns a failure result that says what was wrong and names the next valid call.
The delegation loop and direct Tools mode both prepare every call here before
the host runs it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

from core.model_tasks._live_programs import CODING_PROGRAM_ALIASES
from core.model_tasks._live_tools import (
    LIVE_KEYS,
    LIVE_TOOL_NAMES,
    LIVE_VIEWS,
    TOOL_OPEN,
    TOOL_OVERVIEW,
    TOOL_READ,
    TOOL_SEND_MESSAGE,
    TOOL_START_AGENT_SESSION,
    TOOL_START_CODING_TERMINAL,
    TOOL_STOP,
    TOOL_TERMINAL,
    live_failure,
    live_tools,
)
from core.tools import called_tool_name
from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, is_placeholder, spelling
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract

JsonObject = dict[str, Any]

_OLD_APP_TOOL = "vbot_app"
_OLD_TERMINAL_TOOL = "vbot_terminal"
_WRAPPER_PREFIX = re.compile(r"^(?:functions?|default_api|tools?)[.:]", re.IGNORECASE)

# Other names for Live Tools, by spelling; the pair's arguments are implied
# only where the call leaves them out.
_NAME_ALIASES: dict[str, tuple[str, JsonObject]] = {
    **dict.fromkeys(("status", "list", "overviewstatus"), (TOOL_OVERVIEW, {})),
    **dict.fromkeys(
        ("startsession", "startagent", "newsession", "delegate", "spawn"),
        (TOOL_START_AGENT_SESSION, {}),
    ),
    **dict.fromkeys(("startterminal", "startcli", "launch"), (TOOL_START_CODING_TERMINAL, {})),
    "startcodex": (TOOL_START_CODING_TERMINAL, {"program": "codex"}),
    "startclaude": (TOOL_START_CODING_TERMINAL, {"program": "claude"}),
    **dict.fromkeys(("send", "message", "reply", "answer"), (TOOL_SEND_MESSAGE, {})),
    **dict.fromkeys(("get", "showsession", "readsession", "readterminal"), (TOOL_READ, {})),
    **dict.fromkeys(("cancel", "abort", "interrupt"), (TOOL_STOP, {})),
    **dict.fromkeys(("navigate", "show", "goto"), (TOOL_OPEN, {})),
}

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
    TOOL_READ: {"target": (*_TARGET, "agent")},
    TOOL_STOP: {"target": (*_TARGET, "agent")},
    TOOL_OPEN: {"target": (*_TARGET, "agent", "group", "project")},
    TOOL_TERMINAL: {
        "action": ("op",),
        "target": ("terminal", "terminal_id", "id", "ref", "group", "group_id", "group_name"),
        "name": ("new_name",),
        "order": ("terminals", "terminal_ids", "refs"),
    },
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
    TOOL_READ: '{"target": "s2"}',
    TOOL_STOP: '{"target": "s2"}',
    TOOL_OPEN: '{"target": "s2"} or {"view": "terminals"}',
    TOOL_TERMINAL: '{"action": "maximize", "target": "t1"}',
}

_OLD_APP_ACTIONS = {"context", "sessions", "read", "open", "send"}
_OLD_TERMINAL_ACTIONS = {
    "start",
    "list",
    "read",
    "input",
    "show",
    "show_group",
    "maximize",
    "restore",
    "reorder",
    "close",
    "create_group",
    "rename_group",
    "delete_group",
}


@dataclass(frozen=True)
class PreparedLiveCall:
    """One canonical Live Tool call; ``called`` is the name the Model used."""

    called: str
    name: str
    arguments: JsonObject


def prepare_live_call(name: Any, arguments: Any) -> PreparedLiveCall | JsonObject:
    """Return the canonical call the Model clearly meant, or a failure result."""

    called = name if isinstance(name, str) else ""
    parsed = _parsed_arguments(arguments)
    if parsed is None:
        tool = _tool_name(called)[0]
        return live_failure(
            "invalid_arguments",
            "The arguments are not one JSON object. Call "
            f"{tool or 'the Tool'} again with an object such as "
            f"{_EXAMPLES.get(tool or '', '{}')}.",
        )
    if called in {_OLD_APP_TOOL, _OLD_TERMINAL_TOOL}:
        converted = _old_call(called, parsed)
        if converted is None:
            return live_failure(
                "unknown_tool",
                f"{called} is no longer available. Call one of: {', '.join(LIVE_TOOL_NAMES)}.",
            )
        tool, parsed = converted
        implied: JsonObject = {}
    else:
        tool, implied = _tool_name(called)
        if tool is None:
            return live_failure(
                "unknown_tool",
                f'There is no Tool called "{called}". Call one of: {", ".join(LIVE_TOOL_NAMES)}.',
            )
    try:
        prepared = {**implied, **_normalized(tool, parsed)}
        _contract(tool).validate_arguments(prepared)
    except ToolContractError as error:
        return live_failure(
            "invalid_arguments",
            f"{error} Call {tool} again, for example with {_EXAMPLES[tool]}.",
        )
    return PreparedLiveCall(called=called, name=tool, arguments=prepared)


def _tool_name(name: str) -> tuple[str | None, JsonObject]:
    if not name.strip():
        return None, {}
    offered = set(LIVE_TOOL_NAMES)
    mapped = called_tool_name(name, offered)
    if mapped in offered:
        return mapped, {}
    alias = _NAME_ALIASES.get(spelling(_WRAPPER_PREFIX.sub("", name.strip())))
    return alias if alias is not None else (None, {})


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
    """Apply the owner's spellings, then the shared contract repairs."""

    cleaned = {
        key: value
        for key, value in arguments.items()
        if not (is_placeholder(value) or value in ([], {}))
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


def _view(value: Any) -> Any:
    value = _single_text(value)
    if isinstance(value, str):
        key = spelling(value)
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


def _old_call(name: str, arguments: JsonObject) -> tuple[str, JsonObject] | None:
    """Map a call of the former ``vbot_app`` / ``vbot_terminal`` Tools, when exact."""

    action = arguments.get("action")
    if name == _OLD_APP_TOOL and action in _OLD_APP_ACTIONS:
        return _old_app_call(str(action), arguments)
    if name == _OLD_TERMINAL_TOOL and action in _OLD_TERMINAL_ACTIONS:
        return _old_terminal_call(str(action), arguments)
    return None


def _old_app_call(action: str, arguments: JsonObject) -> tuple[str, JsonObject]:
    session = arguments.get("session_id") or arguments.get("agent_id")
    if action == "context":
        return TOOL_OVERVIEW, {}
    if action == "sessions":
        return TOOL_OVERVIEW, _present(agent=arguments.get("agent_id"))
    if action == "read":
        return TOOL_READ, _present(target=session)
    if action == "open":
        if arguments.get("session_id"):
            return TOOL_OPEN, _present(target=arguments.get("session_id"))
        return TOOL_OPEN, _present(view=arguments.get("view"))
    return TOOL_SEND_MESSAGE, _present(target=session, text=arguments.get("text"))


def _old_terminal_call(action: str, arguments: JsonObject) -> tuple[str, JsonObject]:
    terminal = arguments.get("terminal_id")
    group = arguments.get("group_id")
    if action == "start":
        return TOOL_START_CODING_TERMINAL, _present(
            program=arguments.get("program"),
            count=arguments.get("count"),
            folder=arguments.get("workdir"),
            name=arguments.get("name"),
        )
    if action == "list":
        return TOOL_OVERVIEW, {}
    if action == "read":
        return TOOL_READ, _present(target=terminal)
    if action == "input":
        if "key" in arguments:
            return TOOL_TERMINAL, _present(action="key", target=terminal, key=arguments.get("key"))
        return TOOL_SEND_MESSAGE, _present(target=terminal, text=arguments.get("text"))
    if action in {"show", "show_group"}:
        return TOOL_OPEN, _present(target=terminal or group)
    if action in {"maximize", "close"}:
        return TOOL_TERMINAL, _present(action=action, target=terminal)
    if action == "restore":
        return TOOL_TERMINAL, {"action": "restore"}
    if action == "reorder":
        return TOOL_TERMINAL, _present(action="reorder", target=group, order=arguments.get("order"))
    return TOOL_TERMINAL, _present(action=action, target=group, name=arguments.get("name"))


def _present(**fields: Any) -> JsonObject:
    return {key: value for key, value in fields.items() if value is not None}


@cache
def _contract(tool: str) -> ToolContract:
    definition = next(item for item in live_tools() if item["name"] == tool)
    return compile_tool_contract(
        name=tool, input_schema=definition["parameters"], require_closed_input=False
    )


__all__ = ["PreparedLiveCall", "prepare_live_call"]
