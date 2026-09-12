"""Tools: definitions behavior."""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    Tool,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolDisplayPart,
    ToolNoteHook,
    ToolPromptBlockRegistry,
    ToolRegistry,
    is_tool_result_envelope,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.tools.tools import run_tool_worker
from tests.core.tools.tools_helpers import (
    READ_FILE_SCHEMA,
    JsonObject,
    make_context,
    read_file_handler,
    register_read_file,
)


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
