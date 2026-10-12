"""Read ``background_tasks`` calls, also in other harnesses' dialects.

Agents reach the Tool under names they know from other harnesses (``KillShell``,
``BashOutput``, ``TaskStop``; ``core/tools/model_names.py``) and with those
harnesses' fields (``shell_id``, ``task_id``, ``bash_id``). The normalizer maps
every id spelling onto ``id`` and action synonyms onto ``list``, ``status`` and
``stop``. A call without an action but with an id shows that task's status
(``ACTION_MISSING``): reading is safe, a stop is never guessed.

Asking to wait for a task fails before anything runs, with the reason. A request
for input or a message is refused by the handler (``INPUT_ACTION``), because its
refusal names ``terminal`` or ``subagent`` only when the Agent can call it.
Fields of other harnesses' output Tools that ask to wait for a task or to filter
its output (``block``, ``timeout``, ``filter``) have no effect; the call runs
and the handler says so (``NO_EFFECT_FIELDS``).
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from core.tools.call_syntax import (
    SpellingAliases,
    is_placeholder,
    normalize_call_arguments,
    spelling,
)
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.tools import JsonObject

BACKGROUND_TASKS_TOOL_NAME = "background_tasks"
BACKGROUND_TASK_ACTIONS = ("list", "status", "stop")
# Set by the normalizer only. ``ACTION_MISSING``: the call named an id but no
# action, so it runs as status with a note. ``INPUT_ACTION``: the action the call
# sent to give a task input or a message, which the handler refuses;
# ``MESSAGE_CONTENT``: the text of a message for a Sub-Agent, for the refusal's
# corrected call. ``NO_EFFECT_FIELDS``: each field that cannot have an effect, by
# the key the call sent, with its kind: ``wait`` or ``filter``.
ACTION_MISSING = "action_missing"
INPUT_ACTION = "input_action"
MESSAGE_CONTENT = "message_content"
NO_EFFECT_FIELDS = "no_effect_fields"
NO_EFFECT_WAIT = "wait"
NO_EFFECT_FILTER = "filter"
BACKGROUND_TASKS_UNADVERTISED_PARAMETERS: JsonObject = {
    ACTION_MISSING: {"type": "boolean"},
    INPUT_ACTION: {"type": "string"},
    MESSAGE_CONTENT: {"type": "string"},
    NO_EFFECT_FIELDS: {
        "type": "object",
        "additionalProperties": {"type": "string", "enum": [NO_EFFECT_WAIT, NO_EFFECT_FILTER]},
    },
}

# Ids of background commands, Sub-Agents and terminals by their prefix.
COMMAND_ID_PREFIX = "cmd_"
SUBAGENT_ID_PREFIX = "sub_"
TERMINAL_ID_PREFIX = "term_"
SESSION_ID_PREFIX = "ses_"

_ID_ALIASES = SpellingAliases(
    {
        "id": (
            "task_id",
            "command_id",
            "subagent_id",
            "work_id",
            "shell_id",
            "bash_id",
            "process_id",
            "proc_id",
            "job_id",
        )
    }
)
# Fields that name something else unless their value is an id this Tool reads:
# a task's id, a terminal's id (refused with the call that serves it), or the
# Session of a Sub-Agent.
_ID_BY_VALUE = {
    "terminalid": (COMMAND_ID_PREFIX, SUBAGENT_ID_PREFIX, TERMINAL_ID_PREFIX),
    "sessionid": (COMMAND_ID_PREFIX, SUBAGENT_ID_PREFIX, SESSION_ID_PREFIX),
    "agentid": (COMMAND_ID_PREFIX, SUBAGENT_ID_PREFIX),
}
_ACTION_SYNONYMS = SpellingAliases(
    {
        "list": ("ls", "all"),
        "status": (
            "output",
            "log",
            "logs",
            "check",
            "get",
            "poll",
            "read",
            "show",
            "inspect",
            "info",
        ),
        "stop": ("kill", "cancel", "terminate", "end", "abort", "halt", "interrupt"),
    }
)
_WAIT_ACTIONS = frozenset({"wait", "await", "polluntil"})
# Claude Code's TaskOutput waits with block and timeout; BashOutput keeps only
# the output lines that match filter.
_WAIT_FIELDS = frozenset({"block", "wait", "timeout", "timeoutms", "timeoutseconds"})
_FILTER_FIELDS = frozenset({"filter", "grep"})
# Values that ask for nothing, such as block "false": no wait is what happens.
_NO_REQUEST = frozenset({"false", "no", "0", "none", "null"})
_MESSAGE_ACTIONS = frozenset({"send", "message", "tell", "steer"})
_INPUT_ACTIONS = frozenset({"input", "write", "submit", "sendkeys", "paste", "stdin"})
_CONTENT_FIELDS = ("content", "message", "text", "prompt")
_CHOICES = ", ".join(BACKGROUND_TASK_ACTIONS)


@cache
def _repair_contract() -> ToolContract:
    # Imported lazily: background_tasks.py imports this module for its registration.
    from core.tools.background_tasks import BACKGROUND_TASKS_TOOL_PARAMETERS

    properties = {
        **BACKGROUND_TASKS_TOOL_PARAMETERS["properties"],
        **BACKGROUND_TASKS_UNADVERTISED_PARAMETERS,
    }
    return compile_tool_contract(
        name=BACKGROUND_TASKS_TOOL_NAME,
        input_schema={**BACKGROUND_TASKS_TOOL_PARAMETERS, "properties": properties},
        require_closed_input=False,
    )


def normalize_background_tasks_arguments(arguments: Any) -> Any:
    """Return the canonical ``background_tasks`` arguments for one Model call."""
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        enum_fields=("action",),
        field_aliases=_ID_ALIASES,
        field_normalizers={"action": _action_word},
        empty_as_omitted=("action", "id"),
        placeholder_as_omitted=("id",),
    )
    if not isinstance(normalized, dict):
        return normalized
    _set_aside_no_effect_fields(normalized)
    _read_ids_by_value(normalized)
    for name in ("action", "id"):
        if name in normalized and is_placeholder(normalized[name]):
            del normalized[name]
    action = normalized.get("action")
    if action is None:
        if "id" not in normalized:
            raise ToolContractError(
                f"{BACKGROUND_TASKS_TOOL_NAME} needs action: list, status or stop."
            )
        normalized["action"] = action = "status"
        normalized[ACTION_MISSING] = True
    elif isinstance(action, str) and action not in BACKGROUND_TASK_ACTIONS:
        return _unknown_action(normalized, action)
    _read_one_id(normalized, action)
    return normalized


def _action_word(value: Any) -> Any:
    return _ACTION_SYNONYMS.get(value, value) if isinstance(value, str) else value


def _set_aside_no_effect_fields(arguments: dict[str, Any]) -> None:
    """Move fields that ask to wait or to filter output under ``NO_EFFECT_FIELDS``.

    A field that asks for nothing, such as ``block: false``, is dropped silently;
    so is a ``timeout`` that comes with ``block: false``.
    """
    fields = {
        key: arguments.pop(key)
        for key in list(arguments)
        if spelling(key) in _WAIT_FIELDS or spelling(key) in _FILTER_FIELDS
    }
    earlier = arguments.pop(NO_EFFECT_FIELDS, None)
    no_effect: dict[str, str] = dict(earlier) if isinstance(earlier, dict) else {}
    waits = not any(spelling(key) == "block" and not _asks(value) for key, value in fields.items())
    for key, value in fields.items():
        if not _asks(value):
            continue
        if spelling(key) in _FILTER_FIELDS:
            no_effect[key] = NO_EFFECT_FILTER
        elif waits:
            no_effect[key] = NO_EFFECT_WAIT
    if no_effect:
        arguments[NO_EFFECT_FIELDS] = no_effect


def _asks(value: Any) -> bool:
    """Whether a field's *value* asks for something rather than for nothing."""
    if value is None or value is False or is_placeholder(value):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().casefold() not in _NO_REQUEST
    return value not in ([], {})


