"""Tests for Provider Tool-schema rendering without strict mode."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from core.providers.tool_schema import render_tool_definitions, sanitize_anthropic_tool_input_schema

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


def test_openai_profile_preserves_canonical_schema_and_explicitly_disables_strict() -> None:
    rendered = render_tool_definitions([_tool("read")], profile="openai_non_strict")

    assert rendered == [{**_tool("read"), "strict": False}]
    assert rendered[0]["parameters"]["required"] == ["path"]
    assert rendered[0]["parameters"] is not _SCHEMA


@pytest.mark.parametrize("profile", ("openai_non_strict", "best_effort"))
def test_caller_cannot_enable_strict_mode(profile: str) -> None:
    tool = {**_tool("read"), "strict": True}

    [rendered] = render_tool_definitions(
        [tool],
        profile=profile,  # type: ignore[arg-type]
    )

    assert rendered.get("strict") is not True
    if profile == "openai_non_strict":
        assert rendered["strict"] is False
    else:
        assert "strict" not in rendered


def test_best_effort_profile_preserves_schema_and_emits_no_internal_fields() -> None:
    rendered = render_tool_definitions([_tool("read")], profile="best_effort")

    assert rendered == [_tool("read")]
    assert rendered[0]["parameters"] is not _SCHEMA
