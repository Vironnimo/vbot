"""Explicit Provider profiles for canonical Tool schemas."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator

from core.utils.logging import get_logger

JsonObject = dict[str, Any]
ToolSchemaProfile = Literal["openai_strict", "anthropic_strict", "best_effort"]
_LOGGER = get_logger("providers.tool_schema")

OPENAI_MAX_TOOLS = 128
ANTHROPIC_MAX_STRICT_TOOLS = 20
ANTHROPIC_MAX_OPTIONAL_PARAMETERS = 24
ANTHROPIC_MAX_UNION_PARAMETERS = 16

_STRICT_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "allOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "unevaluatedProperties",
    }
)


@dataclass(frozen=True)
class ToolSchemaRenderDecision:
    """One canonical-to-wire Tool schema decision."""

    profile_id: ToolSchemaProfile
    schema: JsonObject
    strict: bool
    reason: str | None = None


def render_tool_definitions(
    tools: Sequence[Mapping[str, Any]],
    *,
    profile: ToolSchemaProfile,
) -> list[JsonObject]:
    """Render generic Tool definitions for one verified Provider profile."""
    normalized = [_normalized_tool(tool) for tool in tools]
    if profile == "openai_strict":
        allow_strict = len(normalized) <= OPENAI_MAX_TOOLS
        rendered: list[JsonObject] = []
        for tool in normalized:
            decision = render_tool_schema(
                tool["parameters"],
                profile=profile,
                allow_strict=allow_strict,
            )
            if not decision.strict:
                _LOGGER.debug(
                    "Tool schema downgraded (profile=%s tool=%s reason=%s)",
                    profile,
                    tool["name"],
                    decision.reason,
                )
            definition = {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": decision.schema,
            }
            if decision.strict:
                definition["strict"] = True
            rendered.append(definition)
        return rendered

    if profile == "anthropic_strict":
        strict_reason = _anthropic_set_downgrade_reason(normalized)
        if strict_reason is not None:
            _LOGGER.debug(
                "Tool schema set downgraded (profile=%s tools=%d reason=%s)",
                profile,
                len(normalized),
                strict_reason,
            )
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": copy.deepcopy(tool["parameters"]),
                **({"strict": True} if strict_reason is None else {}),
            }
            for tool in normalized
        ]

    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": copy.deepcopy(tool["parameters"]),
        }
        for tool in normalized
    ]


def render_tool_schema(
    schema: Mapping[str, Any],
    *,
    profile: ToolSchemaProfile,
    allow_strict: bool = True,
) -> ToolSchemaRenderDecision:
    """Render one canonical input schema without weakening its runtime meaning."""
    canonical = copy.deepcopy(dict(schema))
    if profile == "best_effort":
        return ToolSchemaRenderDecision(profile, canonical, False, "profile_best_effort")
    if canonical.get("type") != "object":
        return ToolSchemaRenderDecision(profile, canonical, False, "root_not_object")
    unsupported = _first_unsupported_keyword(canonical)
    if unsupported is not None:
        return ToolSchemaRenderDecision(
            profile,
            canonical,
            False,
            f"unsupported_keyword:{unsupported}",
        )
    if _has_unclosed_fixed_object(canonical):
        return ToolSchemaRenderDecision(
            profile,
            canonical,
            False,
            "fixed_object_not_closed",
        )
    if not allow_strict:
        return ToolSchemaRenderDecision(profile, canonical, False, "request_tool_limit")
    if profile == "openai_strict":
        incompatible_optional = _first_nonnullable_optional_property(canonical)
        if incompatible_optional is not None:
            return ToolSchemaRenderDecision(
                profile,
                canonical,
                False,
                f"optional_property_not_nullable:{incompatible_optional}",
            )
        return ToolSchemaRenderDecision(
            profile,
            _openai_strict_schema(canonical),
            True,
        )
    return ToolSchemaRenderDecision(profile, canonical, True)


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


def _openai_strict_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_openai_strict_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rendered = {key: _openai_strict_schema(value) for key, value in node.items()}
    if rendered.get("type") == "object" and isinstance(rendered.get("properties"), dict):
        properties = rendered["properties"]
        previously_required = set(rendered.get("required", []))
        rendered["additionalProperties"] = False
        rendered["required"] = list(properties)
        for name, property_schema in list(properties.items()):
            if name not in previously_required:
                properties[name] = _nullable_schema(property_schema)
    return rendered


def _first_nonnullable_optional_property(
    node: Any,
    *,
    path: tuple[str | int, ...] = (),
) -> str | None:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = set(node.get("required", []))
            for name, property_schema in properties.items():
                if (
                    name not in required
                    and isinstance(property_schema, dict)
                    and not Draft202012Validator(property_schema).is_valid(None)
                ):
                    return _format_schema_path((*path, "properties", name))
        for key, value in node.items():
            incompatible = _first_nonnullable_optional_property(
                value,
                path=(*path, key),
            )
            if incompatible is not None:
                return incompatible
    elif isinstance(node, list):
        for index, value in enumerate(node):
            incompatible = _first_nonnullable_optional_property(
                value,
                path=(*path, index),
            )
            if incompatible is not None:
                return incompatible
    return None


def _format_schema_path(path: Sequence[str | int]) -> str:
    return "".join(
        f"[{segment}]"
        if isinstance(segment, int)
        else f"/{segment.replace('~', '~0').replace('/', '~1')}"
        for segment in path
    )


def _nullable_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    rendered = copy.deepcopy(schema)
    schema_type = rendered.get("type")
    if isinstance(schema_type, str):
        if schema_type != "null":
            rendered["type"] = [schema_type, "null"]
        return rendered
    if isinstance(schema_type, list):
        if "null" not in schema_type:
            rendered["type"] = [*schema_type, "null"]
        return rendered
    variants = rendered.get("anyOf")
    if isinstance(variants, list):
        if not any(isinstance(item, dict) and item.get("type") == "null" for item in variants):
            rendered["anyOf"] = [*variants, {"type": "null"}]
        return rendered
    return {"anyOf": [rendered, {"type": "null"}]}


def _anthropic_set_downgrade_reason(tools: Sequence[JsonObject]) -> str | None:
    if len(tools) > ANTHROPIC_MAX_STRICT_TOOLS:
        return "request_tool_limit"
    optional_parameters = 0
    union_parameters = 0
    for tool in tools:
        schema = tool["parameters"]
        unsupported = _first_unsupported_keyword(schema)
        if unsupported is not None:
            return f"unsupported_keyword:{unsupported}"
        if _has_unclosed_fixed_object(schema):
            return "fixed_object_not_closed"
        optional_parameters += _count_optional_properties(schema)
        union_parameters += _count_union_parameters(schema)
    if optional_parameters > ANTHROPIC_MAX_OPTIONAL_PARAMETERS:
        return "request_optional_parameter_limit"
    if union_parameters > ANTHROPIC_MAX_UNION_PARAMETERS:
        return "request_union_parameter_limit"
    return None


def _first_unsupported_keyword(node: Any) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key in _STRICT_UNSUPPORTED_KEYWORDS:
                return key
            nested = _first_unsupported_keyword(value)
            if nested is not None:
                return nested
    elif isinstance(node, list):
        for value in node:
            nested = _first_unsupported_keyword(value)
            if nested is not None:
                return nested
    return None


def _has_unclosed_fixed_object(node: Any) -> bool:
    if isinstance(node, dict):
        if (
            node.get("type") == "object"
            and isinstance(node.get("properties"), dict)
            and node.get("additionalProperties") is not False
        ):
            return True
        return any(_has_unclosed_fixed_object(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_unclosed_fixed_object(value) for value in node)
    return False


def _count_optional_properties(node: Any) -> int:
    count = 0
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = set(node.get("required", []))
            count += len(set(properties).difference(required))
        count += sum(_count_optional_properties(value) for value in node.values())
    elif isinstance(node, list):
        count += sum(_count_optional_properties(value) for value in node)
    return count


def _count_union_parameters(node: Any) -> int:
    count = 0
    if isinstance(node, dict):
        if isinstance(node.get("anyOf"), list) or isinstance(node.get("oneOf"), list):
            count += 1
        schema_type = node.get("type")
        if isinstance(schema_type, list) and len(schema_type) > 1:
            count += 1
        count += sum(_count_union_parameters(value) for value in node.values())
    elif isinstance(node, list):
        count += sum(_count_union_parameters(value) for value in node)
    return count


__all__ = [
    "ANTHROPIC_MAX_OPTIONAL_PARAMETERS",
    "ANTHROPIC_MAX_STRICT_TOOLS",
    "ANTHROPIC_MAX_UNION_PARAMETERS",
    "OPENAI_MAX_TOOLS",
    "ToolSchemaRenderDecision",
    "ToolSchemaProfile",
    "render_tool_definitions",
    "render_tool_schema",
    "sanitize_anthropic_tool_input_schema",
]
