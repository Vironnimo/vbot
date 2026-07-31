"""Provider-neutral Tool schemas that never enable strict Tool calling."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, Literal

JsonObject = dict[str, Any]
ToolSchemaProfile = Literal["explicit_non_strict", "omit_strict"]


def render_tool_definitions(
    tools: Sequence[Mapping[str, Any]],
    *,
    profile: ToolSchemaProfile,
) -> list[JsonObject]:
    """Render Tool definitions without ever enabling Provider strict mode.

    Responses-style APIs may normalize an omitted ``strict`` flag into strict
    mode. Their profile therefore emits ``strict: false`` explicitly. Provider
    contracts without that field receive the canonical schema without it.
    """
    normalized = [_normalized_tool(tool) for tool in tools]
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": copy.deepcopy(tool["parameters"]),
            **({"strict": False} if profile == "explicit_non_strict" else {}),
        }
        for tool in normalized
    ]


def sanitize_anthropic_tool_input_schema(
    schema: Any,
    *,
    tool_name: str = "",
) -> JsonObject:
    """Return an unchanged object-root schema or reject an invalid definition."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        suffix = f" for {tool_name!r}" if tool_name else ""
        raise ValueError(f"Anthropic Tool input schema must have an object root{suffix}")
    return copy.deepcopy(schema)


def _normalized_tool(tool: Mapping[str, Any]) -> JsonObject:
    name = tool.get("name")
    description = tool.get("description")
    parameters = tool.get("parameters")
    function = tool.get("function")
    if isinstance(function, Mapping):
        if not isinstance(name, str) or not name:
            name = function.get("name")
        if not isinstance(description, str) or not description:
            description = function.get("description")
        if not isinstance(parameters, Mapping):
            parameters = function.get("parameters")
    if not isinstance(name, str) or not name:
        raise ValueError("Tool definition name must be a non-empty string")
    if not isinstance(description, str) or not description:
        raise ValueError(f"Tool definition description is required: {name}")
    if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
        raise ValueError(f"Tool definition parameters must have an object root: {name}")
    return {
        "name": name,
        "description": description,
        "parameters": copy.deepcopy(dict(parameters)),
    }


__all__ = [
    "ToolSchemaProfile",
    "render_tool_definitions",
    "sanitize_anthropic_tool_input_schema",
]
