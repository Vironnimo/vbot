"""Tools: result envelopes, call display, prompt blocks, workers and built-in conventions."""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    ToolDisplay,
    ToolDisplayField,
    ToolDisplayPart,
    ToolPromptBlockRegistry,
    ToolRegistry,
    is_tool_result_envelope,
    result_count_fact_builder,
    tool_failure,
    tool_success,
)
from core.tools.apply_patch import APPLY_PATCH_TOOL_PARAMETERS
from core.tools.calendar import CALENDAR_TOOL_PARAMETERS
from core.tools.channel import CHANNEL_SEND_TOOL_PARAMETERS
from core.tools.cron import CRON_TOOL_PARAMETERS
from core.tools.image import ANALYZE_IMAGE_TOOL_PARAMETERS, IMAGE_GENERATION_TOOL_PARAMETERS
from core.tools.memory import MEMORY_TOOL_PARAMETERS
from core.tools.project import PROJECT_TOOL_PARAMETERS
from core.tools.read import READ_TOOL_PARAMETERS
from core.tools.search_files import SEARCH_FILES_TOOL_PARAMETERS
from core.tools.session_search import SESSION_SEARCH_TOOL_PARAMETERS
from core.tools.shell import SHELL_TOOL_PARAMETERS
from core.tools.skill import SKILL_TOOL_PARAMETERS
from core.tools.skill_manage import SKILL_MANAGE_TOOL_PARAMETERS
from core.tools.speech import TEXT_TO_SPEECH_TOOL_PARAMETERS
from core.tools.status import STATUS_TOOL_PARAMETERS
from core.tools.subagent import SUBAGENT_TOOL_PARAMETERS
from core.tools.tools import display_results, display_text, run_tool_worker
from core.tools.web_fetch import WEB_FETCH_TOOL_PARAMETERS
from core.tools.web_search import WEB_SEARCH_TOOL_PARAMETERS
from tests.core.tools.tools_test_support import JsonObject, make_context, read_file_handler

# --- Result envelopes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        pytest.param(
            tool_success({"content": "hello"}),
            {"ok": True, "error": None, "data": {"content": "hello"}, "artifacts": []},
            id="success",
        ),
        pytest.param(
            tool_failure("not_found", "File not found"),
            {
                "ok": False,
                "error": {"code": "not_found", "message": "File not found"},
                "data": None,
                "artifacts": [],
            },
            id="failure-without-retry-signal",
        ),
        pytest.param(
            tool_failure(
                "request_error", "HTTP 503 while fetching URL", retryable=True, attempts_made=4
            ),
            {
                "ok": False,
                "error": {
                    "code": "request_error",
                    "message": "HTTP 503 while fetching URL",
                    "retryable": True,
                    "attempts_made": 4,
                },
                "data": None,
                "artifacts": [],
            },
            id="retry-signal-inside-error",
        ),
        pytest.param(
            tool_failure("validation_error", "bad input", retryable=False),
            {
                "ok": False,
                "error": {"code": "validation_error", "message": "bad input", "retryable": False},
                "data": None,
                "artifacts": [],
            },
            id="retryable-false-without-attempts",
        ),
    ],
)
def test_envelope_helpers_build_valid_envelopes(envelope: JsonObject, expected: JsonObject) -> None:
    assert envelope == expected
    assert is_tool_result_envelope(envelope) is True


@pytest.mark.parametrize(
    ("signal", "field"),
    [
        ({"retryable": "yes"}, "retryable"),
        ({"attempts_made": -1}, "attempts_made"),
        ({"attempts_made": True}, "attempts_made"),
        ({"attempts_made": 1.5}, "attempts_made"),
    ],
)
def test_failure_rejects_an_invalid_retry_signal(signal: dict[str, Any], field: str) -> None:
    with pytest.raises(ValueError, match=field):
        tool_failure("x", "y", **signal)


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({"ok": True, "data": {}}, id="missing-keys"),
        pytest.param(
            {
                "ok": False,
                "error": {"code": "x", "message": "y", "unexpected": 1},
                "data": None,
                "artifacts": [],
            },
            id="unknown-error-key",
        ),
        pytest.param(
            {
                "ok": False,
                "error": {"code": "x", "message": "y", "attempts_made": -1},
                "data": None,
                "artifacts": [],
            },
            id="negative-attempts",
        ),
    ],
)
def test_malformed_envelopes_are_not_result_envelopes(envelope: JsonObject) -> None:
    assert is_tool_result_envelope(envelope) is False


