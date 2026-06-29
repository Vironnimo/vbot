"""Tests for the block-model System Prompt assembly.

The manager now assembles the prompt from declared blocks (core text blocks from
the prompt resources, the memory block, the SOUL / project-files / agent-body data
blocks, plus contributed tool/extension blocks) in the bundled default layout
order, gated by the three gates. These tests use the real bundled resource block
texts (what production ships) and an empty block store (no saved layout/overrides),
so they exercise the same path a real identity run takes.
"""

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
from core.prompts.blocks import (
    BlockDefinition,
    LayoutEntry,
    validate_workspace_include,
)
from core.prompts.prompts import (
    ProjectPromptContext,
    PromptAgent,
    PromptError,
    SkillPromptMetadata,
    SystemPromptManager,
)

_RESOURCES_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "resources" / "prompts"
# The core text-block fragment files whose contents are the blocks' default texts.
_CORE_FRAGMENT_NAMES = ("runtime.md", "tools.md", "channels.md", "skills.md")


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
    (directory / "MEMORY.md").write_text("Memory text", encoding="utf-8")
    (directory / "USER.md").write_text("User text", encoding="utf-8")
    return directory


def _manager(
    tmp_path: Path,
    *,
    storage: StubStorage | None = None,
    tools: StubTools | None = None,
    skills: StubSkills | None = None,
    channels: StubChannels | None = None,
    block_definitions: Sequence[BlockDefinition] = (),
    loaded_extensions: Sequence[str] = (),
    host: str = "test-host",
    os_name: str = "test-os",
    current_date: str = "2026-05-04",
) -> SystemPromptManager:
    return SystemPromptManager(
        storage or StubStorage(),
        tools or StubTools(),
        skills or StubSkills([]),
        channel_registry=channels,
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        host=host,
        os_name=os_name,
        current_date=lambda: current_date,
        block_definitions=block_definitions,
        loaded_extensions=loaded_extensions,
    )


# --- Identity-agent assembly (content + order + normalization) ----------------


def test_identity_agent_prompt_assembles_blocks_in_default_layout_order(
    workspace: Path,
    tmp_path: Path,
) -> None:
    tools = StubTools()
    skills = StubSkills(
        [StubSkill("agent-cli", "Delegate coding tasks"), StubSkill("news", "News")]
    )
    channels = StubChannels(
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
    )
    manager = _manager(tmp_path, tools=tools, skills=skills, channels=channels)
    agent = _agent(workspace, allowed_tools=["read_file"], allowed_skills=["agent-cli"])

    prompt = manager.build_system_prompt(agent)

    # Runtime block (variables filled).
    assert "- Host: test-host" in prompt
    assert "- OS: test-os" in prompt
    assert "- App version: 0.1.0" in prompt
    assert "- You are powered by the model openai/gpt-5.2" in prompt
    assert f"{workspace}" in prompt
    assert "- Thinking level: high" in prompt
    assert "- Date: 2026-05-04" in prompt
    # Tools block.
    assert "- read_file: Read a workspace file" in prompt
    assert "shell" not in prompt
    # Channels block (only this agent's active channels).
    assert "## Channels" in prompt
    assert "- tg-private: telegram (default target available)" in prompt
    assert "- tg-group: telegram (explicit target required)" in prompt
    assert "other-agent-channel" not in prompt
    # Skills block.
    assert "<name>agent-cli</name>" in prompt
    assert "<description>Delegate coding tasks</description>" in prompt
    assert "news" not in prompt
    # Data blocks: SOUL + memory files.
    assert "Soul text" in prompt
    assert "Memory text" in prompt
    assert "User text" in prompt
    assert '<file name="SOUL.md">' in prompt
    assert '<file name="MEMORY.md">' in prompt
    assert '<file name="USER.md">' in prompt
    # No leftover placeholders / no "- None" / clean normalization.
    assert "{" not in prompt
    assert "- None" not in prompt
    assert prompt == prompt.strip()
    assert "\n\n\n" not in prompt
    # Order: SOUL < memory < runtime < tools < channels < skills.
    order = ["Soul text", "<memory>", "## Runtime", "## Available Tools", "## Channels"]
    positions = [prompt.index(section) for section in order]
    assert positions == sorted(positions)
    assert prompt.index("## Channels") < prompt.index("## Available Skills")
    # Same agent allowlist drives prompt tools and gate 2's memory-tool check.
    assert tools.prompt_allowlist_calls[0] == ["read_file"]
    assert skills.allowlist == ["agent-cli"]


