"""Call shapes of the connection Tool (``mcp_<id>``): spelling, aliases, and refusals.

The connection Tool's own schema is vBot's, so this owner may read common
proxy-tool spellings of its fields (``tool``/``name`` for target, ``args`` or
``input`` for arguments) and verbs for its actions. It never touches a target's
arguments, whose names belong to the remote server. When a field cannot apply
to the action, or the action is missing and the readings differ in effect, it
refuses before any request and names the corrected call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools.contracts import ToolContractError, compile_tool_contract

from ._definitions import MCP_PARAMETERS

_FIELDS = tuple(MCP_PARAMETERS["properties"])

APPLICABLE = {
    "search": ("query", "kind", "offset", "limit"),
    "describe": ("target",),
    "call": ("target", "arguments"),
    "read": ("result_id", "pointer", "offset", "limit", "fields"),
}
_REQUIRED = {"describe": "target", "call": "target", "read": "result_id"}
# A corrected call longer than this is described in words rather than repeated.
_CORRECTED_CHARACTERS = 1500


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_object(value: Any) -> bool:
    return isinstance(value, dict) or (isinstance(value, str) and value.strip().startswith("{"))


def _is_count(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and value.strip().isdigit()
    )


def _is_result_id(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("res_")


def _is_target(value: Any) -> bool:
    return _is_text(value) and not _is_result_id(value)


# Other spellings of a field, used only while the field itself is absent.
_ALIASES: dict[str, tuple[str, Callable[[Any], bool]]] = {
    **dict.fromkeys(("tool", "toolname", "targetname", "name"), ("target", _is_target)),
    **dict.fromkeys(
        (
            "args",
            "argument",
            "input",
            "inputs",
            "params",
            "parameters",
            "toolinput",
            "toolargs",
            "toolarguments",
        ),
        ("arguments", _is_object),
    ),
    **dict.fromkeys(("q", "search", "keywords", "keyword", "terms"), ("query", _is_text)),
    **dict.fromkeys(("type", "category"), ("kind", _is_text)),
    **dict.fromkeys(("resultid", "result"), ("result_id", _is_result_id)),
    **dict.fromkeys(("path", "jsonpointer"), ("pointer", _is_text)),
    **dict.fromkeys(("pagesize", "maxresults"), ("limit", _is_count)),
    **dict.fromkeys(("start", "skip"), ("offset", _is_count)),
}

_ACTIONS = {
    **dict.fromkeys(("browse", "list", "find", "discover"), "search"),
    **dict.fromkeys(("schema", "info", "details", "show"), "describe"),
    **dict.fromkeys(("run", "invoke", "execute", "use", "calltool"), "call"),
    **dict.fromkeys(("readresult", "page"), "read"),
}

_KINDS = {
    "tools": "tool",
    "resources": "resource",
    "templates": "template",
    "resourcetemplate": "template",
    "resourcetemplates": "template",
    "prompts": "prompt",
    "operations": "operation",
}


def _spelling(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.casefold())


def _call(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _alias(key: str, value: Any) -> tuple[str, Callable[[Any], bool]] | None:
    spelling = _spelling(key)
    if spelling == "id":
        # A saved result id reads as result_id; any other id names a target.
        return ("result_id", _is_result_id) if _is_result_id(value) else ("target", _is_target)
    if spelling in {_spelling(field) for field in _FIELDS}:
        return None
    return _ALIASES.get(spelling)


def _lift_aliases(arguments: dict[str, Any]) -> dict[str, Any]:
    spellings = {_spelling(key) for key in arguments}
    present = {field for field in _FIELDS if _spelling(field) in spellings}
    lifted: dict[str, Any] = {}
    for key, value in arguments.items():
        alias = _alias(key, value)
        if alias is not None and alias[0] not in present and alias[1](value):
            present.add(alias[0])
            lifted[alias[0]] = value
        else:
            lifted[key] = value
    return lifted


def _action(value: Any) -> Any:
    return _ACTIONS.get(_spelling(value), value) if isinstance(value, str) else value


def _kind(value: Any) -> Any:
    return _KINDS.get(_spelling(value), value) if isinstance(value, str) else value


def _pointer(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip().removeprefix("#")
    if text in {"", "/"}:
        return ""
    return text if text.startswith("/") else "/" + text


def _fields(value: Any) -> Any:
    if isinstance(value, str) and "," in value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


class _Browse:
    """Normalize one connection Tool's call before contract validation."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.contract = compile_tool_contract(
            name=tool, input_schema=MCP_PARAMETERS, require_closed_input=False
        )

    def __call__(self, arguments: Any) -> Any:
        if isinstance(arguments, dict):
            arguments = _lift_aliases(arguments)
        repaired = normalize_call_arguments(
            self.contract,
            arguments,
            enum_fields=("action", "kind"),
            field_normalizers={
                "action": _action,
                "kind": _kind,
                "pointer": _pointer,
                "fields": _fields,
                "target": lambda value: value.strip() if isinstance(value, str) else value,
                "arguments": self._arguments,
            },
            empty_as_omitted=("query", "kind", "target", "result_id", "arguments"),
        )
        return self._complete(repaired) if isinstance(repaired, dict) else repaired

    def refuse(self, action: str | None, reason: str) -> ToolContractError:
        subject = f"{self.tool} {action}" if action in APPLICABLE else self.tool
        return ToolContractError(f"{subject} was not run: {reason}")

    def _arguments(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError as error:
            detail = getattr(error, "msg", "invalid JSON")
            position = getattr(error, "pos", None)
            where = f" at character {position}" if position is not None else ""
            raise self.refuse(
                "call",
                f"arguments is text that is not a JSON object ({detail}{where}). Send "
                'arguments as an object, such as "arguments":{"name":"value"}.',
            ) from None
        if not isinstance(parsed, dict):
            raise self.refuse("call", 'arguments must be an object, such as {"name":"value"}.')
        return parsed

    def _complete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action")
        if action is None:
            action = self._infer_action(arguments)
            arguments = {"action": action, **arguments}
        if action not in APPLICABLE:
            raise self.refuse(
                None,
                f"action must be one of {', '.join(APPLICABLE)}. Start with "
                f"{_call({'action': 'search'})} to see what this connection offers.",
            )
        applicable = APPLICABLE[action]
        for field in [key for key in arguments if key != "action" and key not in applicable]:
            value = arguments[field]
            if _is_empty(value) or (field == "offset" and value == 0):
                del arguments[field]
        unknown = [key for key in arguments if key not in _FIELDS]
        if unknown:
            raise self.refuse(action, self._unknown_reason(action, arguments, unknown))
        extra = [key for key in arguments if key != "action" and key not in applicable]
        if extra:
            raise self.refuse(action, self._inapplicable_reason(action, arguments, extra))
        required = _REQUIRED.get(action)
        if required is not None and required not in arguments:
            raise self.refuse(action, self._missing_reason(action, required))
        return arguments

    def _infer_action(self, arguments: dict[str, Any]) -> str:
        if "result_id" in arguments:
            return "read"
        if "target" not in arguments:
            return "search"
        if "arguments" in arguments:
            return "call"
        target = arguments["target"]
        raise self.refuse(
            None,
            "action is missing, and describing a target differs from calling it. Describe it "
            f"with {_call({'action': 'describe', 'target': target})}, or call it with "
            f"{_call({'action': 'call', 'target': target, 'arguments': {}})} and its arguments.",
        )

    def _unknown_reason(self, action: str, arguments: dict[str, Any], unknown: list[str]) -> str:
        names = ", ".join(unknown)
        verb = "is not a field" if len(unknown) == 1 else "are not fields"
        if action == "call":
            moved = {key: arguments[key] for key in unknown}
            inner = arguments.get("arguments", {})
            if isinstance(inner, dict) and not set(moved) & set(inner):
                corrected = {key: value for key, value in arguments.items() if key not in unknown}
                corrected["arguments"] = {**inner, **moved}
                example = _call(corrected)
                if len(example) > _CORRECTED_CHARACTERS:
                    example = f"move {names} into arguments and call again"
                return (
                    f"{names} {verb} of {self.tool}. The target's own arguments go inside "
                    f"arguments: {example}."
                )
            return (
                f"{names} {verb} of {self.tool}. The target's own arguments go inside "
                "arguments, once each."
            )
        return f"{names} {verb} of {self.tool}. {action} takes {', '.join(APPLICABLE[action])}."

    def _inapplicable_reason(self, action: str, arguments: dict[str, Any], extra: list[str]) -> str:
        names = " and ".join(extra)
        verb = "does" if len(extra) == 1 else "do"
        corrected = {key: value for key, value in arguments.items() if key not in extra}
        reason = (
            f"{names} {verb} not apply to {action}, which takes {', '.join(APPLICABLE[action])}."
        )
        if action == "describe" and "arguments" in extra and "target" in arguments:
            call = _call(
                {
                    "action": "call",
                    "target": arguments["target"],
                    "arguments": arguments["arguments"],
                }
            )
            if len(call) > _CORRECTED_CHARACTERS:
                call = 'the same call with "action":"call"'
            return (
                f"{reason} Describe the target with {_call(corrected)}, or call it with "
                f"these arguments: {call}."
            )
        if action == "search" and "target" in extra:
            describe = {"action": "describe", "target": arguments["target"]}
            return (
                f"{reason} Describe that target with {_call(describe)}, or send {_call(corrected)}."
            )
        example = _call(corrected)
        if len(example) > _CORRECTED_CHARACTERS:
            example = f"the same call without {names}"
        return f"{reason} Send {example}."

    def _missing_reason(self, action: str, required: str) -> str:
        if required == "result_id":
            return (
                "result_id is missing. Use the result_id of an earlier result from this "
                "connection, as shown in its continue or read call."
            )
        return (
            "target is missing. Use a target from search, such as tool:name:..., or a tool "
            f"name; find them with {_call({'action': 'search'})}."
        )


def browse_normalizer(tool: str) -> Callable[[Any], Any]:
    """Return the ``argument_normalizer`` of the connection Tool named *tool*."""
    return _Browse(tool)