# --- Call display ---------------------------------------------------------------


def _display_for_call(display: ToolDisplay, arguments: Any, **call: Any) -> JsonObject:
    registry = ToolRegistry()
    registry.register(
        "probe", "Probe the call display.", {"type": "object"}, read_file_handler, display=display
    )
    return registry.display_for_call("probe", arguments, **call)


def _text_part(value: str) -> JsonObject:
    return {
        "kind": "text",
        "value": value,
        "full_value": value,
        "truncate": "end",
        "tooltip": "truncated",
        "max_characters": 64,
        "quote": False,
        "copyable": False,
    }


@pytest.mark.parametrize(
    ("arguments", "summary", "primary"),
    [
        pytest.param(
            {"pattern": "TODO", "path": "src", "content": "large body"},
            "TODO · src",
            [_text_part("TODO · src")],
            id="summary-fields",
        ),
        pytest.param({"content": "large body"}, "", [], id="no-summary-argument"),
    ],
)
def test_summary_fields_describe_the_call_and_bulky_arguments_stay_hidden(
    arguments: JsonObject, summary: str, primary: list[JsonObject]
) -> None:
    display = ToolDisplay(summary_fields=("pattern", "path"), hidden_argument_keys=("content",))

    assert _display_for_call(display, arguments) == {
        "version": 1,
        "summary": summary,
        "hidden_argument_keys": ["content"],
        "primary": primary,
        "facts": [],
    }


@pytest.mark.parametrize(
    ("description", "value", "kind", "quote"),
    [
        ("  run tests  ", "run tests", "description", True),
        ("  ", "python -m pytest", "command", False),
    ],
)
def test_display_shows_the_first_nonblank_primary_candidate(
    description: str, value: str, kind: str, quote: bool
) -> None:
    display = ToolDisplay(
        primary_candidates=(
            ToolDisplayField("description", kind="description", quote=True),
            ToolDisplayField("command", kind="command"),
        )
    )

    [part] = _display_for_call(
        display, {"description": description, "command": "python -m pytest"}
    )["primary"]

    assert (part["value"], part["kind"], part["quote"]) == (value, kind, quote)


def test_display_builds_computed_semantic_parts() -> None:
    display = ToolDisplay(
        parts_builder=lambda _arguments: (
            ToolDisplayPart("status", truncate="never", tooltip="none"),
            ToolDisplayPart("process-session-one", kind="identifier", truncate="middle"),
        )
    )

    payload = _display_for_call(display, {"action": "status"})

    assert payload["summary"] == "status · process-session-one"
    assert payload["primary"][0]["truncate"] == "never"
    assert payload["primary"][1]["kind"] == "identifier"
    assert payload["primary"][1]["truncate"] == "middle"
    assert "detail" not in payload["primary"][0]


def test_display_part_carries_the_complete_value_it_stands_for() -> None:
    display = ToolDisplay(
        parts_builder=lambda _arguments: (
            ToolDisplayPart(
                "Run the tests",
                kind="description",
                copyable=True,
                detail="  python -m pytest\n  -x  ",
                detail_kind="command",
            ),
        )
    )

    [part] = _display_for_call(display, {"command": "python -m pytest"})["primary"]

    assert part["value"] == "Run the tests"
    assert (part["detail"], part["detail_kind"]) == ("python -m pytest\n  -x", "command")


