"""Prompt layout, override, and scope-selection tests."""

from .prompts_test_support import (
    BlockDefinition,
    LayoutEntry,
    Path,
    StubBlockStore,
    StubSkill,
    StubSkills,
    StubStorage,
    StubTools,
    SystemPromptManager,
    _agent,
    _manager,
)
from .prompts_test_support import workspace as workspace


def test_saved_layout_disables_a_core_block(workspace: Path, tmp_path: Path) -> None:
    # A scope that disables the skills block in its saved layout drops it; the other
    # blocks still default in at their rank.
    layout = [LayoutEntry(id="core:skills", enabled=False, source="core")]
    store = StubBlockStore(layouts={"default": layout})
    manager = SystemPromptManager(
        StubStorage(),
        StubTools(),
        StubSkills([StubSkill("agent-cli", "Delegate")]),
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname="h",
        os_name="o",
        current_date=lambda: "2026-05-04",
        block_store=store,
    )
    agent = _agent(workspace, allowed_skills=["agent-cli"])

    prompt = manager.build_system_prompt(agent)

    assert "## Available Skills" not in prompt
    assert "## Runtime" in prompt  # other blocks still present


def test_block_override_replaces_owner_default_text(workspace: Path, tmp_path: Path) -> None:
    store = StubBlockStore(
        overrides={("default", "core:tools"): "## Custom Tools\n{generated:tool_list}"}
    )
    manager = SystemPromptManager(
        StubStorage(),
        StubTools(),
        StubSkills([]),
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname="h",
        os_name="o",
        current_date=lambda: "2026-05-04",
        block_store=store,
    )
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = manager.build_system_prompt(agent)

    assert "## Custom Tools" in prompt
    assert "## Tool Call Style" not in prompt  # bundled default replaced
    assert "- read_file: Read a workspace file" in prompt  # producer still expands


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
    # runtime fragment makes the runtime block empty → it collapses.
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
    # Default-scope runtime fragment is not read for an agent build.
    assert ("default", "runtime.md") not in storage.reads
    # Tools fragment is unset for the agent scope → tools block collapses.
    assert "## Tool Call Style" not in prompt


def test_default_prompt_scope_preview_ignores_agent_custom_toggle(
    workspace: Path, tmp_path: Path
) -> None:
    storage = StubStorage()
    storage.set_agent_prompt_fragment("coder", "runtime.md", "## Custom Runtime")
    manager = _manager(tmp_path, storage=storage)

    prompt = manager.build_system_prompt(
        _agent(workspace, custom_system_prompt_enabled=True),
        scope={"type": "default"},
    )

    # Default scope uses bundled runtime, not the agent's custom fragment.
    assert "## Custom Runtime" not in prompt
    assert "## Runtime" in prompt
