"""Prompt layout, override, and scope-selection tests."""

from dataclasses import replace
from pathlib import Path
from typing import override

from core.agents.temporary import TemporaryAgent
from core.prompts.blocks import BlockDefinition, LayoutEntry
from core.prompts.prompts import ProjectPromptContext
from core.storage.prompt_blocks import PromptBlockStore
from core.tools.availability import ToolAccess
from tests.core.prompts.prompts_test_support import (
    StubBlockStore,
    StubSkill,
    StubSkills,
    StubStorage,
    _agent,
    _manager,
)
from tests.core.prompts.prompts_test_support import workspace as workspace


def _temporary_agent(tmp_path: Path, prompt_blocks: list[str]) -> TemporaryAgent:
    return TemporaryAgent(
        id="preview",
        name="Test",
        model="fixture/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=[],
        tools={},
        fallback_models=[],
        prompt_blocks=prompt_blocks,
    )


def test_explicit_participant_selection_controls_preview_and_runtime(tmp_path: Path) -> None:
    project_file = tmp_path / "AGENTS.md"
    project_file.write_text("project-content-sentinel", encoding="utf-8")
    context = ProjectPromptContext.from_project("test", "Test", str(tmp_path), ["AGENTS.md"])
    calls: list[bool] = []

    def render_future(_context: object) -> str:
        calls.append(True)
        return "future-sentinel"

    manager = _manager(
        tmp_path,
        block_definitions=[
            BlockDefinition(id="extension:future", owner="always", render=render_future)
        ],
    )
    agent = _temporary_agent(tmp_path, ["core:agent_body"])
    reads: list[Path] = []
    assert (
        manager.build_system_prompt(
            agent,
            agent_body="body-sentinel",
            project_context=context,
            read_paths=reads,
        )
        == "body-sentinel"
    )
    assert calls == []
    assert project_file not in reads
    details: list[dict[str, object]] = []
    preview = manager.build_system_prompt(
        agent,
        agent_body="body-sentinel",
        project_context=context,
        block_details=details,
    )
    assert preview == "body-sentinel"
    project = next(block for block in details if block["id"] == "core:working_project")
    assert project["included"] is False
    assert "project-content-sentinel" in str(project["text"])
    enabled = replace(agent, prompt_blocks=["core:agent_body", "core:working_project"])
    prompt = manager.build_system_prompt(
        enabled,
        agent_body="body-sentinel",
        project_context=context,
        read_paths=reads,
    )
    assert "project-content-sentinel" in prompt
    assert project_file in reads
    assert "future-sentinel" not in prompt
    assert (
        manager.build_system_prompt(replace(agent, prompt_blocks=[]), agent_body="body-sentinel")
        == ""
    )


def test_participant_selection_overrides_shared_enablement(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        storage=StubStorage(
            {
                "tools.md": "tools-sentinel",
                "skills.md": "skills-sentinel",
                "runtime.md": "runtime-sentinel",
            }
        ),
        block_store=StubBlockStore(
            layouts={"default": [LayoutEntry(id="core:tools", enabled=False, source="core")]}
        ),
    )
    agent = _temporary_agent(tmp_path, ["core:tools", "core:skills"])

    assert manager.build_system_prompt(agent) == "tools-sentinel\n\nskills-sentinel"
    assert "runtime-sentinel" in manager.build_system_prompt(replace(agent, prompt_blocks=None))


def test_request_local_data_does_not_read_persistent_override_paths(
    workspace: Path, tmp_path: Path
) -> None:
    storage = PromptBlockStore(data_dir=tmp_path, ensure_directories=lambda: None)

    class RealOverrideStore(StubBlockStore):
        @override
        def read_block_override(self, scope: str, block_id: str) -> str | None:
            return storage.read_block_override(None if scope == "default" else scope[6:], block_id)

    manager = _manager(tmp_path, block_store=RealOverrideStore())
    agent = _agent(workspace)
    block = BlockDefinition(
        id="extension_session:orientation",
        owner="always",
        kind="data",
        default_text="private-context-sentinel {include:secret.md}",
        default_rank=10_000,
    )
    prompt = manager.build_system_prompt(agent, request_block_definitions=[block])
    assert str(block.default_text) in prompt
    assert "private-context-sentinel" not in manager.build_system_prompt(agent)
    assert all(item["id"] != block.id for item in manager.list_blocks())


def test_default_scope_layout_and_override_shape_the_prompt(
    workspace: Path, tmp_path: Path
) -> None:
    # A saved layout disabling the skills block drops it while the other blocks default
    # in; an override replaces the owner default text and still expands its producers.
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="core:skills", enabled=False, source="core")]},
        overrides={("default", "core:tools"): "## Custom Tools\n{generated:tool_list}"},
    )
    manager = _manager(
        tmp_path, skills=StubSkills([StubSkill("agent-cli", "Delegate")]), block_store=store
    )
    agent = _agent(workspace, allowed_tools=["read"], allowed_skills=["agent-cli"])

    prompt = manager.build_system_prompt(agent)

    assert "agent-cli" not in prompt
    assert "Delegate" not in prompt
    assert "0.1.0" in prompt
    assert "## Custom Tools" in prompt
    assert "- read: Read a workspace file" in prompt


def test_update_block_definitions_refreshes_contributed_blocks(
    workspace: Path, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace)
    assert "Hello refreshed." not in manager.build_system_prompt(agent)

    manager.update_block_definitions(
        [
            BlockDefinition(
                id="extension:late",
                owner="extension:late",
                default_text="Hello refreshed.",
            )
        ],
        ["late"],
    )

    assert "Hello refreshed." in manager.build_system_prompt(agent)


def test_custom_agent_scope_uses_agent_fragments_without_default_fallback(
    workspace: Path, tmp_path: Path
) -> None:
    # An agent scope reads agent fragments with no default fallback: an unset
    # fragment makes its block empty, so it collapses.
    storage = StubStorage()
    storage.set_agent_prompt_fragment(
        "coder",
        "runtime.md",
        "## Custom Runtime\nHost {server_hostname}",
    )
    manager = _manager(tmp_path, storage=storage)
    agent = _agent(workspace, custom_system_prompt_enabled=True)

    prompt = manager.build_system_prompt(agent)

    assert "## Custom Runtime" in prompt
    assert "Host test-host" in prompt
    # Default-scope fragments are not read for an agent build.
    assert ("default", "runtime.md") not in storage.reads
    assert ("default", "tools.md") not in storage.reads
    # The System Reminder anchor has no Agent copy: every scope renders the bundled text.
    assert "<system-reminder>" in prompt

    # A default-scope preview ignores the Agent's custom toggle.
    default_scope_preview = manager.build_system_prompt(agent, scope={"type": "default"})
    assert "## Custom Runtime" not in default_scope_preview
    assert "test-os" in default_scope_preview
