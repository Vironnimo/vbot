"""Extension templates shipped with the vbot-docs Skill work after installation.

These examples are documentation-grade: a third-party author copies them first,
so they must load without diagnostics and behave as their comments claim. They
are installed into a disposable Extension root and loaded through the real
filesystem loader.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from core.chat import CommandDispatcher, CommandExecutionContext, ReplySurface
from core.extensions import ExtensionRegistry
from core.runs import ChatRunManager, Run
from core.skills import SkillRegistry
from core.tools import ToolContractError, ToolRegistry
from tests.core.extensions.extension_test_support import hook_context, tool_context

_ASSETS_DIR = Path(__file__).resolve().parents[3] / "resources/skills/vbot-docs/assets/extensions"


@pytest.fixture
def examples_dir(tmp_path: Path) -> Path:
    """Install the shipped files in a disposable instance's Extension root."""
    return Path(shutil.copytree(_ASSETS_DIR, tmp_path / "data/extensions"))


def _allow_validator(extension_name: str, candidate: dict) -> dict:
    return candidate


def test_example_extensions_load_cleanly_and_word_count_honors_its_schema(
    examples_dir: Path,
) -> None:
    registry = ExtensionRegistry.load(examples_dir)
    tools = ToolRegistry()
    registry.apply_tools(tools)

    assert {"guard_bash", "word_count", "workflow_command"} <= {
        item.name for item in registry.records()
    }
    assert registry.diagnostics() == []
    assert [(item.status, item.capability_errors) for item in registry.records()] == [
        ("loaded", [])
    ] * len(registry.records())

    tool = tools.get("word_count")
    assert tool.parallel_safe is True
    assert tool.open_input_schema is True
    assert "additionalProperties" not in tool.parameters
    assert tool.result_schema is not None
    assert tool.result_schema["additionalProperties"] is False
    context = tool_context("word_count", examples_dir)
    for text, count in (("one two three", 3), (" \t\n", 0)):
        result = asyncio.run(tools.dispatch(context, {"text": text}))
        assert (result["ok"], result["data"]) == (True, {"word_count": count})
    for arguments, problem in (
        ({}, '"text" is required'),
        ({"text": None}, '"text" must be a string'),
        ({"text": "one", "extra": True}, '"extra" is not a parameter'),
    ):
        with pytest.raises(ToolContractError) as exc_info:
            asyncio.run(tools.dispatch(context, arguments))
        assert problem in str(exc_info.value)


def test_example_guard_bash_denies_only_dangerous_commands(examples_dir: Path) -> None:
    registry = ExtensionRegistry.load(examples_dir)
    notes: list[str] = []

    def decide(command: str):
        return asyncio.run(
            registry.dispatch_tool_call(
                hook_context(add_note=notes.append),
                tool_name="bash",
                tool_call_id="c1",
                input={"command": command},
                validator=_allow_validator,
            )
        )

    allowed = decide("ls -la")
    assert (allowed.deny_reason, allowed.replacement) == (None, None)
    assert allowed.effective_input == {"command": "ls -la"}
    assert notes == []

    denied = decide("rm -rf / --no-preserve-root")
    assert denied.deny_extension == "guard_bash"
    assert denied.deny_reason
    # A system-reminder note tells the model why the command was refused.
    assert notes


@pytest.mark.asyncio
async def test_example_workflow_command_starts_bundled_skill_run(examples_dir: Path) -> None:
    follow_up = Run(run_id="run-workflow", agent_id="coder", session_id="session-one")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Trigger:
        async def trigger_run(self, *args: object, **kwargs: object) -> Run:
            calls.append((args, kwargs))
            return follow_up

    registry = ExtensionRegistry.load(examples_dir)
    skills = SkillRegistry.load(examples_dir / "workflow_command/skills")
    assert {skill.name for skill in skills.list_all()} == {"workflow"}
    assert all(item.valid and item.loadable and not item.warnings for item in skills.diagnostics())
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