@pytest.mark.parametrize(
    "arguments",
    [{"detail": 3}, {"detail": "ls", "detail_kind": "shell"}],
)
def test_display_part_rejects_an_invalid_detail(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ToolDisplayPart("Run the tests", **arguments)


def test_display_resolves_a_path_against_the_call_cwd(tmp_path: Path) -> None:
    display = ToolDisplay(
        primary_candidates=(
            ToolDisplayField(
                "path", kind="path", truncate="start", tooltip="always", copyable=True
            ),
        )
    )
    context = make_context("probe", workspace=tmp_path / "workspace", cwd=tmp_path / "project")

    payload = _display_for_call(display, {"path": "src/main.py"}, context=context)

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


def test_facts_recorded_by_the_handler_precede_the_display_facts() -> None:
    display = ToolDisplay(
        fact_builder=lambda _arguments, _result: (
            {"kind": "line_range", "start": 170, "end": 280},
            {"kind": "line_change", "change": "added", "value": 3},
        )
    )
    context = make_context("probe")
    context.add_display_count(10, "matches", at_least=True)
    context.add_display_line_changes(added=4, removed=0)

    assert _display_for_call(display, {}, context=context)["facts"] == [
        {"kind": "count", "value": 10, "unit": "matches", "at_least": True},
        {"kind": "line_change", "change": "added", "value": 4},
        {"kind": "line_change", "change": "removed", "value": 0},
        {"kind": "line_range", "start": 170, "end": 280},
        {"kind": "line_change", "change": "added", "value": 3},
    ]


def test_results_block_keeps_titled_items_with_bounded_text_and_web_links() -> None:
    items: list[dict[str, str | None]] = [
        {
            "title": " First ",
            "url": "https://example.com/a?b=1",
            "meta": "User",
            "time": "2026-09-30T12:00:00Z",
            "text": "x" * 700,
        },
        {"title": "", "text": "untitled"},
        # Only an absolute http(s) address becomes a link.
        {"title": "Second", "url": "javascript:alert(1)", "meta": " ", "text": None},
    ]
    items.extend({"title": f"More {index}"} for index in range(30))

    block = display_results(items)

    assert block["type"] == "results"
    assert len(block["items"]) == 20
    assert block["items"][0] == {
        "title": "First",
        "url": "https://example.com/a?b=1",
        "meta": "User",
        "time": "2026-09-30T12:00:00Z",
        "text": "x" * 599 + "…",
    }
    assert block["items"][1] == {"title": "Second"}


def test_detail_blocks_follow_the_built_then_recorded_order_and_show_an_unnoticed_failure() -> None:
    display = ToolDisplay(
        detail_builder=lambda arguments, _result: (
            display_text("query", source="arguments", path=("query",)),
        )
    )
    context = make_context("probe")
    context.add_display_notice("warning", "  Check the syntax.  ", subject="a.py")
    context.add_display_file_change("a.py", "created", None, "a\n")
    context.add_display_text("output", "  line\n")
    context.add_display_notice("error", "Could not write b.py.")
    context.add_display_file_change("c.py", "deleted", "c\n", None)
    rejected = tool_failure("invalid_arguments", "The call was rejected.")

    details = _display_for_call(display, {}, context=context, result=rejected)["details"]

    assert [block["type"] for block in details] == [
        "text",
        "notice",
        "file_changes",
        "text",
        "notice",
    ]
    assert details[0] == {
        "type": "text",
        "label": "query",
        "source": {"from": "arguments", "path": ["query"]},
    }
    assert details[1] == {
        "type": "notice",
        "level": "warning",
        "text": "Check the syntax.",
        "subject": "a.py",
    }
    assert [change["path"] for change in details[2]["files"]] == ["a.py", "c.py"]
    assert details[3] == {"type": "text", "label": "output", "text": "  line\n"}
    assert _display_for_call(
        ToolDisplay(details=True), {}, context=make_context("probe"), result=rejected
    )["details"] == [{"type": "notice", "level": "error", "text": "The call was rejected."}]
    assert "details" not in _display_for_call(ToolDisplay(), {}, context=context)


@pytest.mark.parametrize(
    ("arguments", "result", "facts"),
    [
        pytest.param(
            {"action": "list"},
            tool_success({"items": [{"id": 1}, {"id": 2}], "has_more": True}),
            [{"kind": "count", "value": 2, "unit": "results", "at_least": True}],
            id="listed-page-with-more",
        ),
        pytest.param(
            {"action": "list"},
            tool_success({"items": 4}),
            [{"kind": "count", "value": 4, "unit": "results", "at_least": False}],
            id="reported-count",
        ),
        pytest.param(
            {"action": "list"},
            tool_success({"items": [], "has_more": True}),
            [{"kind": "count", "value": 0, "unit": "results", "at_least": False}],
            id="empty-page-is-no-lower-bound",
        ),
        pytest.param({"action": "list"}, tool_failure("failed", "no count"), [], id="failed-call"),
        pytest.param({"action": "add"}, tool_success({"items": 4}), [], id="other-action"),
        *(
            pytest.param(
                {"action": "list"}, tool_success({"items": count}), [], id=f"count-{count!r}"
            )
            for count in (-1, True, "4", None)
        ),
    ],
)
def test_result_count_fact_counts_successful_listings(
    arguments: JsonObject, result: JsonObject, facts: list[JsonObject]
) -> None:
    display = ToolDisplay(
        fact_builder=result_count_fact_builder(
            "items", when_arguments={"action": "list"}, at_least_field="has_more"
        )
    )

    assert _display_for_call(display, arguments, result=result)["facts"] == facts


def test_display_rejects_bare_string_summary_fields() -> None:
    with pytest.raises(ValueError, match="summary_fields"):
        ToolDisplay(summary_fields="path")  # type: ignore[arg-type]


# --- Prompt blocks ----------------------------------------------------------------


class TestToolPromptBlockRegistry:
    """A Tool declares its System Prompt block here; the prompts domain imports no Tool."""

    def test_static_and_dynamic_tool_blocks_become_definitions(self) -> None:
        registry = ToolPromptBlockRegistry()
        assert registry.block_definitions() == []
        registry.register("bash", default_text="Bash guidance.")
        registry.register("web_fetch", render=lambda ctx: "Fetched.")

        by_id = {definition.id: definition for definition in registry.block_definitions()}

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


# --- Blocking work ----------------------------------------------------------------


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


# --- Built-in Tool conventions ----------------------------------------------------

_BUILTIN_TOOL_SCHEMAS: dict[str, JsonObject] = {
    "apply_patch": APPLY_PATCH_TOOL_PARAMETERS,
    "analyze_image": ANALYZE_IMAGE_TOOL_PARAMETERS,
    "bash": SHELL_TOOL_PARAMETERS,
    "calendar": CALENDAR_TOOL_PARAMETERS,
    "channel_send": CHANNEL_SEND_TOOL_PARAMETERS,
    "cron": CRON_TOOL_PARAMETERS,
    "image_generation": IMAGE_GENERATION_TOOL_PARAMETERS,
    "memory": MEMORY_TOOL_PARAMETERS,
    "project": PROJECT_TOOL_PARAMETERS,
    "read": READ_TOOL_PARAMETERS,
    "search_files": SEARCH_FILES_TOOL_PARAMETERS,
    "session_search": SESSION_SEARCH_TOOL_PARAMETERS,
    "skill": SKILL_TOOL_PARAMETERS,
    "skill_manage": SKILL_MANAGE_TOOL_PARAMETERS,
    "status": STATUS_TOOL_PARAMETERS,
    "subagent": SUBAGENT_TOOL_PARAMETERS,
    "text_to_speech": TEXT_TO_SPEECH_TOOL_PARAMETERS,
    "web_fetch": WEB_FETCH_TOOL_PARAMETERS,
    "web_search": WEB_SEARCH_TOOL_PARAMETERS,
}


def test_builtin_tool_schemas_are_flat_objects_with_declared_required_fields() -> None:
    def violations(schema: JsonObject) -> list[str]:
        found = []
        if schema.get("type") != "object":
            found.append("not an object")
        if "oneOf" in schema:
            found.append("oneOf")
        if "additionalProperties" in schema:
            found.append("additionalProperties")
        if not set(schema.get("required", ())) <= set(schema.get("properties", {})):
            found.append("undeclared required field")
        return found

    assert {
        name: violations(schema)
        for name, schema in _BUILTIN_TOOL_SCHEMAS.items()
        if violations(schema)
    } == {}


def test_every_builtin_registration_has_an_explicit_display_profile() -> None:
    tools_dir = Path(__file__).parents[3] / "core" / "tools"
    missing: list[str] = []
    for source_path in tools_dir.glob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        if "registry.register(" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=str(source_path))):
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
