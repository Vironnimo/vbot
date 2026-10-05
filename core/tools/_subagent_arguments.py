"""Read ``subagent`` calls written in vBot's schema or another harness's dialect.

Models trained on other harnesses delegate with the same intent under other
names: Claude Code and opencode ``Task`` (``description``, ``prompt``,
``subagent_type``, ``run_in_background``), Hermes ``delegate_task`` (``goal``,
``context``, ``toolsets``), OpenClaw and pi (``task``, ``label``, ``agentId``,
``agent``, ``thinking``), and vBot's own earlier ``request`` and ``blocking``
shapes. This owner maps those spellings onto the canonical fields, reads
action synonyms, and omits placeholder values, so a call whose intent is clear
executes. What vBot cannot honor as written (per-call Tool sets, several tasks
in one call) fails before any side effect with the corrected call.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from core.tools.call_syntax import (
    PLACEHOLDER_WORDS,
    SpellingAliases,
    is_placeholder,
    normalize_call_arguments,
    spelling,
)
from core.tools.contracts import ToolContract, ToolContractError
from core.tools.tools import JsonObject

BACKGROUND_FIELD = "background"
# Accepted, never advertised: a requested delivery mode reaches the handler, which
# states in the result when vBot's fixed mode differs, and a ``session_id`` from a
# result names the Sub-Agent of that Session.
UNADVERTISED_PARAMETERS: JsonObject = {
    BACKGROUND_FIELD: {"type": "boolean"},
    "session_id": {"type": "string"},
}

_FIELD_ALIASES = SpellingAliases(
    {
        "content": (
            "prompt",
            "task",
            "message",
            "instructions",
            "instruction",
            "goal",
            "objective",
            "brief",
        ),
        "description": ("title", "label", "summary", "task_name"),
        "agent_id": (
            "agent",
            "agent_name",
            "agent_type",
            "subagent",
            "subagent_name",
            "subagent_type",
            "target",
            "target_agent",
            "target_agent_id",
        ),
        "session_id": ("session", "subagent_session_id"),
        "thinking_effort": ("thinking", "effort", "reasoning", "reasoning_effort"),
        "id": ("work_id", "subagent_id"),
        BACKGROUND_FIELD: ("run_in_background", "in_background", "non_blocking"),
    }
)
_ACTION_SYNONYMS = SpellingAliases(
    {
        "run": ("spawn", "delegate", "start", "create", "launch", "dispatch", "execute"),
        "send": (
            "message",
            "steer",
            "tell",
            "continue",
            "resume",
            "followup",
            "reply",
            "answer",
            "instruct",
        ),
        "list": (
            "status",
            "ls",
            "check",
            "get",
            "inspect",
            "poll",
            "query",
            "show",
            "info",
            "progress",
            "result",
            "results",
            "fetch",
            "wait",
        ),
        "cancel": ("stop", "kill", "abort", "terminate", "halt", "interrupt"),
    }
)
# Placeholder vocabulary per optional field. ``"none"`` is a real thinking effort.
_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "id": PLACEHOLDER_WORDS,
    "session_id": PLACEHOLDER_WORDS | {"new"},
    "agent_id": PLACEHOLDER_WORDS,
    "description": PLACEHOLDER_WORDS,
    "model": PLACEHOLDER_WORDS | {"default", "inherit"},
    "thinking_effort": (PLACEHOLDER_WORDS - {"none"}) | {"default", "inherit"},
}
_TASK_TEXT_KEYS = ("task", "prompt", "goal", "message")
_BLOCKING = "blocking"
_CONTEXT = "context"
_TOOLSETS_MESSAGE = (
    "subagent was not run: a Sub-Agent always works with its Agent's own Tools, so "
    '"toolsets" cannot be chosen per call. Repeat this call without "toolsets", and state '
    "any Tool restriction in content."
)
_TASKS_MESSAGE = (
    'subagent was not run: it takes one task per call. Send each item of "tasks" as its '
    'own subagent call, with that task as "content", all in the same turn so they run '
    "concurrently."
)
_BOOLEAN_WORDS = {"true": True, "false": False}


def normalize_subagent_arguments(
    contract: ToolContract,
    arguments: Any,
) -> Any:
    """Return the canonical ``subagent`` arguments for one Model call."""
    if isinstance(arguments, dict):
        arguments = _task_text_request(arguments)
    normalized = normalize_call_arguments(
        contract,
        arguments,
        enum_fields=("action", "thinking_effort"),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"action": _action_word},
    )
    if not isinstance(normalized, dict):
        return normalized
    if _field(normalized, "toolsets") is not None:
        raise ToolContractError(_TOOLSETS_MESSAGE)
    if _field(normalized, "tasks") is not None:
        raise ToolContractError(_TASKS_MESSAGE)
    _merge_context(normalized)
    _read_delivery_mode(normalized)
    if is_placeholder(normalized.get("action", ""), words=()):
        normalized.pop("action", None)
    for name, words in _PLACEHOLDERS.items():
        if name in normalized and is_placeholder(normalized[name], words):
            del normalized[name]
    return normalized


def _task_text_request(arguments: dict[str, Any]) -> dict[str, Any]:
    """Read a ``request`` string that is not a JSON object as the task itself."""
    result: dict[str, Any] = {}
    for key, item in arguments.items():
        if spelling(key) == "request" and isinstance(item, str) and not _is_json_object(item):
            key = next((name for name in _TASK_TEXT_KEYS if name not in arguments), key)
        result[key] = item
    return result


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _action_word(value: Any) -> Any:
    return _ACTION_SYNONYMS.get(value, value) if isinstance(value, str) else value


def _field(arguments: dict[str, Any], name: str) -> str | None:
    """Return the key spelled like ``name``, if the call carries one."""
    return next((key for key in arguments if spelling(key) == name), None)


def _merge_context(arguments: dict[str, Any]) -> None:
    """Append a Hermes-style ``context`` string to the task it belongs to."""
    key = _field(arguments, _CONTEXT)
    if key is None or not isinstance(arguments[key], str):
        return
    context = arguments.pop(key).strip()
    task = arguments.get("content")
    if not context:
        return
    if isinstance(task, str) and task.strip():
        arguments["content"] = f"{task}\n\nContext:\n{context}"
    else:
        arguments["content"] = context


def _read_delivery_mode(arguments: dict[str, Any]) -> None:
    """Fold ``blocking`` into ``background`` and read true/false words as booleans."""
    requested: list[Any] = []
    for key in [key for key in arguments if spelling(key) in (BACKGROUND_FIELD, _BLOCKING)]:
        value = _boolean(arguments.pop(key))
        if spelling(key) == _BLOCKING and isinstance(value, bool):
            value = not value
        requested.append(value)
    values = _distinct(requested)
    if len(values) > 1:
        raise ToolContractError(
            'Conflicting "background" and "blocking" values; provide one intended value.'
        )
    if values:
        arguments[BACKGROUND_FIELD] = values[0]


def _boolean(value: Any) -> Any:
    if isinstance(value, str):
        return _BOOLEAN_WORDS.get(value.strip().casefold(), value)
    return value


def _distinct(values: Iterable[Any]) -> list[Any]:
    distinct: list[Any] = []
    for value in values:
        if not any(value == seen and type(value) is type(seen) for seen in distinct):
            distinct.append(value)
    return distinct


__all__ = ["BACKGROUND_FIELD", "UNADVERTISED_PARAMETERS", "normalize_subagent_arguments"]
