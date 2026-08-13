"""Shared stubs, fixtures, and dependencies for System Prompt tests."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
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
from core.prompts.blocks import (
    BlockDefinition,
    LayoutEntry,
    validate_workspace_include,
)
from core.prompts.prompts import (
    SOUL_FRAMING,
    PinnedSkillCatalog,
    ProjectPromptContext,
    PromptAgent,
    PromptError,
    SkillPromptMetadata,
    SystemPromptManager,
)
from core.tools import HISTORY_TOOL_NAME, ToolRegistry, tool_success
from core.tools.availability import ToolAccess

_RESOURCES_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "resources" / "prompts"

_CORE_FRAGMENT_NAMES = (
    "identity_runtime.md",
    "runtime.md",
    "working_project.md",
    "tools.md",
    "tools_list.md",
    "channels.md",
    "skills.md",
    "skill_maintenance.md",
)


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str
    origin: str | None = None


class StubStorage:
    """Storage stub returning the real bundled resource fragments by default.

    The core text blocks read their default text through ``read_prompt_fragment``;
    seeding it with the real ``resources/prompts/*.md`` exercises the production
    block texts. Agent-scope fragments default to ``""`` (no default fallback),
    matching the real storage contract.
    """

    def __init__(self, fragments: dict[str, str] | None = None) -> None:
        self._fragments = fragments if fragments is not None else _real_fragments()
        self._agent_fragments: dict[tuple[str, str], str] = {}
        self.reads: list[tuple[str, str]] = []

    def read_prompt_fragment(self, fragment_name: str) -> str:
        self.reads.append(("default", fragment_name))
        return self._fragments.get(fragment_name, "")

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        self.reads.append((agent_id, fragment_name))
        return self._agent_fragments.get((agent_id, fragment_name), "")

    def set_agent_prompt_fragment(self, agent_id: str, fragment_name: str, content: str) -> None:
        self._agent_fragments[(agent_id, fragment_name)] = content


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
        self.prompt_profile_agent_ids: list[str | None] = []
        self.provider_profile_agent_ids: list[str | None] = []

    def list_tools(self) -> list[Any]:
        return [
            SimpleNamespace(
                name="read_file",
                internal=False,
                activation="configurable",
                constraints=(),
            ),
            SimpleNamespace(
                name="shell",
                internal=False,
                activation="configurable",
                constraints=(),
            ),
            SimpleNamespace(
                name="memory",
                internal=False,
                activation="memory_mode",
                constraints=("identity_agent",),
            ),
            SimpleNamespace(
                name="skill",
                internal=False,
                activation="configurable",
                constraints=(),
            ),
            SimpleNamespace(
                name="skill_manage",
                internal=False,
                activation="configurable",
                constraints=("identity_agent",),
            ),
            SimpleNamespace(
                name="session_read",
                internal=False,
                activation="follows",
                activation_source="session_search",
                constraints=(),
            ),
            SimpleNamespace(
                name="session_search",
                internal=False,
                activation="configurable",
                constraints=(),
            ),
            SimpleNamespace(
                name="history",
                internal=False,
                activation="session_grant",
                constraints=(),
            ),
        ]

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        profile_context: Any | None = None,
    ) -> list[dict[str, Any]]:
        self.prompt_allowlist = list(allowed_tools) if allowed_tools is not None else None
        self.prompt_allowlist_calls.append(self.prompt_allowlist)
        self.prompt_profile_agent_ids.append(getattr(profile_context, "agent_id", None))
        # skill / skill_manage are ordinary registered tools, so the real registry
        # lists them in the prompt surface too (both prompt and provider definitions
        # build from one ``list_tools``). Gate 2 for a ``tool:<name>``-owned block
        # reads through this surface, so it must carry them for parity.
        tools = [
            {"name": "read_file", "description": "Read a workspace file"},
            {"name": "shell", "description": "Run a shell command"},
            {"name": "memory", "description": "Manage pinned memory"},
            {"name": "skill", "description": "Load a skill"},
            {"name": "skill_manage", "description": "Author a skill"},
            {"name": "session_read", "description": "Read a Session"},
            {"name": "session_search", "description": "Search Sessions"},
            {"name": "history", "description": "Read compacted history"},
        ]
        return _filter_by_allowlist(tools, allowed_tools)

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        profile_context: Any | None = None,
    ) -> list[dict[str, Any]]:
        self.provider_allowlist = list(allowed_tools) if allowed_tools is not None else None
        self.provider_allowlist_calls.append(self.provider_allowlist)
        self.provider_profile_agent_ids.append(getattr(profile_context, "agent_id", None))
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
            {
                "name": "skill_manage",
                "description": "Author a skill",
                "parameters": {"type": "object"},
            },
            {
                "name": "session_read",
                "description": "Read a Session",
                "parameters": {"type": "object"},
            },
            {
                "name": "session_search",
                "description": "Search Sessions",
                "parameters": {"type": "object"},
            },
            {
                "name": "history",
                "description": "Read compacted history",
                "parameters": {"type": "object"},
            },
        ]
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

    def list_channels(self) -> list[ChannelConfig]:
        return list(self._channels)


def _real_fragments() -> dict[str, str]:
    return {
        name: (_RESOURCES_PROMPTS_DIR / name).read_text(encoding="utf-8")
        for name in _CORE_FRAGMENT_NAMES
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    (directory / "SOUL.md").write_text("Soul text", encoding="utf-8")
    (directory / "MEMORY.md").write_text("- Memory text\n", encoding="utf-8")
    (directory / "USER.md").write_text("- User text\n", encoding="utf-8")
    return directory


def _manager(
    tmp_path: Path,
    *,
    storage: StubStorage | None = None,
    tools: Any | None = None,
    skills: StubSkills | None = None,
    channels: StubChannels | None = None,
    block_definitions: Sequence[BlockDefinition] = (),
    loaded_extensions: Sequence[str] = (),
    server_hostname: str = "test-host",
    operating_system: str = "test-os",
    current_utc_date: str = "2026-05-04",
) -> SystemPromptManager:
    return SystemPromptManager(
        storage or StubStorage(),
        tools or StubTools(),
        skills or StubSkills([]),
        channel_registry=channels,
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname=server_hostname,
        operating_system=operating_system,
        current_utc_date=lambda: current_utc_date,
        block_definitions=block_definitions,
        loaded_extensions=loaded_extensions,
    )


class StubBlockStore:
    """A BlockStore stub: a per-scope layout + per-(scope, id) override map.

    Implements the full read **and** write surface in memory so the block-edit
    facade (update/reset/set_layout/create/remove/reset_layout) can be unit-tested
    without the on-disk store. The write side keeps the same scope-key convention
    the manager uses (``"default"`` / ``"agent:<id>"``).
    """

    def __init__(
        self,
        *,
        layouts: dict[str, list[LayoutEntry]] | None = None,
        overrides: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._layouts = layouts or {}
        self._overrides = overrides or {}

    def read_layout(self, scope: str) -> list[LayoutEntry]:
        return list(self._layouts.get(scope, []))

    def read_block_override(self, scope: str, block_id: str) -> str | None:
        return self._overrides.get((scope, block_id))

    def write_layout(self, scope: str, entries: Sequence[LayoutEntry]) -> None:
        self._layouts[scope] = list(entries)

    def prune_layout(
        self, scope: str, entries: Sequence[LayoutEntry], known_ids: frozenset[str]
    ) -> None:
        self._layouts[scope] = [entry for entry in entries if entry.id in known_ids]

    def seed_agent_layout(
        self, scope: str, default_layout: Sequence[LayoutEntry], *, overwrite: bool = False
    ) -> None:
        if scope in self._layouts and not overwrite:
            return
        self._layouts[scope] = list(default_layout)

    def write_block_override(self, scope: str, block_id: str, content: str) -> None:
        self._overrides[(scope, block_id)] = content

    def remove_block_override(self, scope: str, block_id: str) -> bool:
        return self._overrides.pop((scope, block_id), None) is not None


def _facade_manager(
    tmp_path: Path,
    *,
    store: StubBlockStore | None = None,
    agents: list[PromptAgent] | None = None,
) -> SystemPromptManager:
    """A manager wired with the block store + agent store the edit facade needs."""
    return SystemPromptManager(
        StubStorage(),
        StubTools(),
        StubSkills([StubSkill("agent-cli", "Delegate")]),
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname="h",
        operating_system="o",
        current_utc_date=lambda: "2026-05-04",
        block_store=store or StubBlockStore(),
        agent_store=StubAgentStore(agents) if agents is not None else None,
    )


def _agent(
    workspace: str | Path,
    *,
    agent_id: str = "coder",
    allowed_tools: list[str] | None = None,
    allowed_skills: list[str] | None = None,
    tools: dict[str, Any] | None = None,
    custom_system_prompt_enabled: bool = False,
    memory_prompt_mode: MemoryPromptMode = MEMORY_PROMPT_MODE_AGENT_USER,
    thinking_effort: str | None = "high",
) -> Agent:
    return Agent(
        id=agent_id,
        name="Coder Agent",
        model="openai/gpt-5.2",
        fallback_model="",
        workspace=str(workspace),
        temperature=0.1,
        thinking_effort=thinking_effort,
        memory_prompt_mode=memory_prompt_mode,
        tool_access=(
            ToolAccess(mode="all")
            if allowed_tools is None or "*" in allowed_tools
            else ToolAccess(mode="selected", allowed=tuple(allowed_tools))
        ),
        allowed_skills=["*"] if allowed_skills is None else allowed_skills,
        tools={} if tools is None else tools,
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


__all__ = [
    "logging",
    "Sequence",
    "dataclass",
    "Path",
    "Any",
    "pytest",
    "Agent",
    "ChannelConfig",
    "MEMORY_PROMPT_MODE_AGENT",
    "MEMORY_PROMPT_MODE_AGENT_USER",
    "MEMORY_PROMPT_MODE_OFF",
    "MemoryPromptMode",
    "BlockDefinition",
    "LayoutEntry",
    "validate_workspace_include",
    "SOUL_FRAMING",
    "PinnedSkillCatalog",
    "ProjectPromptContext",
    "PromptAgent",
    "PromptError",
    "SkillPromptMetadata",
    "SystemPromptManager",
    "HISTORY_TOOL_NAME",
    "ToolRegistry",
    "tool_success",
    "_RESOURCES_PROMPTS_DIR",
    "_CORE_FRAGMENT_NAMES",
    "StubSkill",
    "StubStorage",
    "StubAgentStore",
    "StubTools",
    "StubSkills",
    "StubChannels",
    "_real_fragments",
    "workspace",
    "_manager",
    "StubBlockStore",
    "_facade_manager",
    "_agent",
    "_filter_by_allowlist",
]
