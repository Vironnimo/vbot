"""Prompt Tool, Skill, and extension block tests."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.memory import MEMORY_PROMPT_MODE_AGENT_USER, MEMORY_PROMPT_MODE_OFF, MemoryPromptMode
from core.projects import ProjectStore
from core.prompts.blocks import BlockDefinition, LayoutEntry
from core.subagents import SubAgentPromptTarget
from core.tools import HISTORY_TOOL_NAME, ToolRegistry, model_names, tool_success
from core.tools.bash import register_bash_tool
from core.tools.file_state import FileReadState
from core.tools.process_manager import ProcessManager
from core.tools.project import register_project_tool
from core.tools.subagent import register_subagent_tools
from core.tools.tools import ToolPromptBlockRegistry
from core.utils.paths import model_path
from tests.core.prompts.prompts_test_support import (
    StubBlockStore,
    StubSkills,
    StubTools,
    _agent,
    _manager,
)
from tests.core.prompts.prompts_test_support import workspace as workspace

_TOOLS_LIST_ON = StubBlockStore(
    layouts={"default": [LayoutEntry(id="core:tools_list", enabled=True, source="core")]}
)


@pytest.mark.parametrize(
    ("mode", "names"),
    [
        (MEMORY_PROMPT_MODE_AGENT_USER, ["read_file", "memory"]),
        (MEMORY_PROMPT_MODE_OFF, ["read_file"]),
    ],
)
def test_tool_definitions_follow_the_agent_allowlist_profile_and_memory_mode(
    workspace: Path, tmp_path: Path, mode: MemoryPromptMode, names: list[str]
) -> None:
    tools = StubTools()
    manager = _manager(tmp_path, tools=tools)
    agent = _agent(workspace, allowed_tools=["read_file"], memory_prompt_mode=mode)

    definitions = manager.provider_tool_definitions(agent)
    manager.build_system_prompt(agent)

    assert [definition["name"] for definition in definitions] == names
    assert definitions[0] == {
        "name": "read_file",
        "description": "Read a workspace file",
        "parameters": {"type": "object"},
    }
    assert tools.provider_allowlist_calls == [names]
    # Prompt and Provider surfaces both use the Agent's configuration profile.
    assert tools.provider_profile_agent_ids == ["coder"]
    assert tools.prompt_profile_agent_ids
    assert set(tools.prompt_profile_agent_ids) == {"coder"}


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


@pytest.mark.parametrize(
    ("allowed_agents", "targets"),
    [
        ([], ["orchestrator"]),
        (["worker", "builder@vbot"], ["orchestrator", "worker", "builder@vbot"]),
    ],
    ids=["self-only", "explicit-targets"],
)
def test_provider_subagent_definition_narrows_agent_id_to_self_and_allowed_targets(
    workspace: Path, tmp_path: Path, allowed_agents: list[str], targets: list[str]
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
        tools={"subagent": {"allowed_agents": allowed_agents}},
    )

    [definition] = manager.provider_tool_definitions(agent)

    assert definition["parameters"]["properties"]["agent_id"]["enum"] == targets
    assert "required" not in definition["parameters"]


@pytest.mark.parametrize(
    ("identity", "allowed_tools", "offered"),
    [
        # The loader never waits for the Agent to have a Skill: one can be authored or
        # activated mid-Session.
        (True, ["*"], {"skill", "skill_manage"}),
        (True, ["read_file", "memory"], set()),
        # A config Agent has no private Skill home, even under a wildcard allow-list.
        (False, ["*"], {"skill"}),
    ],
    ids=["identity-wildcard", "identity-disallowed", "config-wildcard"],
)
def test_skill_tools_follow_the_allowlist_and_authoring_needs_an_identity_agent(
    workspace: Path, tmp_path: Path, identity: bool, allowed_tools: list[str], offered: set[str]
) -> None:
    manager = _manager(tmp_path, skills=StubSkills([]))
    agent = _agent(workspace if identity else "", allowed_tools=allowed_tools, allowed_skills=[])
    details: list[dict[str, Any]] = []

    names = {definition["name"] for definition in manager.provider_tool_definitions(agent)}
    manager.build_system_prompt(agent, block_details=details)

    assert names & {"skill", "skill_manage"} == offered
    # The skill maintenance block is owned by tool:skill_manage (gate 2).
    maintenance = next(
        (block for block in details if block["id"] == "core:skill_maintenance"), None
    )
    assert bool(maintenance and maintenance["included"]) is ("skill_manage" in offered)


def test_extension_blocks_render_only_for_loaded_extensions_and_isolate_failures(
    workspace: Path, tmp_path: Path
) -> None:
    def boom(context: Any) -> str:
        raise RuntimeError("render failed")

    blocks = [
        BlockDefinition(
            id="extension:greeter",
            owner="extension:greeter",
            default_text="Hello from the greeter extension.",
        ),
        BlockDefinition(
            id="extension:good", owner="extension:good", render=lambda ctx: "Dynamic OK"
        ),
        BlockDefinition(id="extension:bad", owner="extension:bad", render=boom),
    ]
    agent = _agent(workspace)

    def prompt(loaded: list[str]) -> str:
        manager = _manager(tmp_path, block_definitions=blocks, loaded_extensions=loaded)
        return manager.build_system_prompt(agent)

    loaded = prompt(["greeter", "good", "bad"])

    assert "Hello from the greeter extension." in loaded
    # The raising dynamic block drops only itself.
    assert "Dynamic OK" in loaded
    assert "Hello from the greeter extension." not in prompt(["good", "bad"])


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

        def foreground_timeout_minutes(self) -> int:
            return 17

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

    top_level_blocks: list[dict[str, Any]] = []
    nested_blocks: list[dict[str, Any]] = []
    prompt = manager.build_system_prompt(
        allowed, agent_project_id="vbot", block_details=top_level_blocks
    )
    nested_prompt = manager.build_system_prompt(
        allowed,
        agent_project_id="vbot",
        nesting_depth=1,
        block_details=nested_blocks,
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
    top_level_block = next(block for block in top_level_blocks if block["id"] == "tool:subagent")
    nested_block = next(block for block in nested_blocks if block["id"] == "tool:subagent")
    assert "17" in nested_block["text"]
    assert "17" not in top_level_block["text"]


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


def test_enabled_tools_list_block_names_tools_as_the_model_sees_them(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # core:tools_list ships disabled; a saved layout that switches it on renders the
    # name/description list, an opt-in booster for models that attend poorly to
    # native Tool schemas.
    monkeypatch.setattr(model_names, "_MODEL_NAMES", {"read_file": "host_read"})
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = _manager(tmp_path, block_store=_TOOLS_LIST_ON).build_system_prompt(agent)

    assert "- host_read: Read a workspace file" in prompt


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
    manager = _manager(tmp_path, tools=registry, block_store=_TOOLS_LIST_ON)
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