def _read_ids_by_value(arguments: dict[str, Any]) -> None:
    """Read ``terminal_id``, ``session_id`` and ``agent_id`` as ``id`` when they hold one."""
    for key in list(arguments):
        prefixes = _ID_BY_VALUE.get(spelling(key))
        value = arguments[key]
        if prefixes is None or not isinstance(value, str) or not value.startswith(prefixes):
            continue
        current = arguments.get("id")
        if current is not None and not is_placeholder(current) and current != value:
            raise ToolContractError(
                f"Conflicting values for id: id is {json.dumps(current)} and {key} is "
                f"{json.dumps(value)}. Send only the intended one."
            )
        del arguments[key]
        arguments["id"] = value


def is_wait_action(action: str) -> bool:
    """Whether *action* asks to wait for a task, which nothing in vBot does."""
    return spelling(action) in _WAIT_ACTIONS


def is_message_action(action: str) -> bool:
    """Whether *action* asks to send a task a message."""
    return spelling(action) in _MESSAGE_ACTIONS


def _wait_refusal(action: str) -> str:
    """The refusal of an action that waits for a task."""
    return (
        f'Unknown action "{action}". Use one of: {_CHOICES}. Command results and Sub-Agent '
        "answers arrive as new messages, so there is nothing to wait for."
    )


