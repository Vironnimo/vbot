"""Tests for tool registry, envelopes, and execution scheduling."""

import ast
import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    DuplicateToolError,
    Tool,
    ToolCall,
    ToolContext,
    ToolContractError,
    ToolDefinitionProfile,
    ToolDefinitionProfileContext,
    ToolDisplay,
    ToolDisplayField,
    ToolDisplayPart,
    ToolExecutionConfig,
    ToolExecutor,
    ToolNotAllowedError,
    ToolNoteHook,
    ToolPromptBlockRegistry,
    ToolRegistry,
    is_tool_result_envelope,
    result_count_fact_builder,
    tool_failure,
    tool_is_ready,
    tool_success,
)
from core.tools.tools import run_tool_worker

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_tool_worker_offloads_and_settles_mutation_before_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []

    def blocking_mutation() -> str:
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return "done"

    loop_thread = threading.get_ident()
    task = asyncio.create_task(run_tool_worker(blocking_mutation))
    assert await asyncio.to_thread(started.wait, 2)
    assert worker_threads != [loop_thread]

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}
WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
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
        vbot_root=Path("app"),
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
        vbot_root=Path("app"),
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

    def test_effective_cwd_falls_back_to_workspace_without_project_cwd(self) -> None:
        # Default identity behavior: no project cwd means tools resolve against
        # the workspace exactly as before this field existed.
        context = make_context()

        assert context.cwd is None
        assert context.effective_cwd == Path("workspace")

    def test_effective_cwd_uses_project_cwd_when_set(self) -> None:
        context = ToolContext(
            agent_id="agent-1",
            session_id="session-1",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="read_file",
            tool_call_index=0,
            workspace=Path("workspace"),
            vbot_root=Path("app"),
            data_root=Path("data"),
            cwd=Path("repo"),
        )

        assert context.effective_cwd == Path("repo")

    def test_resolve_path_uses_effective_cwd_for_relative_path(self, tmp_path: Path) -> None:
        context = ToolContext(
            agent_id="agent",
            session_id="session",
            run_id="run",
            tool_call_id="call",
            tool_name="read",
            tool_call_index=0,
            workspace=tmp_path / "workspace",
            vbot_root=tmp_path / "app",
            data_root=tmp_path / "data",
            cwd=tmp_path / "repo",
        )

        assert context.resolve_path("src/main.py") == (tmp_path / "repo" / "src/main.py").resolve()

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
            vbot_root=Path("app"),
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
            vbot_root=Path("app"),
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
            vbot_root=Path("app"),
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
            vbot_root=Path("app"),
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

    def test_failure_envelope_carries_retry_signal_inside_error(self) -> None:
        result = tool_failure(
            "request_error",
            "HTTP 503 while fetching URL",
            retryable=True,
            attempts_made=4,
        )

        assert result == {
            "ok": False,
            "error": {
                "code": "request_error",
                "message": "HTTP 503 while fetching URL",
                "retryable": True,
                "attempts_made": 4,
            },
            "data": None,
            "artifacts": [],
        }
        # The retry signal lives inside error, so the top-level key set is intact.
        assert is_tool_result_envelope(result) is True

    def test_failure_envelope_omits_unset_retry_signal(self) -> None:
        result = tool_failure("validation_error", "bad input")

        assert set(result["error"]) == {"code", "message"}
        assert is_tool_result_envelope(result) is True

    def test_failure_envelope_allows_retryable_false_without_attempts(self) -> None:
        result = tool_failure("validation_error", "bad input", retryable=False)

        assert result["error"] == {
            "code": "validation_error",
            "message": "bad input",
            "retryable": False,
        }
        assert is_tool_result_envelope(result) is True

    def test_failure_envelope_rejects_non_bool_retryable(self) -> None:
        with pytest.raises(ValueError, match="retryable"):
            tool_failure("x", "y", retryable="yes")  # type: ignore[arg-type]

    @pytest.mark.parametrize("attempts", [-1, True, 1.5])
    def test_failure_envelope_rejects_invalid_attempts_made(self, attempts: object) -> None:
        with pytest.raises(ValueError, match="attempts_made"):
            tool_failure("x", "y", attempts_made=attempts)  # type: ignore[arg-type]

    def test_envelope_rejects_unknown_error_keys(self) -> None:
        assert (
            is_tool_result_envelope(
                {
                    "ok": False,
                    "error": {"code": "x", "message": "y", "unexpected": 1},
                    "data": None,
                    "artifacts": [],
                }
            )
            is False
        )

    def test_envelope_rejects_negative_attempts_made(self) -> None:
        assert (
            is_tool_result_envelope(
                {
                    "ok": False,
                    "error": {"code": "x", "message": "y", "attempts_made": -1},
                    "data": None,
                    "artifacts": [],
                }
            )
            is False
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
        assert tool.display == ToolDisplay()

    def test_display_builds_payload_from_summary_fields(self) -> None:
        display = ToolDisplay(
            summary_fields=("pattern", "path"),
            hidden_argument_keys=("content",),
        )

        payload = display.to_payload({"pattern": "TODO", "path": "src", "content": "large body"})

        assert payload == {
            "version": 1,
            "summary": "TODO · src",
            "hidden_argument_keys": ["content"],
            "primary": [
                {
                    "kind": "text",
                    "value": "TODO · src",
                    "full_value": "TODO · src",
                    "truncate": "end",
                    "tooltip": "truncated",
                    "max_characters": 64,
                    "quote": False,
                    "copyable": False,
                }
            ],
            "facts": [],
        }

    def test_display_omits_empty_argument_summary(self) -> None:
        display = ToolDisplay(summary_fields=("path",))

        assert display.to_payload({}) == {
            "version": 1,
            "summary": "",
            "hidden_argument_keys": [],
            "primary": [],
            "facts": [],
        }

    def test_display_prefers_nonblank_structured_candidate(self) -> None:
        display = ToolDisplay(
            primary_candidates=(
                ToolDisplayField("description", kind="description", quote=True),
                ToolDisplayField("command", kind="command"),
            )
        )

        described = display.to_payload(
            {"description": "  run tests  ", "command": "python -m pytest"}
        )
        fallback = display.to_payload({"description": "  ", "command": "python -m pytest"})

        assert described["primary"][0]["value"] == "run tests"
        assert described["primary"][0]["kind"] == "description"
        assert described["primary"][0]["quote"] is True
        assert fallback["primary"][0]["value"] == "python -m pytest"
        assert fallback["primary"][0]["kind"] == "command"

    def test_display_builds_computed_semantic_parts(self) -> None:
        display = ToolDisplay(
            parts_builder=lambda _arguments: (
                ToolDisplayPart("status", truncate="never", tooltip="none"),
                ToolDisplayPart("process-session-one", kind="identifier", truncate="middle"),
            )
        )

        payload = display.to_payload({"action": "status"})

        assert payload["summary"] == "status · process-session-one"
        assert payload["primary"][0]["truncate"] == "never"
        assert payload["primary"][1]["kind"] == "identifier"
        assert payload["primary"][1]["truncate"] == "middle"

    def test_display_resolves_complete_path_against_call_cwd(self, tmp_path: Path) -> None:
        context = ToolContext(
            agent_id="agent-1",
            session_id="session-1",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="read",
            tool_call_index=0,
            workspace=tmp_path / "workspace",
            cwd=tmp_path / "project",
            vbot_root=tmp_path,
            data_root=tmp_path / "data",
        )
        display = ToolDisplay(
            primary_candidates=(
                ToolDisplayField(
                    "path",
                    kind="path",
                    truncate="start",
                    tooltip="always",
                    copyable=True,
                ),
            )
        )

        payload = display.to_payload({"path": "src/main.py"}, context=context)

        assert payload["primary"][0] == {
            "kind": "path",
            "value": "src/main.py",
            "full_value": (tmp_path / "project" / "src" / "main.py").as_posix(),
            "truncate": "start",
            "tooltip": "always",
            "max_characters": 64,
            "quote": False,
            "copyable": True,
        }

    def test_context_records_validated_presentation_count(self) -> None:
        context = make_context()

        context.add_display_count(10, "matches", at_least=True)

        assert context.presentation_facts == [
            {"kind": "count", "value": 10, "unit": "matches", "at_least": True}
        ]

    def test_context_records_added_and_removed_line_facts_in_display_order(self) -> None:
        context = make_context()

        context.add_display_line_changes(added=4, removed=0)

        assert context.presentation_facts == [
            {"kind": "line_change", "change": "added", "value": 4},
            {"kind": "line_change", "change": "removed", "value": 0},
        ]

    def test_display_normalizes_line_range_and_change_facts(self) -> None:
        display = ToolDisplay(
            fact_builder=lambda _arguments, _result: (
                {"kind": "line_range", "start": 170, "end": 280},
                {"kind": "line_change", "change": "added", "value": 3},
                {"kind": "line_change", "change": "removed", "value": 2},
            )
        )

        assert display.to_payload({})["facts"] == [
            {"kind": "line_range", "start": 170, "end": 280},
            {"kind": "line_change", "change": "added", "value": 3},
            {"kind": "line_change", "change": "removed", "value": 2},
        ]

    def test_result_count_fact_builder_counts_successful_lists_and_pagination(self) -> None:
        display = ToolDisplay(
            fact_builder=result_count_fact_builder(
                "items",
                when_arguments={"action": "list"},
                at_least_field="has_more",
            )
        )

        payload = display.to_payload(
            {"action": "list"},
            result=tool_success({"items": [{"id": 1}, {"id": 2}], "has_more": True}),
        )

        assert payload["facts"] == [
            {"kind": "count", "value": 2, "unit": "results", "at_least": True}
        ]

    def test_result_count_fact_builder_ignores_failures_and_other_actions(self) -> None:
        display = ToolDisplay(
            fact_builder=result_count_fact_builder("count", when_arguments={"action": "list"})
        )

        assert (
            display.to_payload({"action": "list"}, result=tool_failure("failed", "no count"))[
                "facts"
            ]
            == []
        )
        assert (
            display.to_payload({"action": "add"}, result=tool_success({"count": 4}))["facts"] == []
        )

    def test_result_count_fact_builder_does_not_mark_empty_page_as_lower_bound(self) -> None:
        display = ToolDisplay(
            fact_builder=result_count_fact_builder("items", at_least_field="has_more")
        )

        assert display.to_payload({}, result=tool_success({"items": [], "has_more": True}))[
            "facts"
        ] == [{"kind": "count", "value": 0, "unit": "results", "at_least": False}]

    @pytest.mark.parametrize("count", (-1, True, "4", None))
    def test_result_count_fact_builder_rejects_invalid_result_counts(self, count: Any) -> None:
        display = ToolDisplay(fact_builder=result_count_fact_builder("count"))

        assert display.to_payload({}, result=tool_success({"count": count}))["facts"] == []

    def test_display_rejects_bare_string_summary_fields(self) -> None:
        with pytest.raises(ValueError, match="summary_fields"):
            ToolDisplay(summary_fields="path")  # type: ignore[arg-type]

    def test_every_builtin_registration_has_an_explicit_display_profile(self) -> None:
        tools_dir = Path(__file__).parents[3] / "core" / "tools"
        missing: list[str] = []
        for source_path in tools_dir.glob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "register" or not isinstance(node.func.value, ast.Name):
                    continue
                if node.func.value.id != "registry":
                    continue
                keyword_names = {keyword.arg for keyword in node.keywords}
                is_tool_registration = len(node.args) >= 4 or "handler" in keyword_names
                if is_tool_registration and "display" not in keyword_names:
                    missing.append(f"{source_path.name}:{node.lineno}")

        assert missing == []

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


def test_registry_preserves_declarative_tool_relationship_metadata() -> None:
    registry = ToolRegistry()

    tool = registry.register(
        "session_read",
        "Read a Session.",
        READ_FILE_SCHEMA,
        read_file_handler,
        family="sessions",
        activation="follows",
        activation_source="session_search",
        constraints=("identity_agent",),
    )

    assert tool.family == "sessions"
    assert tool.activation == "follows"
    assert tool.activation_source == "session_search"
    assert tool.constraints == ("identity_agent",)


def test_registry_owns_family_labels_and_rejects_unknown_membership() -> None:
    registry = ToolRegistry()
    family = registry.register_family(
        "extension:weather:forecast",
        "Weather Forecast",
        extension="weather",
    )

    tool = registry.register(
        "weather_today",
        "Read today's forecast.",
        READ_FILE_SCHEMA,
        read_file_handler,
        family=family.id,
        extension="weather",
    )

    assert tool.family == "extension:weather:forecast"
    assert tool.family_label == "Weather Forecast"
    assert registry.get_family(family.id) == family

    with pytest.raises(ValueError, match="not registered"):
        registry.register(
            "weather_tomorrow",
            "Read tomorrow's forecast.",
            READ_FILE_SCHEMA,
            read_file_handler,
            family="extension:weather:missing",
        )


def test_registry_never_unregisters_builtin_family_metadata() -> None:
    registry = ToolRegistry()

    registry.unregister_family("files")

    assert registry.get_family("files").label == "Files"


def test_registry_rejects_invalid_activation_metadata() -> None:
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="activation"):
        registry.register(
            "read_file",
            "Read a file.",
            READ_FILE_SCHEMA,
            read_file_handler,
            activation="mystery",
        )


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

    def test_configuration_profile_is_stable_and_shared_by_provider_and_prompt(self) -> None:
        registry = ToolRegistry()

        def resolve(context: ToolDefinitionProfileContext) -> ToolDefinitionProfile:
            return ToolDefinitionProfile(
                key=f"agent:{context.agent_id}:readme-only",
                description="Read this Agent's README file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "enum": ["README.md"],
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )

        registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=READ_FILE_SCHEMA,
            handler=read_file_handler,
            definition_profile_resolver=resolve,
        )
        context = ToolDefinitionProfileContext(agent_id="agent-1")

        first = registry.provider_definitions(["read_file"], profile_context=context)
        second = registry.provider_definitions(["read_file"], profile_context=context)
        prompt = registry.prompt_definitions(["read_file"], profile_context=context)

        assert first == second
        assert first[0]["parameters"]["properties"]["path"]["enum"] == ["README.md"]
        assert prompt == [
            {
                "name": "read_file",
                "description": "Read this Agent's README file.",
            }
        ]
        first[0]["parameters"]["properties"]["path"]["enum"].append("SECRET.md")
        assert registry.provider_definitions(
            ["read_file"],
            profile_context=context,
        )[0]["parameters"]["properties"]["path"]["enum"] == ["README.md"]

    def test_configuration_profile_can_hide_tool_for_one_agent(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=READ_FILE_SCHEMA,
            handler=read_file_handler,
            definition_profile_resolver=lambda context: (
                ToolDefinitionProfile(
                    key="enabled",
                    description="Read a UTF-8 text file from the workspace.",
                    parameters=READ_FILE_SCHEMA,
                )
                if context.agent_id == "enabled-agent"
                else None
            ),
        )

        assert (
            registry.provider_definitions(
                ["read_file"],
                profile_context=ToolDefinitionProfileContext(agent_id="disabled-agent"),
            )
            == []
        )
        assert (
            registry.prompt_definitions(
                ["read_file"],
                profile_context=ToolDefinitionProfileContext(agent_id="disabled-agent"),
            )
            == []
        )

    def test_empty_allowlist_omits_tools_from_both_definition_sets(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        assert registry.provider_definitions([]) == []
        assert registry.prompt_definitions([]) == []

    def test_session_scoped_tool_requires_grant_and_overrides_allowlist(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="history",
            description="Read this Session's earlier original records.",
            parameters={"type": "object"},
            handler=read_file_handler,
            session_scoped=True,
        )

        assert registry.provider_definitions(["*"]) == []
        assert registry.prompt_definitions([]) == []
        assert [
            definition["name"]
            for definition in registry.provider_definitions([], session_grants=["history"])
        ] == ["history"]
        assert [
            definition["name"]
            for definition in registry.prompt_definitions([], session_grants=["history"])
        ] == ["history"]

    def test_configurable_listing_can_exclude_session_scoped_tools(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        registry.register(
            name="history",
            description="Read this Session's earlier original records.",
            parameters={"type": "object"},
            handler=read_file_handler,
            session_scoped=True,
        )

        assert [tool.name for tool in registry.list_tools()] == ["history", "read_file"]
        assert [tool.name for tool in registry.list_tools(include_session_scoped=False)] == [
            "read_file"
        ]


class TestToolReadiness:
    def test_tool_without_predicate_is_ready(self) -> None:
        registry = ToolRegistry()
        tool = register_read_file(registry)

        assert tool.ready is None
        assert tool_is_ready(tool) is True

    def test_not_ready_tool_hidden_from_model_facing_surfaces_but_not_list_tools(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=lambda: False,
        )

        # Registered and visible in a plain list, but filtered from the
        # model-facing surfaces (which default to ready_only=True).
        assert [tool.name for tool in registry.list_tools()] == ["gated"]
        assert registry.list_tools(ready_only=True) == []
        assert registry.provider_definitions(["gated"]) == []
        assert registry.prompt_definitions(["gated"]) == []

    def test_ready_predicate_true_keeps_tool_visible(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=lambda: True,
        )

        assert [tool.name for tool in registry.list_tools(ready_only=True)] == ["gated"]
        assert [definition["name"] for definition in registry.provider_definitions(["gated"])] == [
            "gated"
        ]

    def test_raising_predicate_counts_as_not_ready_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = ToolRegistry()

        def boom() -> bool:
            raise RuntimeError("predicate exploded")

        tool = registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=boom,
        )

        with caplog.at_level(logging.WARNING):
            assert tool_is_ready(tool) is False

        assert registry.list_tools(ready_only=True) == []
        assert any("readiness predicate raised" in record.getMessage() for record in caplog.records)

    def test_register_rejects_non_callable_ready(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError):
            registry.register(
                name="gated",
                description="A gated tool.",
                parameters={"type": "object"},
                handler=read_file_handler,
                ready="nope",  # type: ignore[arg-type]
            )

    def test_dispatch_of_not_ready_tool_returns_envelope_without_running_handler(self) -> None:
        registry = ToolRegistry()
        called: list[bool] = []

        def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            called.append(True)
            return tool_success({})

        registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=handler,
            ready=lambda: False,
        )

        result = asyncio.run(registry.dispatch(make_context("gated"), {}))

        assert result["ok"] is False
        assert result["error"]["code"] == "tool_not_ready"
        assert result["error"]["retryable"] is False
        assert called == []

    def test_flipping_backing_state_makes_tool_reappear_without_reregistration(self) -> None:
        registry = ToolRegistry()
        token = {"value": ""}
        registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=lambda: bool(token["value"]),
        )

        assert registry.list_tools(ready_only=True) == []

        token["value"] = "present"

        assert [tool.name for tool in registry.list_tools(ready_only=True)] == ["gated"]
        assert [definition["name"] for definition in registry.provider_definitions(["gated"])] == [
            "gated"
        ]


