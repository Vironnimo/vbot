"""Tests for tool registry, envelopes, and execution scheduling."""

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    DuplicateToolError,
    Tool,
    ToolCall,
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolRegistry,
    is_tool_result_envelope,
    tool_failure,
    tool_success,
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


def make_context(tool_name: str = "read_file", tool_call_id: str = "call_1") -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_call_index=0,
        workspace=Path("workspace"),
        app_root=Path("app"),
        data_root=Path("data"),
    )


def make_execution_config(
    *,
    allowed_tools: list[str] | None = None,
    workspace: Path = Path("workspace"),
) -> ToolExecutionConfig:
    return ToolExecutionConfig(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        workspace=workspace,
        app_root=Path("app"),
        data_root=Path("data"),
        allowed_tools=allowed_tools,
    )


def read_file_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return tool_success(
        {
            "content": f"read {arguments['path']}",
            "tool_call_id": context.tool_call_id,
        }
    )


async def write_file_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return tool_success(
        {
            "written": arguments["path"],
            "bytes": len(arguments["content"]),
            "workspace": str(context.workspace),
        }
    )


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


class TestToolContext:
    @pytest.mark.asyncio
    async def test_emit_uses_async_hook(self) -> None:
        events: list[tuple[str, JsonObject]] = []

        async def emit_hook(event_type: str, payload: JsonObject) -> None:
            events.append((event_type, payload))

        context = ToolContext(
            agent_id="agent-1",
            session_id="session-1",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="read_file",
            tool_call_index=0,
            workspace=Path("workspace"),
            app_root=Path("app"),
            data_root=Path("data"),
            emit_hook=emit_hook,
            cancellation_hook=lambda: True,
        )

        await context.emit("tool_call_started", {"id": "call-1"})

        assert events == [("tool_call_started", {"id": "call-1"})]
        assert context.is_cancelled() is True

    def test_is_cancelled_defaults_to_false(self) -> None:
        context = make_context()

        assert context.is_cancelled() is False


