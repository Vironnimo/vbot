"""Extension templates shipped with the vbot-cli Skill work after installation.

These examples are documentation-grade: a third-party author copies them first,
so they must load without diagnostics and behave as their comments claim. The
tests double as reusable end-to-end fixtures — they exercise the full
declare → apply path through the real filesystem loader.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.chat import CommandDispatcher, CommandExecutionContext, ReplySurface
from core.extensions import ExtensionRegistry, HookContext
from core.runs import ChatRunManager, Run
from core.skills import SkillRegistry
from core.tools import ToolContext, ToolContractError, ToolRegistry

_ASSETS_DIR = Path(__file__).resolve().parents[3] / "resources/skills/vbot-cli/assets/extensions"


@pytest.fixture
def examples_dir(tmp_path: Path) -> Path:
    """Install the shipped files in a disposable instance's Extension root."""
    return Path(shutil.copytree(_ASSETS_DIR, tmp_path / "data/extensions"))


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _allow_validator(extension_name: str, candidate: dict) -> dict:
    return candidate


def test_example_extensions_load_without_diagnostics(examples_dir: Path) -> None:
    registry = ExtensionRegistry.load(examples_dir)

    names = {record.name for record in registry.records()}
    assert {"guard_bash", "word_count", "workflow_command"} <= names
    assert registry.diagnostics() == []
    for record in registry.records():
        assert record.status == "loaded"
        assert record.capability_errors == []


@pytest.mark.parametrize(
    ("arguments", "expected_count"),
    [({"text": "one two three"}, 3), ({"text": " \t\n"}, 0)],
)
def test_example_word_count_tool_registers_and_runs(
    tmp_path: Path, examples_dir: Path, arguments: dict, expected_count: int
) -> None:
    registry = ExtensionRegistry.load(examples_dir)
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
    result = asyncio.run(tool_registry.dispatch(context, arguments))
    tool = tool_registry.get("word_count")

    assert result["ok"] is True
    assert result["data"] == {"word_count": expected_count}
    assert tool.parallel_safe is True
    assert tool.result_schema is not None
    assert tool.result_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("arguments", "contract_error"),
    [({}, True), ({"text": None}, True), ({"text": "one", "extra": True}, False)],
)
def test_example_word_count_rejects_invalid_input(
    examples_dir: Path, arguments: dict, contract_error: bool
) -> None:
    registry = ExtensionRegistry.load(examples_dir)
    tools = ToolRegistry()
    registry.apply_tools(tools)
    context = ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="invalid",
        tool_name="word_count",
        tool_call_index=0,
        workspace=examples_dir,
        vbot_root=examples_dir,
        data_root=examples_dir,
    )

    if contract_error:
        with pytest.raises(ToolContractError):
            asyncio.run(tools.dispatch(context, arguments))
        return

    result = asyncio.run(tools.dispatch(context, arguments))

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert result["data"] is None


def test_example_guard_bash_denies_dangerous_command(examples_dir: Path) -> None:
    registry = ExtensionRegistry.load(examples_dir)
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


def test_example_guard_bash_allows_safe_command(examples_dir: Path) -> None:
    registry = ExtensionRegistry.load(examples_dir)
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