def _unknown_action(arguments: dict[str, Any], action: str) -> JsonObject:
    """Refuse an action the Tool does not have; input and messages go to the handler."""
    word = spelling(action)
    if is_wait_action(action):
        raise ToolContractError(_wait_refusal(action))
    if word not in _MESSAGE_ACTIONS and word not in _INPUT_ACTIONS:
        raise ToolContractError(f'Unknown action "{action}". Use one of: {_CHOICES}.')
    # Only the refusal runs: the call's keys are dropped unread, except a message
    # for a Sub-Agent, which the refusal's corrected call carries.
    refused: JsonObject = {"action": "list", INPUT_ACTION: action}
    task_id = arguments.get("id")
    if (
        word in _MESSAGE_ACTIONS
        and isinstance(task_id, str)
        and task_id.startswith(SUBAGENT_ID_PREFIX)
    ):
        refused["id"] = task_id
        content = next(
            (
                value
                for key, value in arguments.items()
                if spelling(key) in _CONTENT_FIELDS and isinstance(value, str) and value.strip()
            ),
            None,
        )
        if content is not None:
            refused[MESSAGE_CONTENT] = content
    return refused


def _read_one_id(arguments: dict[str, Any], action: str) -> None:
    """Read a list of one id as that id; refuse several, which ``list`` ignores."""
    value = arguments.get("id")
    if not isinstance(value, list):
        return
    ids = [item for item in value if not is_placeholder(item)]
    if action == "list" or not ids:
        del arguments["id"]
    elif len(ids) == 1:
        arguments["id"] = ids[0]
    elif action == "stop":
        raise ToolContractError("stop takes one id; nothing was stopped. Call it once for each id.")
    else:
        raise ToolContractError(f"{action} takes one id. Call it once for each id.")


__all__ = [
    "ACTION_MISSING",
    "BACKGROUND_TASKS_TOOL_NAME",
    "BACKGROUND_TASKS_UNADVERTISED_PARAMETERS",
    "BACKGROUND_TASK_ACTIONS",
    "COMMAND_ID_PREFIX",
    "INPUT_ACTION",
    "MESSAGE_CONTENT",
    "NO_EFFECT_FIELDS",
    "NO_EFFECT_FILTER",
    "NO_EFFECT_WAIT",
    "SESSION_ID_PREFIX",
    "SUBAGENT_ID_PREFIX",
    "TERMINAL_ID_PREFIX",
    "is_message_action",
    "is_wait_action",
    "normalize_background_tasks_arguments",
]
