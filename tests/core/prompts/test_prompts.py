"""Tests for system prompt assembly."""

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

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        self.prompt_allowlist = list(allowed_tools) if allowed_tools is not None else None
        tools = [
            {"name": "read_file", "description": "Read a workspace file"},
            {"name": "shell", "description": "Run a shell command"},
        ]
        return _filter_by_allowlist(tools, allowed_tools)

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        self.provider_allowlist = list(allowed_tools) if allowed_tools is not None else None
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
                    allowed_chat_ids=[8506476339],
                    token_env_var="TELEGRAM_BOT_TOKEN",
                    enabled=True,
                ),
                ChannelConfig(
                    id="tg-group",
                    platform="telegram",
                    agent_id="coder",
                    allowed_chat_ids=[111, 222],
                    token_env_var="TELEGRAM_GROUP_TOKEN",
                    enabled=True,
                ),
                ChannelConfig(
                    id="other-agent-channel",
                    platform="telegram",
                    agent_id="other-agent",
                    allowed_chat_ids=[333],
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
    assert tools.prompt_allowlist == ["read_file"]
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
        }
    ]
    assert tools.provider_allowlist == ["read_file"]


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

    assert [definition["name"] for definition in definitions] == ["skill"]


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
    agent = _agent(workspace, allowed_tools=[], allowed_skills=[])

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


def _agent(
    workspace: Path,
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