class TestToolEnvelope:
    def test_success_envelope_shape_is_valid(self) -> None:
        result = tool_success({"content": "hello"})

        assert result == {
            "ok": True,
            "error": None,
            "data": {"content": "hello"},
            "artifacts": [],
        }
        assert is_tool_result_envelope(result) is True

    def test_failure_envelope_shape_is_valid(self) -> None:
        result = tool_failure("not_found", "File not found")

        assert result == {
            "ok": False,
            "error": {"code": "not_found", "message": "File not found"},
            "data": None,
            "artifacts": [],
        }
        assert is_tool_result_envelope(result) is True

    def test_invalid_envelope_is_rejected(self) -> None:
        assert is_tool_result_envelope({"ok": True, "data": {}}) is False


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

    def test_provider_definitions_do_not_expose_handler_or_context(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        definition = registry.provider_definitions(["read_file"])[0]

        assert set(definition) == {"name", "description", "parameters"}

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
    async def test_dispatch_passes_context_to_sync_handler(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        result = await registry.dispatch(make_context(), {"path": "SOUL.md"}, ["*"])

        assert result == tool_success({"content": "read SOUL.md", "tool_call_id": "call_1"})

    @pytest.mark.asyncio
    async def test_dispatch_async_handler(self) -> None:
        registry = ToolRegistry()
        register_write_file(registry)

        result = await registry.dispatch(
            make_context("write_file"),
            {"path": "SOUL.md", "content": "hello"},
            ["write_file"],
        )

        assert result == tool_success({"written": "SOUL.md", "bytes": 5, "workspace": "workspace"})

    @pytest.mark.asyncio
    async def test_dispatch_non_envelope_result_raises_value_error(self) -> None:
        registry = ToolRegistry()

        def invalid_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            return {"content": "not enveloped"}

        registry.register(
            "invalid_tool",
            "Return an invalid result for testing.",
            {"type": "object"},
            invalid_handler,
        )

        with pytest.raises(ValueError, match="envelope"):
            await registry.dispatch(make_context("invalid_tool"), {}, ["*"])


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_unknown_tool_becomes_failed_result(self) -> None:
        executor = ToolExecutor(ToolRegistry())

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="missing_tool", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results == [tool_failure("tool_not_found", "Tool not found: missing_tool")]

    @pytest.mark.asyncio
    async def test_disallowed_tool_becomes_failed_result(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="read_file", arguments={"path": "SOUL.md"})],
            make_execution_config(allowed_tools=[]),
        )

        assert results == [tool_failure("tool_not_allowed", "Tool not allowed: read_file")]

    @pytest.mark.asyncio
    async def test_invalid_arguments_become_failed_result(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="read_file", arguments=[])],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results == [
            tool_failure("invalid_arguments", "Tool arguments must be a JSON object")
        ]

    @pytest.mark.asyncio
    async def test_handler_exception_becomes_failed_result(self) -> None:
        registry = ToolRegistry()

        def failing_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            raise RuntimeError("boom")

        registry.register("failing", "Fail for testing.", {"type": "object"}, failing_handler)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="failing", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results == [tool_failure("tool_execution_error", "boom")]

    @pytest.mark.asyncio
    async def test_parallel_execution_overlaps_and_preserves_order(self) -> None:
        registry = ToolRegistry()
        started: list[str] = []
        release_second = asyncio.Event()

        async def slow_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            started.append(context.tool_call_id)
            if context.tool_call_id == "call-1":
                await release_second.wait()
            else:
                release_second.set()
            return tool_success({"id": context.tool_call_id})

        registry.register("slow", "Slow tool for testing.", {"type": "object"}, slow_handler)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [
                ToolCall(id="call-1", name="slow", arguments={}),
                ToolCall(id="call-2", name="slow", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )

        assert started == ["call-1", "call-2"]
        assert results == [tool_success({"id": "call-1"}), tool_success({"id": "call-2"})]

    @pytest.mark.asyncio
    async def test_same_tool_can_run_multiple_times_in_parallel(self) -> None:
        registry = ToolRegistry()
        active_count = 0
        max_active_count = 0
        release = asyncio.Event()

        async def same_tool_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            nonlocal active_count, max_active_count
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            if max_active_count == 2:
                release.set()
            await release.wait()
            active_count -= 1
            return tool_success({"id": context.tool_call_id})

        registry.register("same", "Same tool for testing.", {"type": "object"}, same_tool_handler)
        executor = ToolExecutor(registry, per_run_limit=2, global_limit=2)

        results = await executor.execute_many(
            [
                ToolCall(id="call-1", name="same", arguments={}),
                ToolCall(id="call-2", name="same", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )

        assert max_active_count == 2
        assert results == [tool_success({"id": "call-1"}), tool_success({"id": "call-2"})]

    @pytest.mark.asyncio
    async def test_semaphore_queues_overflow_with_lowered_limits(self) -> None:
        registry = ToolRegistry()
        active_count = 0
        max_active_count = 0

        async def queued_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            nonlocal active_count, max_active_count
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            await asyncio.sleep(0.01)
            active_count -= 1
            return tool_success({"id": context.tool_call_id})

        registry.register("queued", "Queued tool for testing.", {"type": "object"}, queued_handler)
        executor = ToolExecutor(registry, per_run_limit=1, global_limit=1)

        results = await executor.execute_many(
            [
                ToolCall(id="call-1", name="queued", arguments={}),
                ToolCall(id="call-2", name="queued", arguments={}),
                ToolCall(id="call-3", name="queued", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )

        assert max_active_count == 1
        assert results == [
            tool_success({"id": "call-1"}),
            tool_success({"id": "call-2"}),
            tool_success({"id": "call-3"}),
        ]


class TestPublicExports:
    def test_registry_exports_from_package_root(self) -> None:
        registry = ToolRegistry()

        tool = register_read_file(registry)

        assert tool.name == "read_file"
