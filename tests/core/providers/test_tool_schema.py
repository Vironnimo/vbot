"""Tests for Anthropic tool ``input_schema`` sanitization."""

from __future__ import annotations

import copy

from core.providers.tool_schema import sanitize_anthropic_tool_input_schema


class TestNullableUnionCollapse:
    """Nullable ``anyOf`` / ``oneOf`` unions collapse to the non-null branch."""

    def test_nullable_anyof_property_collapses_to_non_null_branch(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Optional name",
                }
            },
        }

        result = sanitize_anthropic_tool_input_schema(schema)

        assert result["properties"]["name"] == {
            "type": "string",
            "description": "Optional name",
        }

    def test_nullable_oneof_property_collapses(self):
        schema = {
            "type": "object",
            "properties": {"count": {"oneOf": [{"type": "integer"}, {"type": "null"}]}},
        }

        result = sanitize_anthropic_tool_input_schema(schema)

        assert result["properties"]["count"] == {"type": "integer"}

    def test_nested_nullable_union_collapses(self):
        schema = {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "properties": {
                        "tag": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                }
            },
        }

        result = sanitize_anthropic_tool_input_schema(schema)

        assert result["properties"]["filter"]["properties"]["tag"] == {"type": "string"}

    def test_meaningful_multi_branch_union_is_preserved(self):
        """A union with two non-null branches is not a nullable hint — keep it."""
        schema = {
            "type": "object",
            "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        }

        result = sanitize_anthropic_tool_input_schema(schema)

        assert result["properties"]["value"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


class TestTopLevelUnionStripping:
    """Root-level union keywords are stripped (Anthropic rejects them)."""

    def test_top_level_anyof_stripped_and_object_ensured(self):
        schema = {"anyOf": [{"type": "object"}, {"type": "null"}]}

        result = sanitize_anthropic_tool_input_schema(schema)

        assert "anyOf" not in result
        assert result["type"] == "object"
        assert result["properties"] == {}

    def test_top_level_allof_stripped_keeps_existing_object_fields(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "allOf": [{"required": ["a"]}],
        }

        result = sanitize_anthropic_tool_input_schema(schema)

        assert "allOf" not in result
        assert result["type"] == "object"
        assert result["properties"] == {"a": {"type": "string"}}


class TestPassThroughAndFallback:
    """Valid schemas pass through unchanged; junk degrades to an empty object."""

    def test_valid_object_schema_passes_through_without_mutating_input(self):
        schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        }
        original = copy.deepcopy(schema)

        result = sanitize_anthropic_tool_input_schema(schema)

        assert result == original
        assert schema == original  # the caller's registry entry is untouched

    def test_object_without_properties_gets_empty_properties(self):
        result = sanitize_anthropic_tool_input_schema({"type": "object"})

        assert result == {"type": "object", "properties": {}}

    def test_empty_or_non_dict_returns_empty_object_schema(self):
        assert sanitize_anthropic_tool_input_schema({}) == {"type": "object", "properties": {}}
        assert sanitize_anthropic_tool_input_schema(None) == {"type": "object", "properties": {}}
        assert sanitize_anthropic_tool_input_schema("object") == {
            "type": "object",
            "properties": {},
        }
