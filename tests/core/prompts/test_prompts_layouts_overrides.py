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


def test_explicit_participant_selection_controls_preview_and_runtime(tmp_path):
    from dataclasses import replace

    from core.agents.temporary import TemporaryAgent
    from core.prompts.prompts import ProjectPromptContext
    from core.tools.availability import ToolAccess

    project_file = tmp_path / "AGENTS.md"
    project_file.write_text("project-content-sentinel", encoding="utf-8")
    context = ProjectPromptContext.from_project("test", "Test", str(tmp_path), ["AGENTS.md"])
    calls = []
    manager = _manager(
        tmp_path,
        block_definitions=[
            BlockDefinition(
                id="extension:future",
                owner="always",
                render=lambda _context: calls.append(True) or "future-sentinel",
            )
        ],
    )
    agent = TemporaryAgent(
        id="preview",
        name="Test",
        model="fixture/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=[],
        tools={},
        fallback_models=[],
        prompt_blocks=["core:agent_body"],
    )
    reads = []
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
    details = []
    preview = manager.build_system_prompt(
        agent,
        agent_body="body-sentinel",
        project_context=context,
        block_details=details,
    )
    assert preview == "body-sentinel"
    project = next(block for block in details if block["id"] == "core:working_project")
    assert project["included"] is False
    assert "project-content-sentinel" in project["text"]
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


def test_participant_selection_overrides_shared_enablement(workspace, tmp_path):
    from dataclasses import replace

    from core.agents.temporary import TemporaryAgent
    from core.tools.availability import ToolAccess

    manager = SystemPromptManager(
        StubStorage(
            {
                "tools.md": "tools-sentinel",
                "skills.md": "skills-sentinel",
                "runtime.md": "runtime-sentinel",
            }
        ),
        StubTools(),
        StubSkills([]),
        vbot_version="test",
        vbot_root=tmp_path,
        data_root=tmp_path,
        block_store=StubBlockStore(
            layouts={"default": [LayoutEntry(id="core:tools", enabled=False, source="core")]}
        ),
    )
    agent = TemporaryAgent(
        id="test",
        name="Test",
        model="fixture/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=[],
        tools={},
        fallback_models=[],
        prompt_blocks=["core:tools", "core:skills"],
    )
    assert manager.build_system_prompt(agent) == "tools-sentinel\n\nskills-sentinel"
    assert "runtime-sentinel" in manager.build_system_prompt(replace(agent, prompt_blocks=None))


def test_request_local_data_does_not_read_persistent_override_paths(workspace, tmp_path):
    from core.storage.prompt_blocks import PromptBlockStore

    storage = PromptBlockStore(data_dir=tmp_path, ensure_directories=lambda: None)

    class RealOverrideStore(StubBlockStore):
        def read_block_override(self, scope, block_id):
            return storage.read_block_override(None if scope == "default" else scope[6:], block_id)

    manager = SystemPromptManager(
        StubStorage(),
        StubTools(),
        StubSkills([]),
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path,
        server_hostname="h",
        operating_system="o",
        current_local_date=lambda: "2026-09-08",
        timezone_name=lambda: "UTC",
        block_store=RealOverrideStore(),
    )
    agent = _agent(workspace)
    block = BlockDefinition(
        id="extension_session:orientation",
        owner="always",
        kind="data",
        default_text="private-context-sentinel {include:secret.md}",
        default_rank=10_000,
    )
    prompt = manager.build_system_prompt(agent, request_block_definitions=[block])
    assert block.default_text in prompt
    assert "private-context-sentinel" not in manager.build_system_prompt(agent)
    assert all(item["id"] != block.id for item in manager.list_blocks())


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
        operating_system="o",
        current_local_date=lambda: "2026-05-04",
        timezone_name=lambda: "Europe/Berlin",
        block_store=store,
    )
    agent = _agent(workspace, allowed_skills=["agent-cli"])

    prompt = manager.build_system_prompt(agent)

    assert "agent-cli" not in prompt
    assert "Delegate" not in prompt
    assert "0.1.0" in prompt  # other blocks still render


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
        operating_system="o",
        current_local_date=lambda: "2026-05-04",
        timezone_name=lambda: "Europe/Berlin",
        block_store=store,
    )
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = manager.build_system_prompt(agent)

    assert "## Custom Tools" in prompt
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
    assert ("default", "tools.md") not in storage.reads


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
    assert "test-os" in prompt
