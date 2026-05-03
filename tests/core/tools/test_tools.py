"""Tests for tool registry, allowlist filtering, and dispatch."""

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from core.tools import (
    DuplicateToolError,
    Tool,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolRegistry,
)

JsonObject = dict[str, Any]

READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}
WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
}


def read_file_handler(arguments: JsonObject) -> JsonObject:
    return {"content": f"read {arguments['path']}"}


async def write_file_handler(arguments: JsonObject) -> JsonObject:
    return {"written": arguments["path"], "bytes": len(arguments["content"])}


def register_read_file(registry: ToolRegistry) -> Tool:
    return registry.register(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        parameters=READ_FILE_SCHEMA,
        handler=read_file_handler,
    )


def register_write_file(registry: ToolRegistry) -> Tool:
    return registry.register(
        name="write_file",
        description="Write UTF-8 text to a workspace file.",
        parameters=WRITE_FILE_SCHEMA,
        handler=write_file_handler,
    )


class TestTool:
    def test_fields_are_stored(self) -> None:
        tool = Tool(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=READ_FILE_SCHEMA,
            handler=read_file_handler,
        )

        assert tool.name == "read_file"
        assert tool.description == "Read a UTF-8 text file from the workspace."
        assert tool.parameters == READ_FILE_SCHEMA
        assert tool.handler is read_file_handler

    def test_frozen_raises_on_attribute_assignment(self) -> None:
        tool = Tool(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=READ_FILE_SCHEMA,
            handler=read_file_handler,
        )

        with pytest.raises(FrozenInstanceError):
            tool.name = "changed"  # type: ignore[misc]


class TestToolRegistryRegister:
    def test_register_returns_tool_and_get_finds_it(self) -> None:
        registry = ToolRegistry()

        tool = register_read_file(registry)

        assert registry.get("read_file") is tool

    def test_register_copies_parameter_schema(self) -> None:
        registry = ToolRegistry()
        parameters = {"type": "object"}

        tool = registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=parameters,
            handler=read_file_handler,
        )
        parameters["type"] = "array"

        assert tool.parameters == {"type": "object"}

    def test_duplicate_name_raises(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        with pytest.raises(DuplicateToolError, match="read_file"):
            register_read_file(registry)

    def test_empty_name_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="name"):
            registry.register("", "Description", READ_FILE_SCHEMA, read_file_handler)

    def test_empty_description_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="description"):
            registry.register("read_file", "", READ_FILE_SCHEMA, read_file_handler)

    def test_non_object_parameters_raise_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="parameters"):
            registry.register(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                [],  # type: ignore[arg-type]
                read_file_handler,
            )

    def test_non_callable_handler_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="handler"):
            registry.register(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                READ_FILE_SCHEMA,
                None,  # type: ignore[arg-type]
            )


class TestToolRegistryAllowlistFiltering:
    def test_empty_registry_lists_no_tools(self) -> None:
        registry = ToolRegistry()

        assert registry.list_tools(["*"]) == []

    def test_none_allowlist_returns_all_tools_sorted(self) -> None:
        registry = ToolRegistry()
        register_write_file(registry)
        register_read_file(registry)

        tools = registry.list_tools()

        assert [tool.name for tool in tools] == ["read_file", "write_file"]

    def test_wildcard_allowlist_returns_all_tools_sorted(self) -> None:
        registry = ToolRegistry()
        register_write_file(registry)
        register_read_file(registry)

        tools = registry.list_tools(["*"])

        assert [tool.name for tool in tools] == ["read_file", "write_file"]

    def test_empty_allowlist_returns_no_tools(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        assert registry.list_tools([]) == []

    def test_explicit_allowlist_returns_matching_tools_only(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        register_write_file(registry)

        tools = registry.list_tools(["write_file"])

        assert [tool.name for tool in tools] == ["write_file"]

    def test_unknown_allowlisted_tool_is_ignored_for_exposure(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        tools = registry.list_tools(["missing_tool"])

        assert tools == []


class TestToolRegistryDefinitions:
    def test_provider_definitions_include_schema_for_allowed_tools(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        register_write_file(registry)

        definitions = registry.provider_definitions(["read_file"])

        assert definitions == [
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the workspace.",
                "parameters": READ_FILE_SCHEMA,
            }
        ]

    def test_provider_definitions_copy_schema(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        definitions = registry.provider_definitions(["read_file"])
        definitions[0]["parameters"]["type"] = "array"

        assert registry.get("read_file").parameters["type"] == "object"

    def test_prompt_definitions_include_name_and_description_only(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        definitions = registry.prompt_definitions(["read_file"])

        assert definitions == [
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the workspace.",
            }
        ]

    def test_empty_allowlist_omits_tools_from_both_definition_sets(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        assert registry.provider_definitions([]) == []
        assert registry.prompt_definitions([]) == []


class TestToolRegistryDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_sync_handler(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        result = await registry.dispatch("read_file", {"path": "SOUL.md"}, ["*"])

        assert result == {"content": "read SOUL.md"}

    @pytest.mark.asyncio
    async def test_dispatch_async_handler(self) -> None:
        registry = ToolRegistry()
        register_write_file(registry)

        result = await registry.dispatch(
            "write_file",
            {"path": "SOUL.md", "content": "hello"},
            ["write_file"],
        )

        assert result == {"written": "SOUL.md", "bytes": 5}

    @pytest.mark.asyncio
    async def test_dispatch_unknown_tool_raises(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ToolNotFoundError, match="missing_tool"):
            await registry.dispatch("missing_tool", {}, ["*"])

    @pytest.mark.asyncio
    async def test_dispatch_disallowed_tool_raises(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        with pytest.raises(ToolNotAllowedError, match="read_file"):
            await registry.dispatch("read_file", {"path": "SOUL.md"}, [])

    @pytest.mark.asyncio
    async def test_dispatch_with_none_allowlist_allows_tool(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        result = await registry.dispatch("read_file", {"path": "SOUL.md"})

        assert result == {"content": "read SOUL.md"}

    @pytest.mark.asyncio
    async def test_dispatch_non_object_arguments_raise_value_error(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        with pytest.raises(ValueError, match="arguments"):
            await registry.dispatch("read_file", [], ["*"])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_dispatch_non_object_result_raises_value_error(self) -> None:
        registry = ToolRegistry()

        def invalid_handler(arguments: JsonObject) -> JsonObject:
            return "not an object"  # type: ignore[return-value]

        registry.register(
            "invalid_tool",
            "Return an invalid result for testing.",
            {"type": "object"},
            invalid_handler,
        )

        with pytest.raises(ValueError, match="return"):
            await registry.dispatch("invalid_tool", {}, ["*"])


class TestPublicExports:
    def test_registry_exports_from_package_root(self) -> None:
        registry = ToolRegistry()

        tool = register_read_file(registry)

        assert tool.name == "read_file"
