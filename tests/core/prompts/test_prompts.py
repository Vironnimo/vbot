"""Tests for system prompt assembly."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from core.agents.agents import Agent
from core.channels.channels import ChannelConfig
from core.memory import (
    MEMORY_PROMPT_MODE_AGENT,
    MEMORY_PROMPT_MODE_AGENT_USER,
    MEMORY_PROMPT_MODE_OFF,
    MemoryPromptMode,
)
from core.prompts.prompts import (
    EDITABLE_PROMPT_FRAGMENT_NAMES,
    ProjectPromptContext,
    PromptAgent,
    PromptError,
    PromptFragmentManager,
    SkillPromptMetadata,
    SystemPromptManager,
    _validate_workspace_include,
)


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str


class StubStorage:
    def __init__(self, fragments: dict[str, str]) -> None:
        self._fragments = fragments
        self._agent_fragments: dict[tuple[str, str], str] = {}
        self.reads: list[tuple[str, str]] = []

    def read_prompt_fragment(self, fragment_name: str) -> str:
        self.reads.append(("default", fragment_name))
        return self._fragments[fragment_name]

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        self.reads.append((agent_id, fragment_name))
        return self._agent_fragments.get((agent_id, fragment_name), "")

    def set_agent_prompt_fragment(self, agent_id: str, fragment_name: str, content: str) -> None:
        self._agent_fragments[(agent_id, fragment_name)] = content


class EditableStubStorage(StubStorage):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(
            {
                fragment_name: f"{fragment_name} content"
                for fragment_name in EDITABLE_PROMPT_FRAGMENT_NAMES
            }
        )
        self.prompts_dir = tmp_path / "prompts"

    def write_prompt_fragment(self, fragment_name: str, content: str) -> Path:
        self._fragments[fragment_name] = content
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.prompts_dir / fragment_name
        target_path.write_text(content, encoding="utf-8")
        return target_path

    def agent_prompts_dir(self, agent_id: str) -> Path:
        return self.prompts_dir.parent / "agents" / agent_id / "prompts"

    def agent_prompt_fragment_exists(self, agent_id: str, fragment_name: str) -> bool:
        return (agent_id, fragment_name) in self._agent_fragments

    def write_agent_prompt_fragment(self, agent_id: str, fragment_name: str, content: str) -> Path:
        self._agent_fragments[(agent_id, fragment_name)] = content
        target_dir = self.agent_prompts_dir(agent_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / fragment_name
        target_path.write_text(content, encoding="utf-8")
        return target_path

    def reset_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> Path:
        return self.write_agent_prompt_fragment(
            agent_id,
            fragment_name,
            self._fragments[fragment_name],
        )

    def reset_prompt_fragment(self, fragment_name: str) -> Path:
        content = f"default {fragment_name}"
        self._fragments[fragment_name] = content
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.prompts_dir / fragment_name
        target_path.write_text(content, encoding="utf-8")
        return target_path


class StubAgentStore:
    def __init__(self, agents: list[PromptAgent]) -> None:
        self._agents = {agent.id: agent for agent in agents}

    def get(self, agent_id: str) -> PromptAgent:
        return self._agents[agent_id]

    def list(self) -> list[PromptAgent]:
        return list(self._agents.values())


class StubTools:
    def __init__(self) -> None:
        self.prompt_allowlist: list[str] | None = None
        self.provider_allowlist: list[str] | None = None
        self.prompt_allowlist_calls: list[list[str] | None] = []
        self.provider_allowlist_calls: list[list[str] | None] = []

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        self.prompt_allowlist = list(allowed_tools) if allowed_tools is not None else None
        self.prompt_allowlist_calls.append(self.prompt_allowlist)
        tools = [
            {"name": "read_file", "description": "Read a workspace file"},
            {"name": "shell", "description": "Run a shell command"},
            {"name": "memory", "description": "Manage pinned memory"},
        ]
        return _filter_by_allowlist(tools, allowed_tools)

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        self.provider_allowlist = list(allowed_tools) if allowed_tools is not None else None
        self.provider_allowlist_calls.append(self.provider_allowlist)
        tools = [
            {
                "name": "read_file",
                "description": "Read a workspace file",
                "parameters": {"type": "object"},
            },
            {
                "name": "shell",
                "description": "Run a shell command",
                "parameters": {"type": "object"},
            },
            {
                "name": "memory",
                "description": "Manage pinned memory",
                "parameters": {"type": "object"},
            },
            {
                "name": "skill",
                "description": "Load a skill",
                "parameters": {"type": "object"},
            },
        ]
        if allowed_tools == ["skill"]:
            return [tools[-1]]
        return _filter_by_allowlist(tools, allowed_tools)


class StubSkills:
    def __init__(self, skills: list[StubSkill]) -> None:
        self._skills = skills
        self.allowlist: list[str] | None = None

    def filter_allowed(self, allowed_skills: list[str]) -> list[SkillPromptMetadata]:
        self.allowlist = allowed_skills
        if "*" in allowed_skills:
            return list(self._skills)
        return [skill for skill in self._skills if skill.name in allowed_skills]


class StubChannels:
    def __init__(self, channels: list[ChannelConfig]) -> None:
        self._channels = channels

    def has_active_channels(self) -> bool:
        return any(channel.enabled for channel in self._channels)

    def list_channels(self) -> list[ChannelConfig]:
        return list(self._channels)

    def _is_running(self, channel_id: str) -> bool:
        return any(channel.id == channel_id and channel.enabled for channel in self._channels)


@pytest.fixture
def fragments() -> dict[str, str]:
    return {
        "system.md": ("{include:SOUL.md}\n{memory}\n{runtime}\n{tools}\n{channels}\n{skills}"),
        "runtime.md": (
            "## Runtime\n"
            "- Host: {host}\n"
            "- OS: {os}\n"
            "- App version: {app_version}\n"
            "- You are powered by the model {model}\n"
            "- Your Workspace (HOME, your CWD for tools, where you and your files live): "
            "{agent_workspace}\n"
            "- App Path: {app_dir}\n"
            "- Data Path: All app data (sessions, workspaces, skills, configs, etc.) "
            "lives here: {data_root}\n"
            "- Thinking level: {thinking_effort}\n"
            "- Date: {current_date}"
        ),
        "tools.md": "## Available Tools\n{tool_list}",
        "channels.md": "## Channels\n{channel_list}",
        "skills.md": "## Available Skills\n{skill_list}",
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    (directory / "SOUL.md").write_text("Soul text", encoding="utf-8")
    (directory / "MEMORY.md").write_text("Memory text", encoding="utf-8")
    (directory / "USER.md").write_text("User text", encoding="utf-8")
    return directory


def test_build_system_prompt_replaces_all_placeholders_and_includes_workspace_files(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = StubTools()
    skills = StubSkills(
        [
            StubSkill("agent-cli", "Delegate coding tasks"),
            StubSkill("news", "Fetch news"),
        ]
    )
    manager = SystemPromptManager(
        StubStorage(fragments),
        tools,
        skills,
        channel_registry=StubChannels(
            [
                ChannelConfig(
                    id="tg-private",
                    platform="telegram",
                    agent_id="coder",
                    allowed_chat_ids=["8506476339"],
                    token_env_var="TELEGRAM_BOT_TOKEN",
                    enabled=True,
                ),
                ChannelConfig(
                    id="tg-group",
                    platform="telegram",
                    agent_id="coder",
                    allowed_chat_ids=["111", "222"],
                    token_env_var="TELEGRAM_GROUP_TOKEN",
                    enabled=True,
                ),
                ChannelConfig(
                    id="other-agent-channel",
                    platform="telegram",
                    agent_id="other-agent",
                    allowed_chat_ids=["333"],
                    token_env_var="OTHER_TOKEN",
                    enabled=True,
                ),
            ]
        ),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        host="test-host",
        os_name="test-os",
        current_date=lambda: "2026-05-04",
    )
    agent = _agent(
        workspace,
        allowed_tools=["read_file"],
        allowed_skills=["agent-cli"],
    )

    prompt = manager.build_system_prompt(agent)

    assert "- Host: test-host" in prompt
    assert "- OS: test-os" in prompt
    assert "- App version: 0.1.0" in prompt
    assert "- You are powered by the model openai/gpt-5.2" in prompt
    assert f"{workspace}" in prompt
    assert str((tmp_path / "app").resolve()) in prompt
    assert str((tmp_path / "data").resolve()) in prompt
    assert "- Thinking level: high" in prompt
    assert "- Date: 2026-05-04" in prompt
    assert "- read_file: Read a workspace file" in prompt
    assert "shell" not in prompt
    assert "## Channels" in prompt
    assert "- tg-private: telegram (default target available)" in prompt
    assert "- tg-group: telegram (explicit target required)" in prompt
    assert "other-agent-channel" not in prompt
    assert "<name>agent-cli</name>" in prompt
    assert "<description>Delegate coding tasks</description>" in prompt
    assert "<path>" not in prompt
    assert "<location>" not in prompt
    assert "news" not in prompt
    assert "Soul text" in prompt
    assert "Memory text" in prompt
    assert "User text" in prompt
    assert '<file name="SOUL.md">' in prompt
    assert '<file name="MEMORY.md">' in prompt
    assert '<file name="USER.md">' in prompt
    assert "{" not in prompt
    assert tools.prompt_allowlist_calls == [["read_file"], ["memory"]]
    assert skills.allowlist == ["agent-cli"]


def test_memory_block_omits_workspace_memory_when_agent_memory_is_off(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    prompt = manager.build_system_prompt(
        _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    )

    assert "Soul text" in prompt
    assert "Memory text" not in prompt
    assert "User text" not in prompt
    assert "<memory>" not in prompt


def test_memory_block_can_include_only_agent_memory(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    prompt = manager.build_system_prompt(
        _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)
    )

    assert "<memory>" in prompt
    assert '<file name="MEMORY.md">' in prompt
    assert "Memory text" in prompt
    assert '<file name="USER.md">' not in prompt
    assert "User text" not in prompt


def test_provider_tool_definitions_use_same_agent_allowlist(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = StubTools()
    manager = SystemPromptManager(
        StubStorage(fragments),
        tools,
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
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
    assert tools.provider_allowlist_calls == [["read_file"], ["memory"]]


def test_provider_tool_definitions_omit_memory_when_agent_memory_is_off(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    definitions = manager.provider_tool_definitions(agent)

    assert "memory" not in [definition["name"] for definition in definitions]


def test_build_system_prompt_renders_none_when_agent_has_no_active_channels(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        channel_registry=StubChannels([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = manager.build_system_prompt(agent)

    assert "## Channels\n- None" in prompt


def test_provider_tool_definitions_include_internal_skill_when_agent_has_skills(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([StubSkill("debugging", "Debug failures")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
    agent = _agent(workspace, allowed_tools=[], allowed_skills=["debugging"])

    definitions = manager.provider_tool_definitions(agent)

    assert [definition["name"] for definition in definitions] == ["memory", "skill"]


def test_provider_tool_definitions_omit_internal_skill_when_agent_has_no_skills(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([StubSkill("debugging", "Debug failures")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
    agent = _agent(workspace, allowed_tools=["read_file"], allowed_skills=[])

    definitions = manager.provider_tool_definitions(agent)

    assert "skill" not in [definition["name"] for definition in definitions]


def test_empty_tool_and_skill_allowlists_emit_empty_sections(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([StubSkill("agent-cli", "Delegate coding tasks")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        current_date=lambda: "2026-05-04",
    )
    agent = _agent(
        workspace,
        allowed_tools=[],
        allowed_skills=[],
        memory_prompt_mode=MEMORY_PROMPT_MODE_OFF,
    )

    prompt = manager.build_system_prompt(agent)

    assert "- read_file" not in prompt
    assert "<available_skills>\n</available_skills>" in prompt
    assert "agent-cli" not in prompt


def test_skill_xml_escapes_metadata(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([StubSkill("a&b", "Use <danger>")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )
    agent = _agent(workspace, allowed_skills=["*"])

    prompt = manager.build_system_prompt(agent)

    assert "<name>a&amp;b</name>" in prompt
    assert "<description>Use &lt;danger&gt;</description>" in prompt


def test_unsafe_workspace_include_raises_error(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    fragments["system.md"] = "{include:../secret.md}"
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    with pytest.raises(PromptError, match="Unsafe workspace include"):
        manager.build_system_prompt(_agent(workspace))


@pytest.mark.parametrize("filename", ["SOUL.md", "CUSTOM.md", "my-notes.txt", "notes.json"])
def test_validate_workspace_include_accepts_safe_flat_filenames(filename: str) -> None:
    _validate_workspace_include(filename)  # should not raise


@pytest.mark.parametrize(
    "filename",
    [
        "../foo",
        "foo/bar",
        "/etc/passwd",
        "C:\\Windows\\system32\\cmd.exe",
    ],
)
def test_validate_workspace_include_rejects_unsafe_paths(filename: str) -> None:
    with pytest.raises(PromptError, match="Unsafe workspace include"):
        _validate_workspace_include(filename)


def test_workspace_include_wraps_content_in_xml_file_tag(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    fragments["system.md"] = "{include:SOUL.md}"
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    prompt = manager.build_system_prompt(_agent(workspace))

    assert prompt == '<file name="SOUL.md">\nSoul text\n</file>'


def test_missing_workspace_include_is_omitted(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    fragments["system.md"] = "Before\n{include:MISSING.md}\nAfter"
    manager = SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    prompt = manager.build_system_prompt(_agent(workspace))

    assert prompt == "Before\n\nAfter"


def test_custom_agent_system_prompt_uses_agent_fragments_without_default_fallback(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    storage = StubStorage(fragments)
    storage.set_agent_prompt_fragment("coder", "system.md", "Custom root\n{skills}")
    manager = SystemPromptManager(
        storage,
        StubTools(),
        StubSkills([StubSkill("debugging", "Debug failures")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    prompt = manager.build_system_prompt(_agent(workspace, custom_system_prompt_enabled=True))

    assert prompt == "Custom root\n"
    assert ("default", "skills.md") not in storage.reads


def test_custom_agent_system_prompt_omits_unreferenced_blocks_lazily(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    storage = StubStorage(fragments)
    storage.set_agent_prompt_fragment("coder", "system.md", "Custom root only")
    manager = SystemPromptManager(
        storage,
        StubTools(),
        StubSkills([StubSkill("debugging", "Debug failures")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    prompt = manager.build_system_prompt(_agent(workspace, custom_system_prompt_enabled=True))

    assert prompt == "Custom root only"
    assert storage.reads == [("coder", "system.md")]


def test_default_prompt_scope_preview_ignores_agent_custom_toggle(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    fragments["system.md"] = "Default root"
    storage = StubStorage(fragments)
    storage.set_agent_prompt_fragment("coder", "system.md", "Custom root")
    manager = SystemPromptManager(
        storage,
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )

    prompt = manager.build_system_prompt(
        _agent(workspace, custom_system_prompt_enabled=True),
        scope={"type": "default"},
    )

    assert prompt == "Default root"


def test_prompt_fragment_manager_lists_editable_fragments_in_ui_order(tmp_path: Path) -> None:
    storage = EditableStubStorage(tmp_path)
    storage.write_prompt_fragment("runtime.md", "custom runtime")
    manager = PromptFragmentManager(storage)

    fragments = manager.list_fragments()

    assert [fragment["name"] for fragment in fragments] == list(EDITABLE_PROMPT_FRAGMENT_NAMES)
    assert fragments[0]["is_modified"] is False
    assert fragments[1]["is_modified"] is True
    assert any(variable["placeholder"] == "{app_version}" for variable in fragments[1]["variables"])


def test_prompt_fragment_manager_updates_and_resets_editable_fragment(tmp_path: Path) -> None:
    storage = EditableStubStorage(tmp_path)
    manager = PromptFragmentManager(storage)

    updated = manager.update_fragment("tools.md", "custom tools")
    reset = manager.reset_fragment("tools.md")

    assert updated == {"name": "tools.md", "content": "custom tools", "is_modified": True}
    assert reset == {"name": "tools.md", "content": "default tools.md", "is_modified": False}


def test_prompt_fragment_manager_lists_available_agent_scopes(tmp_path: Path) -> None:
    enabled_agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    disabled_agent = _agent(
        tmp_path,
        agent_id="disabled",
        custom_system_prompt_enabled=False,
    )
    manager = PromptFragmentManager(
        EditableStubStorage(tmp_path),
        StubAgentStore([disabled_agent, enabled_agent]),
    )

    scopes = manager.list_scopes()

    assert scopes == [
        {"type": "default", "label": "Default"},
        {"type": "agent", "agent_id": "coder", "label": "Coder Agent"},
    ]


def test_prompt_fragment_manager_reads_missing_agent_fragments_as_empty(tmp_path: Path) -> None:
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = PromptFragmentManager(EditableStubStorage(tmp_path), StubAgentStore([agent]))

    fragments = manager.list_fragments({"type": "agent", "agent_id": "coder"})

    assert [fragment["name"] for fragment in fragments] == list(EDITABLE_PROMPT_FRAGMENT_NAMES)
    assert all(fragment["content"] == "" for fragment in fragments)
    assert all(fragment["is_modified"] is False for fragment in fragments)


def test_prompt_fragment_manager_updates_and_resets_agent_fragment(tmp_path: Path) -> None:
    storage = EditableStubStorage(tmp_path)
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = PromptFragmentManager(storage, StubAgentStore([agent]))
    scope = {"type": "agent", "agent_id": "coder"}

    updated = manager.update_fragment("skills.md", "custom agent skills", scope)
    reset = manager.reset_fragment("skills.md", scope)

    assert updated == {"name": "skills.md", "content": "custom agent skills", "is_modified": True}
    assert reset == {"name": "skills.md", "content": "skills.md content", "is_modified": True}


def test_prompt_fragment_manager_rejects_disabled_agent_scope(tmp_path: Path) -> None:
    agent = _agent(tmp_path, custom_system_prompt_enabled=False)
    manager = PromptFragmentManager(EditableStubStorage(tmp_path), StubAgentStore([agent]))

    with pytest.raises(PromptError, match="not enabled"):
        manager.list_fragments({"type": "agent", "agent_id": "coder"})


def test_prompt_fragment_manager_rejects_internal_compaction_fragment(tmp_path: Path) -> None:
    manager = PromptFragmentManager(EditableStubStorage(tmp_path))

    with pytest.raises(PromptError, match="unknown prompt fragment: compaction.md"):
        manager.update_fragment("compaction.md", "custom compaction")


# --- Phase 4: {agent_body} and {project_files} placeholders -----------------
#
# These tests mirror the real ``resources/prompts/system.md`` slotting: the
# config-agent body sits in the identity slot (before SOUL/memory), the project
# files after the identity block. The emptiness-collapse rule means an identity
# agent at home (empty body, no project context) gets the unchanged prompt.

# Root fragment with the two new placeholders in their real slots. The trailing
# ``{tools}`` etc. are dropped so these focused tests assert body/project framing
# without channel/skill noise; ``{runtime}`` is dropped too (not under test here).
_PROJECT_ROOT_FRAGMENT = "{agent_body}\n{include:SOUL.md}\n{memory}\n{project_files}"


def _project_manager(
    fragments: dict[str, str],
    tmp_path: Path,
    *,
    root: str = _PROJECT_ROOT_FRAGMENT,
) -> SystemPromptManager:
    fragments = dict(fragments)
    fragments["system.md"] = root
    return SystemPromptManager(
        StubStorage(fragments),
        StubTools(),
        StubSkills([]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
    )


def test_config_agent_prompt_inserts_body_and_project_files_in_order(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    # Config agent: no SOUL/memory home, a verbatim body, AGENTS.md + one
    # auto-load file in the repo cwd.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_text("Project context", encoding="utf-8")
    manager = _project_manager(fragments, tmp_path)
    # Config agent's real production workspace is "" (no SOUL/memory home), so
    # {include:SOUL.md}/{memory} collapse — the same value the resolver synthesizes.
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(repo, ["CONTEXT.md"])

    prompt = manager.build_system_prompt(
        agent,
        agent_body="You are the orchestrator.",
        project_context=context,
    )

    # Body verbatim, then the project files (AGENTS.md first), each <file>-wrapped.
    assert "You are the orchestrator." in prompt
    assert '<file name="AGENTS.md">\nTeam rules\n</file>' in prompt
    assert '<file name="CONTEXT.md">\nProject context\n</file>' in prompt
    # Order: body before AGENTS.md before CONTEXT.md.
    assert prompt.index("You are the orchestrator.") < prompt.index("AGENTS.md")
    assert prompt.index("AGENTS.md") < prompt.index("CONTEXT.md")


def test_config_agent_body_with_braces_is_not_expanded(
    fragments: dict[str, str],
    tmp_path: Path,
) -> None:
    # Plan risk "Body-Wörtlichkeit": a "{...}" in the imported body must survive
    # verbatim — never treated as a vBot placeholder. Use real vBot placeholder
    # names inside the body to prove they are not substituted.
    manager = _project_manager(fragments, tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    body = "Use {memory} and {include:SOUL.md} and {runtime} literally; also {custom}."

    prompt = manager.build_system_prompt(
        agent,
        agent_body=body,
        project_context=None,
    )

    assert body in prompt


def test_config_agent_empty_workspace_skips_includes_and_ignores_cwd(
    fragments: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A config agent's production workspace is "". An empty workspace must mean
    # "no includes" — never Path("") == Path("."), which resolves {include:SOUL.md}
    # against the server's process CWD. A decoy SOUL.md in the CWD proves it is
    # never read, and no per-turn "missing include" warning is emitted.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "SOUL.md").write_text("LEAKED SOUL FROM CWD", encoding="utf-8")
    manager = _project_manager(fragments, tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    with caplog.at_level(logging.WARNING):
        prompt = manager.build_system_prompt(
            agent,
            agent_body="You are the orchestrator.",
            project_context=None,
        )

    assert "You are the orchestrator." in prompt
    assert "LEAKED SOUL FROM CWD" not in prompt
    assert '<file name="SOUL.md">' not in prompt
    assert "Skipping missing workspace include" not in caplog.text


def test_identity_agent_prompt_unchanged_without_body_or_project(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    # Identity agent at home: empty body, no project context → both placeholders
    # collapse and the prompt equals the no-placeholder build of the same root.
    manager = _project_manager(fragments, tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)

    with_placeholders = manager.build_system_prompt(agent)

    baseline_manager = _project_manager(fragments, tmp_path, root="{include:SOUL.md}\n{memory}")
    baseline = baseline_manager.build_system_prompt(agent)

    # The collapsing placeholders leave only their surrounding blank lines, which
    # the identity-at-home build had anyway; the substantive content is identical.
    assert "Soul text" in with_placeholders
    assert "Memory text" in with_placeholders
    assert baseline.strip() == with_placeholders.strip()
    assert "{agent_body}" not in with_placeholders
    assert "{project_files}" not in with_placeholders


def test_rooted_identity_agent_prompt_puts_identity_before_project(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    # Rooted identity agent: has a workspace (SOUL/memory) AND a project context.
    # The only case where both appear in the system prompt — identity first.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    manager = _project_manager(fragments, tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)
    context = ProjectPromptContext.from_project(repo, [])

    prompt = manager.build_system_prompt(agent, project_context=context)

    assert "Soul text" in prompt
    assert "Memory text" in prompt
    assert "Team rules" in prompt
    # Identity (SOUL, then memory) before the project files.
    assert prompt.index("Soul text") < prompt.index("Team rules")
    assert prompt.index("Memory text") < prompt.index("Team rules")


def test_project_files_render_only_existing_files(
    fragments: dict[str, str],
    tmp_path: Path,
) -> None:
    # Lazy rendering: a missing AGENTS.md is skipped silently, a missing auto-load
    # entry is skipped, no warning/placeholder leakage.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CONTEXT.md").write_text("Context only", encoding="utf-8")
    manager = _project_manager(fragments, tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(repo, ["CONTEXT.md", "MISSING.md"])

    prompt = manager.build_system_prompt(agent, agent_body="", project_context=context)

    assert "AGENTS.md" not in prompt
    assert "MISSING.md" not in prompt
    assert '<file name="CONTEXT.md">\nContext only\n</file>' in prompt


def test_project_files_allow_subfolders_at_any_depth(
    fragments: dict[str, str],
    tmp_path: Path,
) -> None:
    # Auto-load points at files inside subfolders (this project keeps its context
    # under .vorch/). Subfolders at any depth load — no "unsafe" rejection. The
    # path is the user's own config; where the file lives is not restricted.
    repo = tmp_path / "repo"
    (repo / ".vorch").mkdir(parents=True)
    (repo / ".vorch" / "PROJECT.md").write_text("Project doc", encoding="utf-8")
    deep = repo / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "DEEP.md").write_text("Deep doc", encoding="utf-8")
    manager = _project_manager(fragments, tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(repo, [".vorch/PROJECT.md", "a/b/c/d/DEEP.md"])

    prompt = manager.build_system_prompt(agent, project_context=context)

    assert '<file name=".vorch/PROJECT.md">\nProject doc\n</file>' in prompt
    assert '<file name="a/b/c/d/DEEP.md">\nDeep doc\n</file>' in prompt


def test_project_files_allow_absolute_paths_outside_cwd(
    fragments: dict[str, str],
    tmp_path: Path,
) -> None:
    # An absolute path is read as-is, even outside the project cwd — the auto-load
    # list may name any file the user wants, anywhere on the host.
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere" / "EXTERNAL.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("External doc", encoding="utf-8")
    manager = _project_manager(fragments, tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(repo, [str(outside)])

    prompt = manager.build_system_prompt(agent, project_context=context)

    assert "External doc" in prompt
    assert f'<file name="{outside}">' in prompt


def test_render_project_files_collapses_to_empty_without_files(
    fragments: dict[str, str],
    tmp_path: Path,
) -> None:
    # A bare project (empty repo) renders no project block; the placeholder
    # collapses, so render returns "".
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = _project_manager(fragments, tmp_path)
    context = ProjectPromptContext.from_project(repo, [])

    assert manager.render_project_files(context) == ""
    assert manager.render_project_files(None) == ""


def test_render_project_files_one_source_for_reminder_and_prompt(
    fragments: dict[str, str],
    tmp_path: Path,
) -> None:
    # The visiting reminder reuses the same render as the {project_files} system
    # prompt block — one source, identical framing.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    manager = _project_manager(fragments, tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(repo, [])

    rendered = manager.render_project_files(context)
    in_prompt = manager.build_system_prompt(agent, project_context=context)

    assert rendered == '<file name="AGENTS.md">\nTeam rules\n</file>'
    assert rendered in in_prompt


def test_real_system_md_identity_at_home_is_byte_identical_without_placeholders(
    fragments: dict[str, str],
    workspace: Path,
    tmp_path: Path,
) -> None:
    # Hard requirement ("Identitäts-Agent zu Hause byte-gleich"): with the real
    # bundled root, an identity agent at home (empty body, no project) must get a
    # prompt byte-identical to the same root with the two new placeholders removed.
    # Resolve relative to this test file so the read is cwd-independent.
    repo_root = Path(__file__).resolve().parents[3]
    real_root = (repo_root / "resources" / "prompts" / "system.md").read_text(encoding="utf-8")
    root_without_placeholders = real_root.replace("{agent_body}", "").replace("{project_files}", "")

    with_placeholders = _project_manager(fragments, tmp_path, root=real_root).build_system_prompt(
        _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT_USER)
    )
    without_placeholders = _project_manager(
        fragments, tmp_path, root=root_without_placeholders
    ).build_system_prompt(_agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT_USER))

    assert with_placeholders == without_placeholders


def _agent(
    workspace: str | Path,
    *,
    agent_id: str = "coder",
    allowed_tools: list[str] | None = None,
    allowed_skills: list[str] | None = None,
    custom_system_prompt_enabled: bool = False,
    memory_prompt_mode: MemoryPromptMode = MEMORY_PROMPT_MODE_AGENT_USER,
) -> Agent:
    return Agent(
        id=agent_id,
        name="Coder Agent",
        model="openai/gpt-5.2",
        fallback_model="",
        workspace=str(workspace),
        temperature=0.1,
        thinking_effort="high",
        memory_prompt_mode=memory_prompt_mode,
        allowed_tools=["*"] if allowed_tools is None else allowed_tools,
        allowed_skills=["*"] if allowed_skills is None else allowed_skills,
        custom_system_prompt_enabled=custom_system_prompt_enabled,
        created_at="2026-05-03T12:00:00Z",
        updated_at="2026-05-03T12:00:00Z",
    )


def _filter_by_allowlist(
    definitions: list[dict[str, Any]],
    allowlist: Sequence[str] | None,
) -> list[dict[str, Any]]:
    if allowlist is None or "*" in allowlist:
        return definitions
    return [definition for definition in definitions if definition["name"] in allowlist]