# --- Memory block (the empty-memory fix is the key regression) ----------------


def test_memory_block_renders_with_empty_memory_files(tmp_path: Path) -> None:
    # The bug fix (D5): the guidance is the block's own text and the owner gate is
    # "memory tool enabled" — so the block appears whenever memory_prompt_mode != off,
    # even with empty/absent MEMORY.md/USER.md (the agent needs the guidance most
    # precisely before the first entry).
    empty_workspace = tmp_path / "empty-ws"
    empty_workspace.mkdir()
    manager = _manager(tmp_path)
    agent = _agent(empty_workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT_USER)

    prompt = manager.build_system_prompt(agent)

    assert "<memory>" in prompt
    assert "declarative facts" in prompt  # the guidance prose
    assert "<file name=" not in prompt  # no memory files to embed


def test_memory_block_absent_when_memory_off(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert "Soul text" in prompt  # SOUL still renders
    assert "<memory>" not in prompt
    assert "Memory text" not in prompt
    assert "User text" not in prompt


def test_memory_block_includes_only_agent_memory(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)

    prompt = manager.build_system_prompt(agent)

    assert "<memory>" in prompt
    assert '<file name="MEMORY.md">' in prompt
    assert "Memory text" in prompt
    assert '<file name="USER.md">' not in prompt
    assert "User text" not in prompt


# --- Channels block: channel-less agent has NO channels block (not "- None") ---


def test_channel_less_agent_has_no_channels_block(workspace: Path, tmp_path: Path) -> None:
    # Adapted from the old "renders - None" test: with no active channels the whole
    # block gates out (owner "channel"), it does not render "- None".
    manager = _manager(tmp_path, channels=StubChannels([]))
    agent = _agent(workspace, allowed_tools=["read_file"])

    prompt = manager.build_system_prompt(agent)

    assert "## Channels" not in prompt
    assert "- None" not in prompt


def test_channels_block_absent_without_channel_registry(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path, channels=None)
    agent = _agent(workspace)

    prompt = manager.build_system_prompt(agent)

    assert "## Channels" not in prompt


# --- SOUL / project-files / agent-body data blocks ----------------------------


def test_soul_block_collapses_without_workspace_file(tmp_path: Path) -> None:
    # A config agent has workspace "" → SOUL block collapses (gate 3); no decoy
    # SOUL from the process CWD is read.
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert '<file name="SOUL.md">' not in prompt


def test_config_agent_body_renders_verbatim(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent, agent_body="You are the orchestrator.")

    assert "You are the orchestrator." in prompt


def test_config_agent_body_with_braces_is_not_expanded(tmp_path: Path) -> None:
    # Plan risk "Body-Wörtlichkeit": the agent body is a data block, never expanded —
    # a "{...}" inside it (even a real vBot placeholder name) survives verbatim.
    manager = _manager(tmp_path)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    body = "Use {host} and {include:SOUL.md} and {generated:tool_list} literally; also {custom}."

    prompt = manager.build_system_prompt(agent, agent_body=body)

    assert body in prompt


def test_project_files_render_in_order_after_memory(workspace: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_text("Project context", encoding="utf-8")
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)
    context = ProjectPromptContext.from_project(repo, ["AGENTS.md", "CONTEXT.md"])

    prompt = manager.build_system_prompt(agent, project_context=context)

    assert '<file name="AGENTS.md">\nTeam rules\n</file>' in prompt
    assert '<file name="CONTEXT.md">\nProject context\n</file>' in prompt
    # Default layout: memory before project files; AGENTS.md before CONTEXT.md.
    assert prompt.index("<memory>") < prompt.index("AGENTS.md")
    assert prompt.index("AGENTS.md") < prompt.index("CONTEXT.md")


def test_project_files_collapse_without_context(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_AGENT)

    prompt = manager.build_system_prompt(agent, project_context=None)

    # No project block; the identity content is unaffected.
    assert "Soul text" in prompt
    assert "<memory>" in prompt


def test_render_project_files_one_source_for_reminder_and_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    manager = _manager(tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(repo, ["AGENTS.md"])

    rendered = manager.render_project_files(context)
    in_prompt = manager.build_system_prompt(agent, project_context=context)

    assert rendered == '<file name="AGENTS.md">\nTeam rules\n</file>'
    assert rendered in in_prompt


def test_project_files_never_abort_run_on_unreadable_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "GOOD.md").write_text("Good doc", encoding="utf-8")
    (repo / "ADIR").mkdir()
    (repo / "BINARY.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    manager = _manager(tmp_path)
    agent = _agent(tmp_path / "empty-ws", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)
    context = ProjectPromptContext.from_project(repo, ["ADIR", "BINARY.md", "GOOD.md"])

    with caplog.at_level(logging.WARNING):
        prompt = manager.build_system_prompt(agent, project_context=context)

    assert '<file name="GOOD.md">\nGood doc\n</file>' in prompt
    assert '<file name="ADIR">' not in prompt
    assert '<file name="BINARY.md">' not in prompt
    assert "Skipping unreadable project file" in caplog.text


# --- Provider tool definitions (unchanged behavior) ---------------------------


def test_provider_tool_definitions_use_same_agent_allowlist(
    workspace: Path, tmp_path: Path
) -> None:
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
    assert tools.provider_allowlist_calls == [["read_file"], ["memory"]]


def test_provider_tool_definitions_omit_memory_when_agent_memory_is_off(
    workspace: Path, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    definitions = manager.provider_tool_definitions(agent)

    assert "memory" not in [definition["name"] for definition in definitions]


def test_provider_tool_definitions_include_internal_skill_when_agent_has_skills(
    workspace: Path, tmp_path: Path
) -> None:
    manager = _manager(tmp_path, skills=StubSkills([StubSkill("debugging", "Debug failures")]))
    agent = _agent(workspace, allowed_tools=[], allowed_skills=["debugging"])

    definitions = manager.provider_tool_definitions(agent)

    assert [definition["name"] for definition in definitions] == ["memory", "skill"]


def test_provider_tool_definitions_omit_internal_skill_when_agent_has_no_skills(
    workspace: Path, tmp_path: Path
) -> None:
    manager = _manager(tmp_path, skills=StubSkills([StubSkill("debugging", "Debug failures")]))
    agent = _agent(workspace, allowed_tools=["read_file"], allowed_skills=[])

    definitions = manager.provider_tool_definitions(agent)

    assert "skill" not in [definition["name"] for definition in definitions]


# --- Contributed tool/extension blocks via the manager ------------------------


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


# --- Workspace include safety (via the SOUL block / unit helper) ---------------


@pytest.mark.parametrize("filename", ["SOUL.md", "CUSTOM.md", "my-notes.txt", "notes.json"])
def test_validate_workspace_include_accepts_safe_flat_filenames(filename: str) -> None:
    validate_workspace_include(filename)  # should not raise


@pytest.mark.parametrize(
    "filename",
    ["../foo", "foo/bar", "/etc/passwd", "C:\\Windows\\system32\\cmd.exe"],
)
def test_validate_workspace_include_rejects_unsafe_paths(filename: str) -> None:
    with pytest.raises(PromptError, match="Unsafe workspace include"):
        validate_workspace_include(filename)


def test_soul_block_wraps_content_in_xml_file_tag(workspace: Path, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    agent = _agent(workspace, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF, allowed_tools=[])

    prompt = manager.build_system_prompt(agent)

    assert '<file name="SOUL.md">\nSoul text\n</file>' in prompt


def test_soul_block_never_aborts_run_on_unreadable_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SOUL.md").mkdir()  # a directory where the file is expected
    manager = _manager(tmp_path)
    agent = _agent(ws, memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    with caplog.at_level(logging.WARNING):
        prompt = manager.build_system_prompt(agent)

    assert '<file name="SOUL.md">' not in prompt
    assert "Skipping unreadable workspace include" in caplog.text


# --- Layout / overrides via an injected block store ---------------------------


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


def test_saved_layout_disables_a_core_block(workspace: Path, tmp_path: Path) -> None:
    # A scope that disables the skills block in its saved layout drops it; the other
    # blocks still default in at their rank.
    layout = [LayoutEntry(id="core:skills", enabled=False, source="core")]
    store = StubBlockStore(layouts={"default": layout})
    manager = SystemPromptManager(
        StubStorage(),
        StubTools(),
        StubSkills([StubSkill("agent-cli", "Delegate")]),
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        host="h",
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
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        host="h",
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


# --- Custom agent scope -------------------------------------------------------


def test_custom_agent_scope_uses_agent_fragments_without_default_fallback(
    workspace: Path, tmp_path: Path
) -> None:
    # An agent scope reads agent fragments with no default fallback: an unset
    # runtime fragment makes the runtime block empty → it collapses.
    storage = StubStorage()
    storage.set_agent_prompt_fragment("coder", "runtime.md", "## Custom Runtime\nHost {host}")
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


# --- Skills block registry override ------------------------------------------


def test_build_system_prompt_skill_registry_override_scopes_skills_block(tmp_path: Path) -> None:
    global_skills = StubSkills([StubSkill("global-skill", "Global only.")])
    project_skills = StubSkills([StubSkill("project-skill", "Project only.")])
    manager = _manager(tmp_path, skills=global_skills)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent, skill_registry=project_skills)

    assert "project-skill" in prompt
    assert "global-skill" not in prompt


def test_skill_catalog_groups_skills_by_origin(tmp_path: Path) -> None:
    skills = StubSkills(
        [
            StubSkill("own-skill", "Mine.", origin="agent"),
            StubSkill("bundled-skill", "Shipped.", origin="bundled"),
            StubSkill("proj-skill", "From the repo.", origin="project:Acme"),
        ]
    )
    manager = _manager(tmp_path, skills=skills)
    agent = _agent("", memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    prompt = manager.build_system_prompt(agent)

    assert '<skill_group label="Bundled skills">' in prompt
    assert '<skill_group label="Your own skills">' in prompt
    assert "Skills from project" in prompt and "Acme" in prompt
    # Plan order: bundled, then project, then the agent's own.
    assert prompt.index("Bundled skills") < prompt.index("Acme") < prompt.index("Your own skills")
    # The catalog stays path-free.
    assert "/skills/" not in prompt


def test_provider_tool_definitions_skill_tool_gated_by_override_registry(tmp_path: Path) -> None:
    global_skills = StubSkills([StubSkill("global-skill", "Global only.")])
    manager = _manager(tmp_path, skills=global_skills)
    agent = _agent("", allowed_tools=["read_file"], memory_prompt_mode=MEMORY_PROMPT_MODE_OFF)

    with_project_skills = manager.provider_tool_definitions(
        agent, skill_registry=StubSkills([StubSkill("project-skill", "Project only.")])
    )
    without_project_skills = manager.provider_tool_definitions(agent, skill_registry=StubSkills([]))

    assert any(definition["name"] == "skill" for definition in with_project_skills)
    assert all(definition["name"] != "skill" for definition in without_project_skills)


# --- Block-edit facade (the prompt.* RPC surface) ----------------------------


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
        app_version="0.1.0",
        app_dir=tmp_path / "app",
        data_root=tmp_path / "data",
        host="h",
        os_name="o",
        current_date=lambda: "2026-05-04",
        block_store=store or StubBlockStore(),
        agent_store=StubAgentStore(agents) if agents is not None else None,
    )


def test_list_blocks_returns_metadata_in_layout_order(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    blocks = manager.list_blocks()

    # Layout order follows the bundled default layout (resources/prompts/layout.json).
    assert [block["id"] for block in blocks] == [
        "core:soul",
        "memory:guidance",
        "core:runtime",
        "core:tools",
        "core:channels",
        "core:skills",
        "core:agent_body",
        "core:project_files",
    ]
    # Ranks are the layout positions, 0-based and contiguous.
    assert [block["rank"] for block in blocks] == list(range(len(blocks)))
    by_id = {block["id"]: block for block in blocks}
    # A core text block: editable, source core, owner always, carries its text.
    tools = by_id["core:tools"]
    assert tools["kind"] == "text"
    assert tools["editable"] is True
    assert tools["source"] == "core"
    assert tools["owner"] == "always"
    assert tools["enabled"] is True
    assert "text" in tools and tools["is_modified"] is False
    # The default scope omits the inheritance badge (T5 is agent-scope only).
    assert "inheritance" not in tools
    # A data block: non-editable, no text payload, channel-owner for channels.
    soul = by_id["core:soul"]
    assert soul["kind"] == "data"
    assert soul["editable"] is False
    assert "text" not in soul
    assert by_id["core:channels"]["owner"] == "channel"
    # The memory block ships under the memory source/owner.
    assert by_id["memory:guidance"]["source"] == "memory"
    assert by_id["memory:guidance"]["owner"] == "memory"


def test_list_blocks_agent_scope_carries_inheritance_flags(tmp_path: Path) -> None:
    # The agent owns an override on the tools block; the default scope overrides the
    # runtime block; the skills block is untouched → owner default.
    store = StubBlockStore(
        overrides={
            ("agent:coder", "core:tools"): "agent tools text",
            ("default", "core:runtime"): "default runtime override",
        }
    )
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = _facade_manager(tmp_path, store=store, agents=[agent])

    blocks = {
        block["id"]: block for block in manager.list_blocks({"type": "agent", "agent_id": "coder"})
    }

    assert blocks["core:tools"]["inheritance"] == "agent_override"
    assert blocks["core:tools"]["text"] == "agent tools text"
    assert blocks["core:tools"]["is_modified"] is True
    assert blocks["core:runtime"]["inheritance"] == "default_override"
    assert blocks["core:runtime"]["is_modified"] is True
    assert blocks["core:skills"]["inheritance"] == "owner_default"
    assert blocks["core:skills"]["is_modified"] is False


def test_update_block_writes_override_and_returns_state(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    result = manager.update_block("core:tools", "## My Tools")

    assert store.read_block_override("default", "core:tools") == "## My Tools"
    assert result["id"] == "core:tools"
    assert result["text"] == "## My Tools"
    assert result["is_modified"] is True


def test_update_block_rejects_data_block(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    with pytest.raises(PromptError, match="not editable"):
        manager.update_block("core:soul", "nope")


def test_reset_block_removes_override_back_to_default(tmp_path: Path) -> None:
    store = StubBlockStore(overrides={("default", "core:tools"): "custom"})
    manager = _facade_manager(tmp_path, store=store)

    result = manager.reset_block("core:tools")

    assert store.read_block_override("default", "core:tools") is None
    assert result["is_modified"] is False


def test_reset_block_agent_scope_falls_back_to_inherited(tmp_path: Path) -> None:
    store = StubBlockStore(
        overrides={
            ("agent:coder", "core:tools"): "agent text",
            ("default", "core:tools"): "default text",
        }
    )
    agent = _agent(tmp_path, custom_system_prompt_enabled=True)
    manager = _facade_manager(tmp_path, store=store, agents=[agent])

    result = manager.reset_block("core:tools", {"type": "agent", "agent_id": "coder"})

    # The agent override is gone; the effective text falls back to the inherited
    # default-scope override (T5 "reset → back to inherited").
    assert store.read_block_override("agent:coder", "core:tools") is None
    assert result["text"] == "default text"
    assert result["inheritance"] == "default_override"


def test_reset_block_rejects_user_block(tmp_path: Path) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "my note"},
    )
    manager = _facade_manager(tmp_path, store=store)

    with pytest.raises(PromptError, match="no default to reset"):
        manager.reset_block("user:note")


def test_set_layout_persists_order_and_prunes_inert_id(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    result = manager.set_layout(
        [
            {"id": "core:skills", "enabled": False},
            {"id": "core:tools", "enabled": True},
            {"id": "extension:gone", "enabled": True},
        ]
    )

    # The contributor-gone id is pruned (tolerate-and-prune, never an error); the
    # live entries keep their order + toggles.
    persisted = store.read_layout("default")
    assert [entry.id for entry in persisted] == ["core:skills", "core:tools"]
    assert persisted[0].enabled is False
    assert [entry["id"] for entry in result["layout"]] == ["core:skills", "core:tools"]


def test_set_layout_keeps_existing_user_block(tmp_path: Path) -> None:
    # A custom block has no contributor definition, so set_layout must not prune it.
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "kept"},
    )
    manager = _facade_manager(tmp_path, store=store)

    result = manager.set_layout(
        [
            {"id": "user:note", "enabled": True},
            {"id": "core:tools", "enabled": True},
        ]
    )

    assert {entry["id"] for entry in result["layout"]} == {"user:note", "core:tools"}


def test_create_block_rejects_bad_slug(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    with pytest.raises(PromptError, match="invalid custom block slug"):
        manager.create_block("../etc/passwd")


def test_create_block_rejects_collision(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="user:note", source="user")]})
    manager = _facade_manager(tmp_path, store=store)

    with pytest.raises(PromptError, match="already exists"):
        manager.create_block("note")


def test_create_block_writes_override_and_layout_entry(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    result = manager.create_block("greeting", "Hello.")

    assert store.read_block_override("default", "user:greeting") == "Hello."
    assert any(entry.id == "user:greeting" for entry in store.read_layout("default"))
    assert result["id"] == "user:greeting"
    assert result["owner"] == "always"
    assert result["kind"] == "text"
    assert result["editable"] is True


def test_create_block_inserts_at_requested_position(tmp_path: Path) -> None:
    store = StubBlockStore()
    manager = _facade_manager(tmp_path, store=store)

    manager.create_block("first", "front", position=0)

    assert store.read_layout("default")[0].id == "user:first"


def test_remove_block_rejects_non_user_id(tmp_path: Path) -> None:
    manager = _facade_manager(tmp_path)

    with pytest.raises(PromptError, match="only custom user blocks"):
        manager.remove_block("core:tools")


def test_remove_block_deletes_override_and_layout_entry(tmp_path: Path) -> None:
    store = StubBlockStore(
        layouts={"default": [LayoutEntry(id="user:note", source="user")]},
        overrides={("default", "user:note"): "my note"},
    )
    manager = _facade_manager(tmp_path, store=store)

    result = manager.remove_block("user:note")

    assert store.read_block_override("default", "user:note") is None
    assert all(entry.id != "user:note" for entry in store.read_layout("default"))
    assert all(entry["id"] != "user:note" for entry in result["layout"])


def test_reset_layout_restores_bundled_default(tmp_path: Path) -> None:
    store = StubBlockStore(layouts={"default": [LayoutEntry(id="core:tools", enabled=False)]})
    manager = _facade_manager(tmp_path, store=store)

    result = manager.reset_layout()

    persisted = store.read_layout("default")
    # The bundled default layout is restored (the disabled-tools custom layout gone).
    assert [entry.id for entry in persisted] == [
        "core:soul",
        "memory:guidance",
        "core:runtime",
        "core:tools",
        "core:channels",
        "core:skills",
        "core:agent_body",
        "core:project_files",
    ]
    assert all(entry.enabled for entry in persisted)
    assert [entry["id"] for entry in result["layout"]] == [entry.id for entry in persisted]


def test_list_scopes_includes_enabled_agent_scopes(tmp_path: Path) -> None:
    enabled = _agent(tmp_path, custom_system_prompt_enabled=True)
    disabled = _agent(tmp_path, agent_id="plain", custom_system_prompt_enabled=False)
    manager = _facade_manager(tmp_path, agents=[disabled, enabled])

    assert manager.list_scopes() == [
        {"type": "default", "label": "Default"},
        {"type": "agent", "agent_id": "coder", "label": "Coder Agent"},
    ]


def test_edit_facade_rejects_disabled_agent_scope(tmp_path: Path) -> None:
    disabled = _agent(tmp_path, custom_system_prompt_enabled=False)
    manager = _facade_manager(tmp_path, agents=[disabled])

    with pytest.raises(PromptError, match="not enabled"):
        manager.list_blocks({"type": "agent", "agent_id": "coder"})


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
