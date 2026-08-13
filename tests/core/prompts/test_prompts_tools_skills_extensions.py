"""Prompt Tool, Skill, and extension block tests."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

from core.projects import ProjectStore
from core.subagents import SubAgentPromptTarget
from core.tools.bash import register_bash_tool
from core.tools.file_state import FileReadState
from core.tools.process_manager import ProcessManager
from core.tools.project import register_project_tool
from core.tools.subagent import register_subagent_tools
from core.tools.tools import ToolPromptBlockRegistry
from core.utils.paths import model_path

from .prompts_test_support import (
    HISTORY_TOOL_NAME,
    MEMORY_PROMPT_MODE_OFF,
    Any,
    BlockDefinition,
    LayoutEntry,
    Path,
    StubBlockStore,
    StubSkill,
    StubSkills,
    StubStorage,
    StubTools,
    SystemPromptManager,
    ToolRegistry,
    _agent,
    _facade_manager,
    _manager,
    tool_success,
)
from .prompts_test_support import workspace as workspace


def test_provider_tool_definitions_use_same_agent_allowlist(
    workspace: Path, tmp_path: Path
) -> None:
    # skill/skill_manage are ordinary tools now: an allow-list without them does not
    # offer them, which is exactly how the per-agent toggle works.
    tools = StubTools()
    manager = _manager(tmp_path, tools=tools)
    agent = _agent(workspace, allowed_tools=["read_file"])

    definitions = manager.provider_tool_definitions(agent)

    assert definitions == [
        {
            "name": "read_file",
            "description": "Read a workspace file",
            "parameters": {"type": "object"},
        },
        {
            "name": "memory",
            "description": "Manage pinned memory",
            "parameters": {"type": "object"},
        },
    ]
    assert tools.provider_allowlist_calls == [["read_file", "memory"]]
    assert tools.provider_profile_agent_ids == ["coder"]


def test_prompt_tool_definitions_use_agent_configuration_profile(
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = StubTools()
    manager = _manager(tmp_path, tools=tools)
    agent = _agent(workspace, agent_id="profile-owner", allowed_tools=["read_file"])

    manager.build_system_prompt(agent)

    assert tools.prompt_profile_agent_ids
    assert set(tools.prompt_profile_agent_ids) == {"profile-owner"}


def test_bash_env_block_renders_only_for_permanent_agent_grants(
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = ToolRegistry()
    prompt_blocks = ToolPromptBlockRegistry()
    process_manager = ProcessManager(sweep_interval_seconds=3600)
    register_bash_tool(tools, process_manager, prompt_blocks=prompt_blocks)
    manager = _manager(
        tmp_path,
        tools=tools,
        block_definitions=prompt_blocks.block_definitions(),
    )
    granted = _agent(
        workspace,
        allowed_tools=["bash"],
        tools={"bash": {"allowed_env": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"]}},
    )
    ungranted = _agent(workspace, allowed_tools=["bash"])
    bash_denied = _agent(
        workspace,
        allowed_tools=[],
        tools={"bash": {"allowed_env": ["OPENAI_API_KEY"]}},
    )

    prompt = manager.build_system_prompt(granted)
    ungranted_prompt = manager.build_system_prompt(ungranted)
    denied_prompt = manager.build_system_prompt(bash_denied)

    assert "OPENAI_API_KEY" in prompt
    assert "OPENROUTER_API_KEY" in prompt
    assert "OPENAI_API_KEY" not in ungranted_prompt
    assert "OPENROUTER_API_KEY" not in ungranted_prompt
    assert "OPENAI_API_KEY" not in denied_prompt


def test_provider_tool_definitions_omit_memory_when_agent_memory_is_off(
    workspace: Path, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    definitions = manager.provider_tool_definitions(agent)

    assert "memory" not in [definition["name"] for definition in definitions]


def test_provider_tool_definitions_derive_session_read_from_session_search(
    workspace: Path,
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    for name in ("session_search", "session_read"):
        registry.register(
            name=name,
            description=f"{name} description",
            parameters={"type": "object", "additionalProperties": False},
            handler=lambda _context, _arguments: tool_success({}),
            activation="follows" if name == "session_read" else "configurable",
            activation_source="session_search" if name == "session_read" else None,
        )
    manager = _manager(tmp_path, tools=registry)
    agent = _agent(
        workspace,
        allowed_tools=["session_search"],
        memory_prompt_mode=MEMORY_PROMPT_MODE_OFF,
    )

    definitions = manager.provider_tool_definitions(agent)

    assert [definition["name"] for definition in definitions] == [
        "session_read",
        "session_search",
    ]


def test_provider_tool_definitions_keep_subagent_tools_for_self_only(
    workspace: Path, tmp_path: Path
) -> None:
    registry = ToolRegistry()
    registry.register(
        name="subagent",
        description="subagent description",
        parameters={
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=lambda _context, _arguments: tool_success({}),
    )
    manager = _manager(tmp_path, tools=registry)
    agent = _agent(
        workspace,
        allowed_tools=["*"],
        tools={"subagent": {"allowed_agents": []}},
    )

    definitions = manager.provider_tool_definitions(agent)

    assert [definition["name"] for definition in definitions] == ["subagent"]
    for definition in definitions:
        parameters = definition["parameters"]
        assert parameters["properties"]["agent_id"]["enum"] == ["coder"]


def test_provider_tool_definitions_narrow_explicit_agent_targets(
    workspace: Path, tmp_path: Path
) -> None:
    registry = ToolRegistry()
    registry.register(
        name="subagent",
        description="Start a Sub-Agent",
        parameters={
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=lambda _context, _arguments: tool_success({}),
    )
    manager = _manager(tmp_path, tools=registry)
    agent = _agent(
        workspace,
        agent_id="orchestrator",
        allowed_tools=["*"],
        tools={"subagent": {"allowed_agents": ["worker", "builder@vbot"]}},
    )

    definitions = manager.provider_tool_definitions(agent)

    assert len(definitions) == 1
    parameters = definitions[0]["parameters"]
    assert parameters["properties"]["agent_id"]["enum"] == [
        "orchestrator",
        "worker",
        "builder@vbot",
    ]
    assert "required" not in parameters


def test_provider_tool_definitions_offer_skill_and_skill_manage_for_identity_agent(
    workspace: Path, tmp_path: Path
) -> None:
    manager = _manager(tmp_path, skills=StubSkills([StubSkill("debugging", "Debug failures")]))
    agent = _agent(workspace, allowed_tools=["*"], allowed_skills=["debugging"])

    names = [definition["name"] for definition in manager.provider_tool_definitions(agent)]

    assert "skill" in names
    assert "skill_manage" in names


def test_provider_tool_definitions_offer_skill_even_without_skills(
    workspace: Path, tmp_path: Path
) -> None:
    # The skill tool is never gated on the agent already having a skill: a skill can
    # be authored or activated mid-session, so the loader stays available whenever the
    # tool itself is allowed (here via the wildcard) even with an empty skill set.
    manager = _manager(tmp_path, skills=StubSkills([]))
    agent = _agent(workspace, allowed_tools=["*"], allowed_skills=[])

    names = [definition["name"] for definition in manager.provider_tool_definitions(agent)]

    assert "skill" in names
    assert "skill_manage" in names


def test_provider_tool_definitions_drop_skill_when_agent_disallows_it(
    workspace: Path, tmp_path: Path
) -> None:
    # Toggling the tools off for an identity agent removes them, like any tool.
    manager = _manager(tmp_path, skills=StubSkills([StubSkill("debugging", "Debug failures")]))
    agent = _agent(workspace, allowed_tools=["read_file", "memory"], allowed_skills=["debugging"])

    names = [definition["name"] for definition in manager.provider_tool_definitions(agent)]

    assert "skill" not in names
    assert "skill_manage" not in names


def test_provider_tool_definitions_omit_skill_manage_for_config_agent(tmp_path: Path) -> None:
    # A config/project agent (empty workspace) has no private skill home, so the
    # authoring tool is withheld even under a wildcard allow-list; the loader stays.
    manager = _manager(tmp_path, skills=StubSkills([StubSkill("debugging", "Debug failures")]))
    agent = _agent("", allowed_tools=["*"], allowed_skills=["debugging"])

    names = [definition["name"] for definition in manager.provider_tool_definitions(agent)]

    assert "skill" in names
    assert "skill_manage" not in names


def test_skill_maintenance_block_renders_for_identity_agent_with_skill_manage(
    workspace: Path, tmp_path: Path
) -> None:
    # An identity agent whose effective tools include skill_manage sees the block,
    # rendering its bundled fragment text (owner tool:skill_manage passes gate 2).
    manager = _manager(tmp_path)
    agent = _agent(workspace, allowed_tools=["skill_manage"])

    prompt = manager.build_system_prompt(agent)
    without_tool = manager.build_system_prompt(_agent(workspace, allowed_tools=["read_file"]))

    assert prompt != without_tool


def test_skill_maintenance_block_absent_without_skill_manage_tool(
    workspace: Path, tmp_path: Path
) -> None:
    # An identity agent whose allow-list excludes skill_manage does not see it:
    # gate 2 (tool:skill_manage) fails when the tool is not effectively allowed.
    manager = _manager(tmp_path)
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = manager.build_system_prompt(agent)
    with_tool = manager.build_system_prompt(_agent(workspace, allowed_tools=["skill_manage"]))

    assert prompt != with_tool


def test_skill_maintenance_block_absent_for_config_agent_even_with_wildcard(
    tmp_path: Path,
) -> None:
    # A config/project agent (empty workspace) has no private skill home, so the
    # IDENTITY_ONLY_TOOLS strip removes skill_manage from its effective tools even
    # under a wildcard allow-list — the block gates out through gate 2.
    manager = _manager(tmp_path)
    agent = _agent("", allowed_tools=["*"], memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent, agent_body="You are the orchestrator.")
    identity_prompt = manager.build_system_prompt(_agent(tmp_path, allowed_tools=["*"]))

    assert prompt != identity_prompt


def test_list_blocks_shows_skill_maintenance_as_editable_tool_owned_block(
    tmp_path: Path,
) -> None:
    # The listing surface exposes the block as an editable text block owned by
    # tool:skill_manage (source core), directly after the skills block.
    manager = _facade_manager(tmp_path)

    blocks = {block["id"]: block for block in manager.list_blocks()}

    maintenance = blocks["core:skill_maintenance"]
    assert maintenance["kind"] == "text"
    assert maintenance["editable"] is True
    assert maintenance["source"] == "core"
    assert maintenance["owner"] == "tool:skill_manage"
    assert maintenance["enabled"] is True


def test_extension_static_block_renders_when_extension_loaded(
    workspace: Path, tmp_path: Path
) -> None:
    block = BlockDefinition(
        id="extension:greeter",
        owner="extension:greeter",
        default_text="Hello from the greeter extension.",
    )
    manager = _manager(tmp_path, block_definitions=[block], loaded_extensions=["greeter"])
    agent = _agent(workspace)

    prompt = manager.build_system_prompt(agent)

    assert "Hello from the greeter extension." in prompt


def test_extension_block_dropped_when_extension_not_loaded(workspace: Path, tmp_path: Path) -> None:
    # The owner gate (gate 2) drops a block whose extension is not in the loaded set.
    block = BlockDefinition(
        id="extension:greeter",
        owner="extension:greeter",
        default_text="Hello from the greeter extension.",
    )
    manager = _manager(tmp_path, block_definitions=[block], loaded_extensions=[])
    agent = _agent(workspace)

    prompt = manager.build_system_prompt(agent)

    assert "Hello from the greeter extension." not in prompt


def test_dynamic_block_renders_and_isolates_failure(workspace: Path, tmp_path: Path) -> None:
    good = BlockDefinition(
        id="extension:good",
        owner="extension:good",
        render=lambda context: "Dynamic OK",
    )

    def boom(context: Any) -> str:
        raise RuntimeError("render failed")

    bad = BlockDefinition(id="extension:bad", owner="extension:bad", render=boom)
    manager = _manager(
        tmp_path,
        block_definitions=[good, bad],
        loaded_extensions=["good", "bad"],
    )
    agent = _agent(workspace)

    prompt = manager.build_system_prompt(agent)

    # The good dynamic block renders; the raising one drops only itself (run lives).
    assert "Dynamic OK" in prompt


def test_tool_block_gated_on_tool_allowlist(workspace: Path, tmp_path: Path) -> None:
    # A tool-owned block (id/owner tool:<name>) renders only when the tool is on the
    # agent's effective allowlist (gate 2 reuses the prompt tool list).
    block = BlockDefinition(
        id="tool:read_file",
        owner="tool:read_file",
        default_text="Read-file guidance.",
    )
    manager = _manager(tmp_path, block_definitions=[block])
    allowed = _agent(workspace, allowed_tools=["read_file"])
    denied = _agent(workspace, allowed_tools=["shell"])

    assert "Read-file guidance." in manager.build_system_prompt(allowed)
    assert "Read-file guidance." not in manager.build_system_prompt(denied)


def test_subagent_block_renders_only_with_tool_and_lists_additional_targets(
    workspace: Path, tmp_path: Path
) -> None:
    class Coordinator:
        async def spawn(self, _context: Any, _arguments: Any) -> Any:
            return tool_success({})

        def prompt_targets(self, _agent: Any, project_id: str | None) -> Any:
            assert project_id == "vbot"
            return [
                SubAgentPromptTarget(
                    agent_id="reviewer",
                    name="Reviewer",
                    description="Reviews completed work.",
                )
            ]

    tools = ToolRegistry()
    prompt_blocks = ToolPromptBlockRegistry()
    register_subagent_tools(tools, cast(Any, Coordinator()), prompt_blocks)
    manager = _manager(
        tmp_path,
        tools=tools,
        block_definitions=prompt_blocks.block_definitions(),
    )
    allowed = _agent(workspace, allowed_tools=["subagent"])
    denied = _agent(workspace, allowed_tools=[])

    prompt = manager.build_system_prompt(allowed, agent_project_id="vbot")
    nested_prompt = manager.build_system_prompt(
        allowed,
        agent_project_id="vbot",
        nesting_depth=1,
    )

    denied_prompt = manager.build_system_prompt(
        denied,
        agent_project_id="vbot",
    )
    for value in ("reviewer", "Reviewer", "Reviews completed work."):
        assert value in prompt
        assert value in nested_prompt
        assert value not in denied_prompt
    assert prompt != nested_prompt


def test_subagent_block_stays_visible_without_additional_targets(
    workspace: Path, tmp_path: Path
) -> None:
    coordinator = SimpleNamespace(
        spawn=lambda _context, _arguments: tool_success({}),
        prompt_targets=lambda _agent, _project_id: [],
    )
    tools = ToolRegistry()
    prompt_blocks = ToolPromptBlockRegistry()
    register_subagent_tools(tools, cast(Any, coordinator), prompt_blocks)
    manager = _manager(
        tmp_path,
        tools=tools,
        block_definitions=prompt_blocks.block_definitions(),
    )
    agent = _agent(
        workspace,
        allowed_tools=["subagent"],
        tools={"subagent": {"allowed_agents": []}},
    )

    prompt = manager.build_system_prompt(agent)
    denied = manager.build_system_prompt(_agent(workspace, allowed_tools=[]))

    assert prompt != denied


def test_project_block_lists_projects_only_for_identity_agent_with_tool(
    workspace: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "vBot", repo)
    tools = ToolRegistry()
    prompt_blocks = ToolPromptBlockRegistry()
    register_project_tool(
        tools,
        projects,
        lambda: cast(Any, None),
        lambda _project_id: [],
        FileReadState(),
        prompt_blocks,
    )
    manager = _manager(
        tmp_path,
        tools=tools,
        block_definitions=prompt_blocks.block_definitions(),
    )
    identity = replace(
        _agent(workspace, allowed_tools=["project"]),
        root_project_id="vbot",
    )
    denied = _agent(workspace, allowed_tools=[])
    config_agent = _agent("", allowed_tools=["*"], memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(identity)

    assert '<project id="vbot" name="vBot"' in prompt
    assert f'project_path="{model_path(repo.resolve())}"' in prompt
    assert ' cwd="' not in prompt
    assert 'available="true" active="true"' in prompt
    assert '<project id="vbot"' not in manager.build_system_prompt(denied)
    assert '<project id="vbot"' not in manager.build_system_prompt(config_agent)


def test_enabling_tools_list_block_renders_tool_descriptions(
    workspace: Path, tmp_path: Path
) -> None:
    # core:tools_list ships disabled (default_enabled=False + bundled layout off);
    # a saved layout that switches it on renders the full name/description list —
    # the opt-in booster for models that attend poorly to native tool schemas.
    layout = [LayoutEntry(id="core:tools_list", enabled=True, source="core")]
    store = StubBlockStore(layouts={"default": layout})
    manager = SystemPromptManager(
        StubStorage(),
        StubTools(),
        StubSkills([]),
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname="h",
        operating_system="o",
        current_utc_date=lambda: "2026-05-04",
        block_store=store,
    )
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = manager.build_system_prompt(agent)

    assert "- read_file: Read a workspace file" in prompt


def test_session_grant_drives_provider_and_enabled_live_tool_list(
    workspace: Path, tmp_path: Path
) -> None:
    registry = ToolRegistry()
    registry.register(
        name=HISTORY_TOOL_NAME,
        description="Verify original Session records.",
        parameters={"type": "object", "additionalProperties": False},
        handler=lambda _context, _arguments: tool_success({}),
        session_scoped=True,
        activation="session_grant",
    )
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="core:tools_list", enabled=True, source="core")]}
    )
    manager = SystemPromptManager(
        StubStorage(),
        registry,
        StubSkills([]),
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname="h",
        operating_system="o",
        current_utc_date=lambda: "2026-05-04",
        block_store=store,
    )
    agent = _agent(workspace, allowed_tools=[])

    preview_definitions = manager.provider_tool_definitions(agent)
    preview_prompt = manager.build_system_prompt(agent)
    live_definitions = manager.provider_tool_definitions(
        agent,
        session_tool_grants=(HISTORY_TOOL_NAME,),
    )
    live_names = [str(definition["name"]) for definition in live_definitions]
    live_prompt = manager.build_system_prompt(
        agent,
        effective_tool_names=live_names,
        session_tool_grants=(HISTORY_TOOL_NAME,),
    )

    assert preview_definitions == []
    assert HISTORY_TOOL_NAME not in preview_prompt
    assert live_names == [HISTORY_TOOL_NAME]
    assert "- history: Verify original Session records." in live_prompt