class TestToolRegistryDispatch:
    @pytest.mark.asyncio
    async def test_session_scoped_dispatch_checks_grant_before_allowlist(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="history",
            description="Read this Session's earlier original records.",
            parameters={"type": "object"},
            handler=lambda _context, _arguments: tool_success({}),
            session_scoped=True,
        )
        executor = ToolExecutor(registry)

        unavailable = await executor.execute_many(
            [ToolCall(id="call-1", name="history", arguments={})],
            make_execution_config(allowed_tools=[]),
        )
        denied = await executor.execute_many(
            [ToolCall(id="call-2", name="history", arguments={})],
            replace(
                make_execution_config(allowed_tools=[]),
                session_tool_grants=("history",),
            ),
        )
        granted = await executor.execute_many(
            [ToolCall(id="call-3", name="history", arguments={})],
            replace(
                make_execution_config(allowed_tools=["history"]),
                session_tool_grants=("history",),
            ),
        )

        assert unavailable[0]["error"]["code"] == "history_unavailable"
        assert denied[0]["error"]["code"] == "tool_not_allowed"
        assert granted[0]["ok"] is True

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
            "version": 1,
            "summary": "notes.md",
            "hidden_argument_keys": ["content"],
            "primary": [
                {
                    "kind": "text",
                    "value": "notes.md",
                    "full_value": "notes.md",
                    "truncate": "end",
                    "tooltip": "truncated",
                    "max_characters": 64,
                    "quote": False,
                    "copyable": False,
                }
            ],
            "facts": [],
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

        with pytest.raises(ToolNotAllowedError):
            await registry.dispatch(make_context("read_file"), {"path": "SOUL.md"}, [])


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_exact_provider_contract_flows_to_dispatch_validation(self) -> None:
        registry = ToolRegistry()
        handler_calls: list[JsonObject] = []

        def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
            handler_calls.append(arguments)
            return tool_success({})

        registry.register(
            "read_file",
            "Read a UTF-8 text file from the workspace.",
            READ_FILE_SCHEMA,
            handler,
        )
        definitions = [
            {
                "name": "read_file",
                "description": "Read only the public README.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "enum": ["README.md"],
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            }
        ]
        contracts = registry.contracts_for_provider_definitions(definitions)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="read_file", arguments={"path": "SECRET.md"})],
            replace(
                make_execution_config(allowed_tools=["read_file"]),
                input_contracts=contracts,
            ),
        )

        assert results[0]["error"]["code"] == "invalid_arguments"
        assert handler_calls == []
        with pytest.raises(ToolContractError):
            contracts["read_file"].validate_arguments({"path": "SECRET.md"})

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
                vbot_root=Path("app"),
                data_root=Path("data"),
                allowed_tools=["*"],
                nesting_depth=3,
            ),
        )

        assert seen_depths == [3]
        assert results == [tool_success({"nesting_depth": 3})]

    @pytest.mark.asyncio
    async def test_cwd_flows_from_config_to_context(self) -> None:
        registry = ToolRegistry()
        seen_cwds: list[Path] = []

        def cwd_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_cwds.append(context.effective_cwd)
            return tool_success({"cwd": str(context.effective_cwd)})

        registry.register(
            "cwd",
            "Return the effective working directory for testing.",
            {"type": "object"},
            cwd_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="cwd", arguments={})],
            ToolExecutionConfig(
                agent_id="agent-1",
                session_id="session-1",
                run_id="run-1",
                workspace=Path("workspace"),
                vbot_root=Path("app"),
                data_root=Path("data"),
                cwd=Path("repo"),
                allowed_tools=["*"],
            ),
        )

        assert seen_cwds == [Path("repo")]
        assert results == [tool_success({"cwd": str(Path("repo"))})]

    @pytest.mark.asyncio
    async def test_cwd_defaults_to_workspace_when_config_has_none(self) -> None:
        registry = ToolRegistry()
        seen_cwds: list[Path] = []

        def cwd_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_cwds.append(context.effective_cwd)
            return tool_success({"cwd": str(context.effective_cwd)})

        registry.register(
            "cwd_default",
            "Return the effective working directory for testing the fallback.",
            {"type": "object"},
            cwd_handler,
        )
        executor = ToolExecutor(registry)

        await executor.execute_many(
            [ToolCall(id="call-1", name="cwd_default", arguments={})],
            make_execution_config(allowed_tools=["*"], workspace=Path("workspace")),
        )

        # No project cwd in the config: tools resolve against the workspace,
        # preserving today's identity-agent behavior.
        assert seen_cwds == [Path("workspace")]

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
                vbot_root=Path("app"),
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
            tool_failure(
                "invalid_arguments",
                "arguments: expected JSON object, received JSON array [type]",
            )
        ]

    @pytest.mark.asyncio
    async def test_argument_error_message_does_not_determine_failure_code(self) -> None:
        registry = ToolRegistry()

        def validating_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            raise ValueError("return_format must be a string")

        registry.register(
            "validating",
            "Validate arguments for testing.",
            {"type": "object"},
            validating_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="validating", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results == [tool_failure("invalid_arguments", "return_format must be a string")]

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
    async def test_non_serializable_handler_result_becomes_failed_result(self) -> None:
        registry = ToolRegistry()

        registry.register(
            "broken_result",
            "Return a value that cannot enter Session JSON.",
            {"type": "object"},
            lambda _context, _arguments: tool_success({"path": Path("README.md")}),
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="broken_result", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results[0]["ok"] is False
        assert results[0]["error"]["code"] == "invalid_tool_result"
        assert "not JSON-serializable" in results[0]["error"]["message"]

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

        registry.register(
            "slow",
            "Slow tool for testing.",
            {"type": "object"},
            slow_handler,
        )
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

        registry.register(
            "same",
            "Same tool for testing.",
            {"type": "object"},
            same_tool_handler,
        )
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
    async def test_serial_tool_is_a_barrier_between_parallel_safe_groups(self) -> None:
        registry = ToolRegistry()
        events: list[str] = []
        active_safe_calls = 0

        async def safe_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            nonlocal active_safe_calls
            active_safe_calls += 1
            events.append(f"start:{context.tool_call_id}")
            await asyncio.sleep(0.01)
            events.append(f"end:{context.tool_call_id}")
            active_safe_calls -= 1
            return tool_success({"id": context.tool_call_id})

        async def serial_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            assert active_safe_calls == 0
            events.append(f"start:{context.tool_call_id}")
            await asyncio.sleep(0)
            events.append(f"end:{context.tool_call_id}")
            return tool_success({"id": context.tool_call_id})

        registry.register(
            "safe",
            "Parallel-safe tool for testing.",
            {"type": "object"},
            safe_handler,
            parallel_safe=True,
        )
        registry.register(
            "serial",
            "Serial tool for testing.",
            {"type": "object"},
            serial_handler,
            parallel_safe=False,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [
                ToolCall(id="safe-1", name="safe", arguments={}),
                ToolCall(id="safe-2", name="safe", arguments={}),
                ToolCall(id="serial", name="serial", arguments={}),
                ToolCall(id="safe-3", name="safe", arguments={}),
                ToolCall(id="safe-4", name="safe", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )

        assert events.index("end:safe-1") < events.index("start:serial")
        assert events.index("end:safe-2") < events.index("start:serial")
        assert events.index("end:serial") < events.index("start:safe-3")
        assert events.index("end:serial") < events.index("start:safe-4")
        assert [result["data"]["id"] for result in results] == [
            "safe-1",
            "safe-2",
            "serial",
            "safe-3",
            "safe-4",
        ]

    @pytest.mark.asyncio
    async def test_unknown_tool_does_not_split_parallel_safe_siblings(self) -> None:
        registry = ToolRegistry()
        active_count = 0
        max_active_count = 0
        both_safe_calls_started = asyncio.Event()

        async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            nonlocal active_count, max_active_count
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            if active_count == 2:
                both_safe_calls_started.set()
            try:
                await asyncio.wait_for(both_safe_calls_started.wait(), timeout=1)
                return tool_success({"id": context.tool_call_id})
            finally:
                active_count -= 1

        registry.register(
            "safe",
            "Parallel-safe tool for testing.",
            {"type": "object"},
            handler,
            parallel_safe=True,
        )
        executor = ToolExecutor(registry, per_run_limit=3, global_limit=3)

        results = await executor.execute_many(
            [
                ToolCall(id="call-1", name="safe", arguments={}),
                ToolCall(id="call-unknown", name="missing", arguments={}),
                ToolCall(id="call-2", name="safe", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )

        assert max_active_count == 2
        assert results[0] == tool_success({"id": "call-1"})
        assert results[1]["error"]["code"] == "tool_not_found"
        assert results[2] == tool_success({"id": "call-2"})

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


class TestToolPromptBlockRegistry:
    """The tool side of the unified contributor path (D6).

    A tool declares a prompt block here; the runtime gathers ``block_definitions``
    and hands them to the prompt manager. The prompts domain only ever consumes a
    list of ``BlockDefinition`` objects — it never imports a tool class.
    """

    def test_static_and_dynamic_tool_blocks_become_definitions(self) -> None:
        registry = ToolPromptBlockRegistry()
        registry.register("bash", default_text="Bash guidance.")
        registry.register("web_fetch", render=lambda ctx: "Fetched.")

        definitions = registry.block_definitions()

        by_id = {definition.id: definition for definition in definitions}
        assert by_id["tool:bash"].owner == "tool:bash"
        assert by_id["tool:bash"].default_text == "Bash guidance."
        assert by_id["tool:bash"].editable is True
        assert by_id["tool:web_fetch"].owner == "tool:web_fetch"
        assert by_id["tool:web_fetch"].render is not None
        assert by_id["tool:web_fetch"].editable is False

    def test_requires_exactly_one_of_text_or_render(self) -> None:
        registry = ToolPromptBlockRegistry()

        with pytest.raises(ValueError):
            registry.register("bash", default_text="x", render=lambda ctx: "y")
        with pytest.raises(ValueError):
            registry.register("bash")

    def test_duplicate_tool_name_is_first_wins(self, caplog: pytest.LogCaptureFixture) -> None:
        registry = ToolPromptBlockRegistry()
        registry.register("bash", default_text="First.")

        caplog.set_level(logging.WARNING, logger="vbot.tools")
        registry.register("bash", default_text="Second.")

        definitions = registry.block_definitions()
        assert len(definitions) == 1
        assert definitions[0].default_text == "First."
        assert any("already declared" in record.getMessage() for record in caplog.records)

    def test_empty_registry_yields_no_definitions(self) -> None:
        assert ToolPromptBlockRegistry().block_definitions() == []
