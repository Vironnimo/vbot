"""Tests for tool registry, envelopes, and execution scheduling."""

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    DuplicateToolError,
    Tool,
    ToolCall,
    ToolContext,
    ToolDisplay,
    ToolExecutionConfig,
    ToolExecutor,
    ToolNoteHook,
    ToolRegistry,
    is_tool_result_envelope,
    tool_failure,
    tool_success,
)
from core.tools.availability import effective_agent_allowed_tools, sanitize_configured_allowed_tools

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
    def test_nesting_depth_defaults_to_zero(self) -> None:
        context = make_context()

        assert context.nesting_depth == 0

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

    def test_add_note_uses_hook_when_present(self) -> None:
        notes: list[str] = []
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
            note_hook=notes.append,
        )

        context.add_note("reminder")

        assert notes == ["reminder"]

    def test_add_note_without_hook_does_nothing(self) -> None:
        context = make_context()

        context.add_note("reminder")

        assert context.note_hook is None


class TestToolContextCancelHooks:
    def test_on_cancel_invokes_registration_hook_with_callback(self) -> None:
        registered: list[Callable[[], None]] = []

        def registration_hook(callback: Callable[[], None]) -> None:
            registered.append(callback)

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
            cancel_registration_hook=registration_hook,
        )

        def cancel_callback() -> None:
            pass

        context.on_cancel(cancel_callback)

        assert registered == [cancel_callback]

    def test_on_cancel_without_hook_is_a_safe_noop(self) -> None:
        context = make_context()

        context.on_cancel(lambda: None)

        assert context.cancel_registration_hook is None

    def test_was_cancelled_by_user_returns_hook_result(self) -> None:
        cancel_state = {"user_cancelled": True}
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
            cancel_check_hook=lambda: cancel_state["user_cancelled"],
        )

        assert context.was_cancelled_by_user() is True

        cancel_state["user_cancelled"] = False

        assert context.was_cancelled_by_user() is False

    def test_was_cancelled_by_user_returns_false_without_hook(self) -> None:
        context = make_context()

        assert context.was_cancelled_by_user() is False
        assert context.cancel_check_hook is None


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
        assert tool.display == ToolDisplay()

    def test_display_builds_payload_from_summary_fields(self) -> None:
        display = ToolDisplay(
            summary_fields=("pattern", "path"),
            hidden_argument_keys=("content",),
        )

        payload = display.to_payload({"pattern": "TODO", "path": "src", "content": "large body"})

        assert payload == {
            "summary": "TODO · src",
            "hidden_argument_keys": ["content"],
        }

    def test_display_omits_empty_argument_summary(self) -> None:
        display = ToolDisplay(summary_fields=("path",))

        assert display.to_payload({}) == {"summary": "", "hidden_argument_keys": []}

    def test_display_rejects_bare_string_summary_fields(self) -> None:
        with pytest.raises(ValueError, match="summary_fields"):
            ToolDisplay(summary_fields="path")  # type: ignore[arg-type]

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

    def test_non_display_metadata_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="display"):
            registry.register(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                READ_FILE_SCHEMA,
                read_file_handler,
                display=object(),  # type: ignore[arg-type]
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


class TestAgentToolAvailability:
    def test_sanitizes_configured_memory_tool(self) -> None:
        assert sanitize_configured_allowed_tools(["read_file", "memory"]) == ["read_file"]

    def test_memory_mode_adds_memory_to_explicit_allowlist(self) -> None:
        allowed_tools = effective_agent_allowed_tools(
            ["read_file"],
            "agent",
            registered_tool_names=["memory", "read_file", "write_file"],
        )

        assert allowed_tools == ["read_file", "memory"]

    def test_memory_off_removes_memory_from_wildcard_allowlist(self) -> None:
        allowed_tools = effective_agent_allowed_tools(
            ["*"],
            "off",
            registered_tool_names=["memory", "read_file", "write_file"],
        )

        assert allowed_tools == ["read_file", "write_file"]


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
        registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=READ_FILE_SCHEMA,
            handler=read_file_handler,
            display=ToolDisplay(summary_fields=("path",)),
        )

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
    def test_display_for_call_uses_registered_tool_display(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="write_file",
            description="Write UTF-8 text to a workspace file.",
            parameters=WRITE_FILE_SCHEMA,
            handler=write_file_handler,
            display=ToolDisplay(summary_fields=("path",), hidden_argument_keys=("content",)),
        )

        payload = registry.display_for_call(
            "write_file",
            {"path": "notes.md", "content": "large body"},
        )

        assert payload == {
            "summary": "notes.md",
            "hidden_argument_keys": ["content"],
        }

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
    async def test_dispatch_runs_sync_handler_on_event_loop_thread(self) -> None:
        registry = ToolRegistry()
        loop_thread_id = threading.get_ident()
        seen_thread_ids: list[int] = []

        def sync_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_thread_ids.append(threading.get_ident())
            return tool_success({"thread_id": seen_thread_ids[-1]})

        registry.register(
            "sync_tool",
            "Run a sync handler and return its thread id.",
            {"type": "object"},
            sync_handler,
        )

        result = await registry.dispatch(make_context("sync_tool"), {}, ["*"])

        assert seen_thread_ids == [loop_thread_id]
        assert result == tool_success({"thread_id": loop_thread_id})

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

    @pytest.mark.asyncio
    async def test_internal_tool_dispatch_ignores_empty_allowlist(self) -> None:
        registry = ToolRegistry()
        registry.register(
            "internal_tool",
            "Internal tool for testing.",
            {"type": "object"},
            lambda _context, _arguments: tool_success({"called": True}),
            internal=True,
        )

        result = await registry.dispatch(make_context("internal_tool"), {}, [])

        assert result == tool_success({"called": True})

    @pytest.mark.asyncio
    async def test_empty_allowlist_still_blocks_normal_tool_dispatch(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        with pytest.raises(Exception, match="Tool not allowed: read_file"):
            await registry.dispatch(make_context("read_file"), {"path": "SOUL.md"}, [])


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_nesting_depth_flows_from_config_to_context(self) -> None:
        registry = ToolRegistry()
        seen_depths: list[int] = []

        def depth_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_depths.append(context.nesting_depth)
            return tool_success({"nesting_depth": context.nesting_depth})

        registry.register(
            "depth",
            "Return the current nesting depth for testing.",
            {"type": "object"},
            depth_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="depth", arguments={})],
            ToolExecutionConfig(
                agent_id="agent-1",
                session_id="session-1",
                run_id="run-1",
                workspace=Path("workspace"),
                app_root=Path("app"),
                data_root=Path("data"),
                allowed_tools=["*"],
                nesting_depth=3,
            ),
        )

        assert seen_depths == [3]
        assert results == [tool_success({"nesting_depth": 3})]

    @pytest.mark.asyncio
    async def test_cancel_hooks_flow_from_config_to_context_through_execute_one(self) -> None:
        registry = ToolRegistry()
        registered_callbacks: list[Callable[[], None]] = []
        user_cancelled = False

        def cancel_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            def cancel_callback() -> None:
                nonlocal user_cancelled
                user_cancelled = True

            context.on_cancel(cancel_callback)
            return tool_success({"was_cancelled": context.was_cancelled_by_user()})

        registry.register(
            "cancel_probe",
            "Probe cancel hooks wired through ToolExecutionConfig.",
            {"type": "object"},
            cancel_handler,
        )
        executor = ToolExecutor(registry)

        def registration_hook(callback: Callable[[], None]) -> None:
            registered_callbacks.append(callback)

        cancel_check_calls = 0

        def cancel_check_hook() -> bool:
            nonlocal cancel_check_calls
            cancel_check_calls += 1
            return True

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="cancel_probe", arguments={})],
            ToolExecutionConfig(
                agent_id="agent-1",
                session_id="session-1",
                run_id="run-1",
                workspace=Path("workspace"),
                app_root=Path("app"),
                data_root=Path("data"),
                allowed_tools=["*"],
                cancel_registration_hook=registration_hook,
                cancel_check_hook=cancel_check_hook,
            ),
        )

        assert len(registered_callbacks) == 1
        assert cancel_check_calls == 1
        assert results == [tool_success({"was_cancelled": True})]

        registered_callbacks[0]()
        assert user_cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_hooks_default_to_safe_noop_in_executor(self) -> None:
        registry = ToolRegistry()
        seen_values: dict[str, bool] = {}

        def cancel_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            context.on_cancel(lambda: None)
            seen_values["was_cancelled"] = context.was_cancelled_by_user()
            return tool_success({"ok": True})

        registry.register(
            "cancel_default",
            "Probe cancel hooks default to no-op when config has none.",
            {"type": "object"},
            cancel_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="cancel_default", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert seen_values == {"was_cancelled": False}
        assert results == [tool_success({"ok": True})]

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
    async def test_handler_exception_becomes_failed_result(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = ToolRegistry()

        def failing_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            raise RuntimeError("boom")

        registry.register("failing", "Fail for testing.", {"type": "object"}, failing_handler)
        executor = ToolExecutor(registry)

        with caplog.at_level(logging.ERROR, logger="vbot.tools"):
            results = await executor.execute_many(
                [ToolCall(id="call-1", name="failing", arguments={})],
                make_execution_config(allowed_tools=["*"]),
            )

        assert results == [tool_failure("tool_execution_error", "boom")]
        crash_records = [
            record
            for record in caplog.records
            if record.levelno == logging.ERROR and "crashed unexpectedly" in record.getMessage()
        ]
        assert crash_records, "expected an error log for the crashing tool handler"
        assert crash_records[0].exc_info is not None

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

    @pytest.mark.asyncio
    async def test_global_limit_is_shared_across_executor_instances(self) -> None:
        registry = ToolRegistry()
        active_count = 0
        max_active_count = 0
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def globally_limited_handler(
            context: ToolContext,
            arguments: JsonObject,
        ) -> JsonObject:
            nonlocal active_count, max_active_count
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            if context.tool_call_id == "call-1":
                first_started.set()
            await release.wait()
            active_count -= 1
            return tool_success({"id": context.tool_call_id})

        registry.register(
            "global_limit",
            "Globally limited tool for testing.",
            {"type": "object"},
            globally_limited_handler,
        )
        first_executor = ToolExecutor(registry, per_run_limit=1, global_limit=1)
        second_executor = ToolExecutor(registry, per_run_limit=1, global_limit=1)

        first_task = asyncio.create_task(
            first_executor.execute_many(
                [ToolCall(id="call-1", name="global_limit", arguments={})],
                make_execution_config(allowed_tools=["*"]),
            )
        )
        await first_started.wait()
        second_task = asyncio.create_task(
            second_executor.execute_many(
                [ToolCall(id="call-2", name="global_limit", arguments={})],
                make_execution_config(allowed_tools=["*"]),
            )
        )
        await asyncio.sleep(0.01)

        assert max_active_count == 1

        release.set()
        assert await first_task == [tool_success({"id": "call-1"})]
        assert await second_task == [tool_success({"id": "call-2"})]
        assert max_active_count == 1


class TestPublicExports:
    def test_registry_exports_from_package_root(self) -> None:
        registry = ToolRegistry()

        tool = register_read_file(registry)

        assert tool.name == "read_file"

    def test_note_hook_type_exports_from_package_root(self) -> None:
        def note_hook(content: str) -> None:
            assert content == "reminder"

        exported_hook: ToolNoteHook = note_hook

        exported_hook("reminder")
