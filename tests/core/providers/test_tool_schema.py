"""Tests for explicit canonical-to-Provider Tool-schema profiles."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from core.providers.tool_schema import (
    ANTHROPIC_MAX_STRICT_TOOLS,
    render_tool_definitions,
    render_tool_schema,
    sanitize_anthropic_tool_input_schema,
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _tool(name: str, schema: dict | None = None) -> dict:
    return {
        "name": name,
        "description": f"Call {name}.",
        "parameters": copy.deepcopy(schema or _SCHEMA),
    }


def test_anthropic_sanitizer_preserves_canonical_schema_without_mutation() -> None:
    original = copy.deepcopy(_SCHEMA)

    result = sanitize_anthropic_tool_input_schema(_SCHEMA)

    assert result == original
    assert result is not _SCHEMA
    assert original == _SCHEMA


@pytest.mark.parametrize("schema", [{}, None, "object", {"anyOf": [{"type": "object"}]}])
def test_anthropic_sanitizer_rejects_non_object_roots(schema: object) -> None:
    with pytest.raises(ValueError, match="object root"):
        sanitize_anthropic_tool_input_schema(schema)


def test_openai_strict_render_preserves_canonical_nullable_optionals() -> None:
    schema = copy.deepcopy(_SCHEMA)
    schema["properties"]["limit"]["type"] = ["integer", "null"]

    decision = render_tool_schema(schema, profile="openai_strict")

    assert decision.strict is True
    assert decision.reason is None
    assert decision.schema["required"] == ["path", "limit"]
    assert decision.schema["properties"]["limit"]["type"] == ["integer", "null"]
    assert decision.schema["additionalProperties"] is False
    assert _SCHEMA["required"] == ["path"]


def test_openai_strict_downgrades_nonnullable_optional_without_weakening_schema() -> None:
    decision = render_tool_schema(_SCHEMA, profile="openai_strict")

    assert decision.strict is False
    assert decision.reason == "optional_property_not_nullable:/properties/limit"
    assert decision.schema == _SCHEMA


def test_openai_strict_downgrades_unsupported_keywords_without_weakening_schema() -> None:
    schema = {**_SCHEMA, "not": {"required": ["limit"]}}

    decision = render_tool_schema(schema, profile="openai_strict")

    assert decision.strict is False
    assert decision.reason == "unsupported_keyword:not"
    assert decision.schema == schema


def test_anthropic_strict_is_all_or_none_for_the_request_set() -> None:
    eligible = render_tool_definitions([_tool("one"), _tool("two")], profile="anthropic_strict")
    oversized = render_tool_definitions(
        [_tool(f"tool_{index}") for index in range(ANTHROPIC_MAX_STRICT_TOOLS + 1)],
        profile="anthropic_strict",
    )

    assert all(tool["strict"] is True for tool in eligible)
    assert all("strict" not in tool for tool in oversized)


def test_best_effort_profile_preserves_schema_and_emits_no_internal_fields() -> None:
    rendered = render_tool_definitions([_tool("read")], profile="best_effort")

    assert rendered == [_tool("read")]
    assert rendered[0]["parameters"] is not _SCHEMA
