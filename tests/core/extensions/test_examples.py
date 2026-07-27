"""The shipped ``examples/extensions/`` load cleanly against the real loader.

These examples are documentation-grade: a third-party author copies them first,
so they must load without diagnostics and behave as their comments claim. The
tests double as reusable end-to-end fixtures — they exercise the full
declare → apply path through the real filesystem loader.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.chat import CommandDispatcher, CommandExecutionContext, ReplySurface
from core.extensions import ExtensionRegistry, HookContext
from core.runs import ChatRunManager, Run
from core.tools import ToolContext, ToolRegistry

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "extensions"


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _allow_validator(extension_name: str, candidate: dict) -> dict:
    return candidate


def test_examples_directory_exists() -> None:
    assert _EXAMPLES_DIR.is_dir(), f"missing examples dir: {_EXAMPLES_DIR}"


def test_example_extensions_load_without_diagnostics() -> None:
    registry = ExtensionRegistry.load(_EXAMPLES_DIR)

    names = {record.name for record in registry.records()}
    assert {"guard_bash", "word_count", "workflow_command"} <= names
    assert registry.diagnostics() == []
    for record in registry.records():
        assert record.status == "loaded"
        assert record.capability_errors == []


def test_example_word_count_tool_registers_and_runs(tmp_path: Path) -> None:
    registry = ExtensionRegistry.load(_EXAMPLES_DIR)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)

    context = ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="c1",
        tool_name="word_count",
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )
    result = asyncio.run(tool_registry.dispatch(context, {"text": "one two three"}))
    tool = tool_registry.get("word_count")

    assert result["ok"] is True
    assert result["data"] == {"word_count": 3}
    assert tool.parallel_safe is True
    assert tool.result_schema is not None
    assert tool.result_schema["additionalProperties"] is False


def test_example_guard_bash_denies_dangerous_command() -> None:
    registry = ExtensionRegistry.load(_EXAMPLES_DIR)
    notes: list[str] = []
    ctx = HookContext(session_id="s", agent_id="a", run_id="r", add_note=notes.append)

    decision = asyncio.run(
        registry.dispatch_tool_call(
            ctx,
            tool_name="bash",
            tool_call_id="c1",
            input={"command": "rm -rf / --no-preserve-root"},
            validator=_allow_validator,
        )
    )

    assert decision.deny_extension == "guard_bash"
    assert decision.deny_reason
    assert notes  # a system-reminder note was added for the model


def test_example_guard_bash_allows_safe_command() -> None:
    registry = ExtensionRegistry.load(_EXAMPLES_DIR)
    ctx = HookContext(session_id="s", agent_id="a", run_id="r")

    decision = asyncio.run(
        registry.dispatch_tool_call(
            ctx,
            tool_name="bash",
            tool_call_id="c1",
            input={"command": "ls -la"},
            validator=_allow_validator,
        )
    )

    assert decision.deny_reason is None
    assert decision.replacement is None
    assert decision.effective_input == {"command": "ls -la"}


@pytest.mark.asyncio
async def test_example_workflow_command_starts_bundled_skill_run() -> None:
    follow_up = Run(run_id="run-workflow", agent_id="coder", session_id="session-one")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Trigger:
        async def trigger_run(self, *args: object, **kwargs: object) -> Run:
            calls.append((args, kwargs))
            return follow_up

    registry = ExtensionRegistry.load(_EXAMPLES_DIR)
    dispatcher = CommandDispatcher(ChatRunManager(), trigger_service=Trigger())
    registry.apply_commands(dispatcher)
    prepared = dispatcher.prepare("/workflow review the release")

    assert prepared is not None
    result = await dispatcher.execute(
        prepared,
        CommandExecutionContext(
            agent_id="coder",
            session_id="session-one",
            project_id="project-one",
            reply_surface=ReplySurface.webui(),
        ),
    )

    assert result.feedback is not None
    assert result.feedback.text == "Workflow started."
    assert result.runs[0].run is follow_up
    assert calls[0][0] == (
        "coder",
        "$workflow\n\nUser objective:\nreview the release",
        "session-one",
    )
    assert calls[0][1]["project_id"] == "project-one"
