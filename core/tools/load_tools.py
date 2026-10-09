"""The ``load_tools`` Tool: definitions of On-demand Tools, loaded when the Agent needs them.

While an Agent's ``tool_loading`` switch is on (``core.tools.on_demand``), its
Tool list leaves its On-demand Tools out and the System Prompt lists them by
name and summary. ``load_tools`` returns the full definitions of the ones the
Agent names, as readable text; Chat then counts them as known for the rest of
the prompt epoch (``ToolContext.record_loaded_tools``). Loading changes no
permission: an On-demand Tool is callable before it is loaded.

The Tool is internal: it is never part of a Tool policy or ``tool.list``, and
Chat lists it exactly while the Agent loads Tools on demand.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.model_names import called_tool_name, model_tool_name
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayPart,
    ToolRegistry,
    tool_success,
)

LOAD_TOOLS_DESCRIPTION = (
    'Load the full definitions of Tools listed under "Tools Loaded on Demand" in your '
    "instructions, so you can call them. Pass every Tool the current task needs in one call."
)
LOAD_TOOLS_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of the Tools to load, exactly as listed.",
        }
    },
    "required": ["names"],
}
_RESULT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"content": {"type": "string"}},
    "required": ["content"],
    "additionalProperties": False,
}

# Field names other harnesses and Models use for the names to load; Claude
# Code's ToolSearch sends ``query: "select:a,b"``.
_FIELD_ALIASES = SpellingAliases(
    {"names": ("name", "tool", "tools", "tool_name", "tool_names", "query", "select")}
)
_CONTRACT = compile_tool_contract(
    name=LOAD_TOOLS_TOOL_NAME,
    input_schema=LOAD_TOOLS_PARAMETERS,
    require_closed_input=False,
)
# Tool names never contain commas or whitespace, so both separate names.
_NAME_SEPARATORS = re.compile(r"[,\s]+")
_SELECT_PREFIX = "select:"
_QUOTES = "\"'`"

_LOADED = "- {name}: loaded"
_ALREADY_AVAILABLE = "- {name}: already available; call it directly"
_NOT_LOADABLE = "- {name}: not available to load"
_LOADABLE_LIST = "Tools you can load: {names}."
_SECTION = "Tool: {name}\nDescription: {description}\nParameters (JSON Schema): {schema}"
_CALL_LOADED = "Call the loaded Tools by name with normal Tool calls."
_EMPTY_CALL = "Nothing was loaded: the call named no Tool."
_NONE_FOUND = "Nothing was loaded: no Tool named {names} can be loaded."
_ALL_AVAILABLE = "Every Tool you can use is already available; call it directly by name."
_RETRY = (
    'Call load_tools again with "names" set to the Tools the task needs from that list, '
    "for example {example}."
)


def register_load_tools_tool(registry: ToolRegistry) -> None:
    """Register the internal ``load_tools`` Tool."""

    registry.register(
        LOAD_TOOLS_TOOL_NAME,
        LOAD_TOOLS_DESCRIPTION,
        LOAD_TOOLS_PARAMETERS,
        _load_tools,
        internal=True,
        result_schema=_RESULT_SCHEMA,
        display=ToolDisplay(parts_builder=_display_parts),
        open_input_schema=True,
        argument_normalizer=_normalize_load_tools_arguments,
    )


def _load_tools(context: ToolContext, arguments: JsonObject) -> JsonObject:
    """Return the definitions of the requested On-demand Tools as readable text."""

    requested: list[str] = arguments.get("names") or []
    loadable = {
        name: definition
        for name, definition in context.loadable_tools.items()
        if context.can_call(name)
    }
    shown = [name for name in (context.offered_tools or ()) if context.can_call(name)]
    candidates = [*loadable, *(name for name in shown if name not in loadable)]
    if not requested:
        raise ToolContractError(_refusal(_EMPTY_CALL, loadable))

    lines: list[str] = []
    loaded: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        name = called_tool_name(raw, candidates)
        if name in loadable:
            if name in seen:
                continue
            seen.add(name)
            loaded.append(name)
            lines.append(_LOADED.format(name=model_tool_name(name)))
        elif name in shown:
            if name in seen:
                continue
            seen.add(name)
            lines.append(_ALREADY_AVAILABLE.format(name=model_tool_name(name)))
        elif raw not in unknown:
            unknown.append(raw)
            lines.append(_NOT_LOADABLE.format(name=raw))

    if unknown and len(unknown) == len(lines):
        names = _joined(json.dumps(name, ensure_ascii=False) for name in unknown)
        raise ToolContractError(_refusal(_NONE_FOUND.format(names=names), loadable))

    remaining = {name: definition for name, definition in loadable.items() if name not in seen}
    if unknown and remaining:
        lines.append(_loadable_sentence(remaining))
    parts = ["\n".join(lines)]
    parts.extend(_section(name, loadable[name]) for name in loaded)
    if loaded:
        parts.append(_CALL_LOADED)
        context.record_loaded_tools(loaded)
    return tool_success({"content": "\n\n".join(parts)})


def _section(name: str, definition: Mapping[str, Any]) -> str:
    """Return one loaded Tool's name, description and parameters."""

    schema = json.dumps(definition.get("parameters", {}), ensure_ascii=False, separators=(",", ":"))
    return _SECTION.format(
        name=model_tool_name(name),
        description=str(definition.get("description", "")).strip(),
        schema=schema,
    )


