"""Shared stubs and builders for System Prompt tests."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.agents import Agent
from core.channels import ChannelConfig
from core.memory import MEMORY_PROMPT_MODE_AGENT_USER, MemoryPromptMode
from core.prompts.blocks import BlockDefinition, LayoutEntry
from core.prompts.prompts import PromptAgent, SkillPromptMetadata, SystemPromptManager
from core.tools.availability import ToolAccess

_RESOURCES_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "resources" / "prompts"

_CORE_FRAGMENT_NAMES = (
    "identity_runtime.md",
    "runtime.md",
    "working_project.md",
    "tools.md",
    "system_reminders.md",
    "tools_list.md",
    "channels.md",
    "skills.md",
    "skill_maintenance.md",
)

# (name, description, activation, constraints) of the registered stub Tools.
_STUB_TOOLS = (
    ("read", "Read a workspace file", "configurable", ()),
    ("shell", "Run a shell command", "configurable", ()),
    ("memory", "Manage pinned memory", "memory_mode", ("identity_agent",)),
    ("skill", "Load a skill", "configurable", ()),
    ("skill_manage", "Author a skill", "configurable", ("identity_agent",)),
    ("session_read", "Read a Session", "follows", ()),
    ("session_search", "Search Sessions", "configurable", ()),
    ("session_board", "Post to the Session board", "session_grant", ()),
)


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str
    origin: str | None = None


class StubStorage:
    """Storage stub returning the real bundled resource fragments by default.

    Seeding ``read_prompt_fragment`` with the real ``resources/prompts/*.md`` exercises
    the production block texts. Agent-scope fragments default to ``""`` (no default
    fallback), matching the real storage contract.
    """

    def __init__(self, fragments: dict[str, str] | None = None) -> None:
        self._fragments = (
            fragments
            if fragments is not None
            else {
                name: (_RESOURCES_PROMPTS_DIR / name).read_text(encoding="utf-8")
                for name in _CORE_FRAGMENT_NAMES
            }
        )
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
    """Tool registry stub recording the allow-lists and profiles it was asked for."""

    def __init__(self) -> None:
        self.prompt_allowlist_calls: list[list[str] | None] = []
        self.provider_allowlist_calls: list[list[str] | None] = []
        self.prompt_profile_agent_ids: list[str | None] = []
        self.provider_profile_agent_ids: list[str | None] = []

    def list_tools(self) -> list[Any]:
        return [
            SimpleNamespace(
                name=name,
                internal=False,
                activation=activation,
                activation_source="session_search" if activation == "follows" else None,
                constraints=constraints,
            )
            for name, _description, activation, constraints in _STUB_TOOLS
        ]

    def prompt_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        profile_context: Any | None = None,
    ) -> list[dict[str, Any]]:
        self.prompt_allowlist_calls.append(_listed(allowed_tools))
        self.prompt_profile_agent_ids.append(getattr(profile_context, "agent_id", None))
        return _definitions(allowed_tools, with_parameters=False)

    def provider_definitions(
        self,
        allowed_tools: Sequence[str] | None = None,
        *,
        include_internal: bool = False,
        session_grants: Sequence[str] = (),
        ready_only: bool = True,
        profile_context: Any | None = None,
    ) -> list[dict[str, Any]]:
        self.provider_allowlist_calls.append(_listed(allowed_tools))
        self.provider_profile_agent_ids.append(getattr(profile_context, "agent_id", None))
        return _definitions(allowed_tools, with_parameters=True)


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


class StubBlockStore:
    """An in-memory BlockStore: a per-scope layout and a per-(scope, id) override map.

    Implements the read and write surface the block-edit facade uses, with the
    manager's scope keys (``"default"`` / ``"agent:<id>"``).
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

    def write_layout(
        self, scope: str, entries: Sequence[LayoutEntry], *, reset: bool = False
    ) -> None:
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
    block_store: Any | None = None,
    agents: list[PromptAgent] | None = None,
    block_definitions: Sequence[BlockDefinition] = (),
    loaded_extensions: Sequence[str] = (),
) -> SystemPromptManager:
    return SystemPromptManager(
        storage or StubStorage(),
        tools or StubTools(),
        skills or StubSkills([]),
        channel_registry=channels,
        vbot_version="0.1.0",
        vbot_root=tmp_path / "app",
        data_root=tmp_path / "data",
        server_hostname="test-host",
        operating_system="test-os",
        current_local_date=lambda: "2026-05-04",
        timezone_name=lambda: "Europe/Berlin",
        block_definitions=block_definitions,
        loaded_extensions=loaded_extensions,
        block_store=block_store,
        agent_store=StubAgentStore(agents) if agents is not None else None,
    )


def _facade_manager(
    tmp_path: Path,
    *,
    store: Any | None = None,
    agents: list[PromptAgent] | None = None,
) -> SystemPromptManager:
    """A manager wired with the block store and Agent store the edit facade needs."""
    return _manager(
        tmp_path,
        skills=StubSkills([StubSkill("agent-cli", "Delegate")]),
        block_store=store or StubBlockStore(),
        agents=agents,
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
        fallback_models=[],
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


def _listed(allowed_tools: Sequence[str] | None) -> list[str] | None:
    return list(allowed_tools) if allowed_tools is not None else None


def _definitions(
    allowed_tools: Sequence[str] | None, *, with_parameters: bool
) -> list[dict[str, Any]]:
    # skill / skill_manage are ordinary registered Tools, so both surfaces list them;
    # gate 2 of a ``tool:<name>``-owned block reads through the prompt surface.
    definitions: list[dict[str, Any]] = []
    for name, description, _activation, _constraints in _STUB_TOOLS:
        if allowed_tools is not None and "*" not in allowed_tools and name not in allowed_tools:
            continue
        definition: dict[str, Any] = {"name": name, "description": description}
        if with_parameters:
            definition["parameters"] = {"type": "object"}
        definitions.append(definition)
    return definitions
