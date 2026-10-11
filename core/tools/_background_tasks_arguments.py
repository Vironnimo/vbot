"""Read ``background_tasks`` calls, also in other harnesses' dialects.

Agents reach the Tool under names they know from other harnesses (``KillShell``,
``BashOutput``, ``TaskStop``; ``core/tools/model_names.py``) and with those
harnesses' fields (``shell_id``, ``task_id``, ``bash_id``). The normalizer maps
every id spelling onto ``id`` and action synonyms onto ``list``, ``status`` and
``stop``. A call without an action but with an id shows that task's status
(``ACTION_MISSING``): reading is safe, a stop is never guessed.

What the Tool never does fails before anything runs: waiting for a task, and
sending one a message, each with the call that does it instead. A request for
input is refused by the handler (``INPUT_ACTION``), because its refusal names
``terminal`` only when the Agent can call it.
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
# sent to give a task input, which the handler refuses.
ACTION_MISSING = "action_missing"
INPUT_ACTION = "input_action"
BACKGROUND_TASKS_UNADVERTISED_PARAMETERS: JsonObject = {
    ACTION_MISSING: {"type": "boolean"},
    INPUT_ACTION: {"type": "string"},
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


def _unknown_action(arguments: dict[str, Any], action: str) -> JsonObject:
    """Refuse an action the Tool does not have; an input request goes to the handler."""
    word = spelling(action)
    unknown = f'Unknown action "{action}". Use one of: {_CHOICES}.'
    if word in _WAIT_ACTIONS:
        raise ToolContractError(
            f"{unknown} Command results and Sub-Agent answers arrive as new messages, so there "
            "is nothing to wait for."
        )
    task_id = arguments.get("id")
    if (
        word in _MESSAGE_ACTIONS
        and isinstance(task_id, str)
        and task_id.startswith(SUBAGENT_ID_PREFIX)
    ):
        raise ToolContractError(_message_refusal(arguments, task_id))
    if word in _MESSAGE_ACTIONS or word in _INPUT_ACTIONS:
        # Only the refusal runs: the call's text and keys are dropped unread.
        return {"action": "list", INPUT_ACTION: action}
    raise ToolContractError(unknown)


def _message_refusal(arguments: dict[str, Any], subagent_id: str) -> str:
    refusal = (
        f"{BACKGROUND_TASKS_TOOL_NAME} sends no messages; nothing was sent. To message "
        f"Sub-Agent {subagent_id}, call subagent"
    )
    content = next(
        (
            value
            for key, value in arguments.items()
            if spelling(key) in _CONTENT_FIELDS and isinstance(value, str) and value.strip()
        ),
        None,
    )
    if content is None:
        return f"{refusal} with action send, this id and your message as content."
    call = {"action": "send", "id": subagent_id, "content": content}
    return f"{refusal} {json.dumps(call, ensure_ascii=False)}."


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
    "SESSION_ID_PREFIX",
    "SUBAGENT_ID_PREFIX",
    "TERMINAL_ID_PREFIX",
    "normalize_background_tasks_arguments",
]