def _refusal(cause: str, loadable: Mapping[str, Any]) -> str:
    """Say that nothing was loaded, why, and which call loads what the Agent can load."""

    if not loadable:
        return f"{cause} {_ALL_AVAILABLE}"
    example = json.dumps({"names": _loadable_names(loadable)[:1]}, ensure_ascii=False)
    return f"{cause} {_loadable_sentence(loadable)} {_RETRY.format(example=example)}"


def _loadable_sentence(loadable: Mapping[str, Any]) -> str:
    """Name the Tools the Agent can load, sorted as the System Prompt lists them."""

    return _LOADABLE_LIST.format(names=", ".join(_loadable_names(loadable)))


def _loadable_names(loadable: Mapping[str, Any]) -> list[str]:
    return sorted(model_tool_name(name) for name in loadable)


def _joined(items: Any) -> str:
    """Join names as prose: ``a``, ``a or b``, ``a, b or c``."""

    names = list(items)
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} or {names[-1]}"


def _normalize_load_tools_arguments(arguments: Any) -> Any:
    """Read the names to load from the forms Models send.

    A bare list or text stands for ``names``; ``name``, ``tools``, ``query`` and
    similar fields name it too. Text holding several names, separated by
    commas, spaces or line breaks, is split, a ``select:`` prefix (Claude Code's
    ToolSearch) and quotes around a name are removed, and a JSON array written
    as text is read as that array. A call without names keeps an empty list, so
    the Tool can name the Tools there are to load.
    """

    bare_names = isinstance(arguments, str) and not arguments.lstrip().startswith("{")
    if bare_names or isinstance(arguments, list):
        arguments = {"names": arguments}
    repaired = normalize_call_arguments(
        _CONTRACT,
        arguments,
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"names": _names},
    )
    if isinstance(repaired, dict):
        repaired.setdefault("names", [])
    return repaired


def _names(value: Any) -> list[str]:
    """Return every Tool name *value* holds, in order."""

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return _names(parsed)
        names: list[str] = []
        for token in _NAME_SEPARATORS.split(text):
            name = token.strip(_QUOTES)
            if name.casefold().startswith(_SELECT_PREFIX):
                name = name[len(_SELECT_PREFIX) :].strip(_QUOTES)
            if name:
                names.append(name)
        return names
    if isinstance(value, list):
        return [name for item in value for name in _names(item)]
    if isinstance(value, dict):
        named = next((value[key] for key in ("name", "tool") if key in value), None)
        return _names(named) if named is not None else []
    if value is None:
        return []
    return [str(value)]


def _display_parts(arguments: JsonObject) -> Sequence[ToolDisplayPart]:
    """Show the Tools the call asked for."""

    try:
        normalized = _normalize_load_tools_arguments(arguments)
    except ValueError:
        return []
    names = normalized.get("names") if isinstance(normalized, dict) else None
    if not isinstance(names, list) or not names:
        return []
    shown = ", ".join(model_tool_name(str(name)) for name in names)
    return [ToolDisplayPart(shown, kind="identifier")]


__all__ = [
    "LOAD_TOOLS_DESCRIPTION",
    "LOAD_TOOLS_PARAMETERS",
    "register_load_tools_tool",
]
