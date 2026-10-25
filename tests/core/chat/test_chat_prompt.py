"""System Prompt inputs the chat loop supplies per Session scope, and their Session pins.

A Project Session hands its config-agent body and Working Project to the prompt builder,
an Identity Session supplies the Project it works in, and an Identity Session working in
its Agent's Workspace supplies neither. Foreign Project Context is loaded only by the
explicit ``project`` Tool and is therefore outside Chat's request-building path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatError
from core.projects import WorkingProjectMissingError
from core.projects.resolver import ConfigAgent
from core.prompts import ProjectPromptContext
from core.tools import ToolContext, ToolRegistry, tool_success
from core.tools.availability import ToolAccess
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubProject,
    StubProjects,
    StubRuntime,
    StubSkill,
    StubSkills,
    build_chat_loop,
    session_address,
)

PROJECT_ID = "vbot"
AGENT_ID = "orchestrator"
MODEL = "openai/gpt-5.2"


@pytest.mark.parametrize("mode", ["off", "agent", "agent_user"])
def test_initial_prompt_files_are_stamped_once(tmp_path: Path, mode: Any) -> None:
    from core.prompts.pinned_context import pinned_memory_files, pinned_soul_context
    from core.tools.file_state import StaleReason
    from tests.core.prompts.prompts_test_support import _agent, _facade_manager

    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        (tmp_path / name).write_text("- ORIGINAL_SENTINEL", encoding="utf-8")
    agent = _agent(tmp_path, memory_prompt_mode=mode)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=cast(Any, agent), adapter=StubAdapter([]))
    runtime.system_prompts = _facade_manager(tmp_path)
    runtime.chat_sessions.create(agent.id, session_id="s1")
    dependencies = build_chat_loop(runtime)._dependencies
    soul = pinned_soul_context(dependencies, agent.id, "s1", agent, None)
    memory = pinned_memory_files(dependencies, agent.id, "s1", agent, None)
    visible = {"SOUL.md"}
    if mode != "off":
        visible.add("MEMORY.md")
    if mode == "agent_user":
        visible.add("USER.md")
    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        path = (tmp_path / name).resolve()
        assert runtime.file_read_state.check_stale("s1", path) == (
            None if name in visible else StaleReason.NEVER_READ
        )
        path.write_text("- CHANGED_SENTINEL_WITH_DIFFERENT_SIZE", encoding="utf-8")
    assert pinned_soul_context(dependencies, agent.id, "s1", agent, None) == soul
    assert pinned_memory_files(dependencies, agent.id, "s1", agent, None) == memory
    for name in visible:
        assert (
            runtime.file_read_state.check_stale("s1", (tmp_path / name).resolve())
            == StaleReason.MODIFIED
        )


@pytest.mark.parametrize("block", ["soul", "memory", "project"])
@pytest.mark.parametrize(
    "winner,previously_read", [("", False), ("earlier snapshot", False), ("earlier snapshot", True)]
)
def test_first_prompt_build_uses_the_concurrent_winning_snapshot(
    tmp_path, monkeypatch, block, winner, previously_read
):
    from core.prompts.pinned_context import (
        pinned_memory_files,
        pinned_soul_context,
        pinned_working_project_context,
    )
    from core.tools.file_state import StaleReason
    from tests.core.prompts.prompts_test_support import _agent, _facade_manager

    filename = {"soul": "SOUL.md", "memory": "MEMORY.md", "project": "AGENTS.md"}[block]
    target = (tmp_path / filename).resolve()
    agent = _agent(tmp_path, memory_prompt_mode="agent")
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=cast(Any, agent), adapter=StubAdapter([]))
    runtime.system_prompts = _facade_manager(tmp_path)
    runtime.chat_sessions.create(agent.id, session_id="s1")
    dependencies = build_chat_loop(runtime)._dependencies
    ensure = runtime.chat_sessions.ensure_prompt_pin

    if previously_read:
        target.write_text("- ORIGINAL", encoding="utf-8")
        runtime.file_read_state.record_read("s1", target)
    target.write_text("- LATER_SNAPSHOT_UNSEEN", encoding="utf-8")

    def first_writer(address, slot, value, matches):
        ensure(address, slot, {**value, "text": winner}, matches)
        return ensure(address, slot, value, matches)

    def snapshot():
        if block == "soul":
            return pinned_soul_context(dependencies, agent.id, "s1", agent, None)
        if block == "memory":
            return pinned_memory_files(dependencies, agent.id, "s1", agent, None)
        project = StubProject(
            project_id=PROJECT_ID,
            cwd=str(tmp_path),
            auto_load=[filename],
            display_name="vBot",
        )
        context = ProjectPromptContext.from_project(
            PROJECT_ID, project.display_name, project.cwd, project.auto_load
        )
        return pinned_working_project_context(
            dependencies, agent.id, "s1", agent, project, context, None
        )

    monkeypatch.setattr(runtime.chat_sessions, "ensure_prompt_pin", first_writer)
    for _ in range(2):
        assert snapshot() == winner
        assert runtime.file_read_state.check_stale("s1", target) == (
            StaleReason.MODIFIED if previously_read else StaleReason.NEVER_READ
        )


@pytest.mark.parametrize(
    "before,after",
    [
        ("off", "agent_user"),
        ("agent_user", "agent"),
        ("agent", "off"),
    ],
)
def test_memory_mode_change_replaces_only_memory_snapshot(tmp_path, before, after):
    from dataclasses import replace

    from core.prompts.pinned_context import pinned_memory_files, pinned_soul_context
    from tests.core.prompts.prompts_test_support import _agent, _facade_manager

    (tmp_path / "SOUL.md").write_text("SOUL_SENTINEL", encoding="utf-8")
    (tmp_path / "USER.md").write_text("- USER_SENTINEL", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("- MEMORY_SENTINEL", encoding="utf-8")
    agent = _agent(tmp_path, memory_prompt_mode=before)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=cast(Any, agent), adapter=StubAdapter([]))
    runtime.system_prompts = _facade_manager(tmp_path)
    runtime.chat_sessions.create(agent.id, session_id="s1")
    dependencies = build_chat_loop(runtime)._dependencies
    soul = pinned_soul_context(dependencies, agent.id, "s1", agent, None)
    pinned_memory_files(dependencies, agent.id, "s1", agent, None)
    (tmp_path / "SOUL.md").write_text("CHANGED_SOUL_SENTINEL", encoding="utf-8")
    agent = replace(agent, memory_prompt_mode=after)
    memory = pinned_memory_files(dependencies, agent.id, "s1", agent, None)
    prompt = runtime.system_prompts.build_system_prompt(
        agent, soul_context=soul, memory_files_context=memory
    )
    assert ("USER_SENTINEL" in prompt) == (after == "agent_user")
    assert ("MEMORY_SENTINEL" in prompt) == (after != "off")
    assert "CHANGED_SOUL_SENTINEL" not in prompt
    assert pinned_soul_context(dependencies, agent.id, "s1", agent, None) == soul
    (tmp_path / "MEMORY.md").write_text("- LATER_MEMORY_SENTINEL", encoding="utf-8")
    assert pinned_memory_files(dependencies, agent.id, "s1", agent, None) == memory


@pytest.mark.parametrize("gate", ["agent", "project"])
def test_shared_memory_pins_refresh_when_its_scope_changes(tmp_path: Path, gate: str) -> None:
    from dataclasses import replace

    from core.prompts.pinned_context import pinned_memory_files
    from tests.core.prompts.prompts_test_support import _agent, _facade_manager

    anchor = tmp_path / "first-anchor"
    anchor.mkdir()
    (anchor / "MEMORY.md").write_text("- FIRST_PROJECT_SENTINEL", encoding="utf-8")
    context = ProjectPromptContext.from_project(
        "first-project", "First", tmp_path, (), memory_workspace=anchor
    )
    agent = _agent("", memory_prompt_mode="off", project_memory_enabled=True)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=cast(Any, agent), adapter=StubAdapter([]))
    runtime.system_prompts = _facade_manager(tmp_path)
    runtime.chat_sessions.create(agent.id, session_id="s1")
    dependencies = build_chat_loop(runtime)._dependencies

    def capture(receiving_agent=agent, project=context):
        return pinned_memory_files(dependencies, agent.id, "s1", receiving_agent, None, project)

    first = capture()
    assert first is not None and "FIRST_PROJECT_SENTINEL" in first
    (anchor / "MEMORY.md").write_text("- UPDATED_PROJECT_SENTINEL", encoding="utf-8")
    assert capture() == first

    disabled = (
        capture(replace(agent, project_memory_enabled=False))
        if gate == "agent"
        else capture(project=replace(context, memory_enabled=False))
    )
    assert disabled == ""
    refreshed = capture()
    assert refreshed is not None and "UPDATED_PROJECT_SENTINEL" in refreshed
    assert "FIRST_PROJECT_SENTINEL" not in refreshed

    other_anchor = tmp_path / "other-anchor"
    other_anchor.mkdir()
    (other_anchor / "MEMORY.md").write_text("- OTHER_PROJECT_SENTINEL", encoding="utf-8")
    other = capture(
        project=replace(context, project_id="other-project", memory_workspace=other_anchor)
    )
    assert other is not None and "OTHER_PROJECT_SENTINEL" in other
    assert "UPDATED_PROJECT_SENTINEL" not in other


BODY = "Use {memory} and {project_files} literally."


def _repo(tmp_path: Path, rules: str = "Team rules") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text(rules, encoding="utf-8")
    return repo


def _projects(repo: Path) -> StubProjects:
    return StubProjects(
        {
            PROJECT_ID: StubProject(
                project_id=PROJECT_ID, cwd=str(repo), auto_load=["AGENTS.md"], display_name="vBot"
            )
        }
    )


def _config_agent(body: str) -> ConfigAgent:
    """A scanned config agent with a verbatim prompt body and a configured model."""
    return ConfigAgent(
        id=AGENT_ID,
        name="Orchestrator",
        model=MODEL,
        temperature=0.1,
        tool_access=ToolAccess(mode="all"),
        allowed_skills=["*"],
        tools={},
        body=body,
        source_path=Path(".opencode/agents/orchestrator.md"),
        source="opencode",
    )


def _project_runtime(tmp_path: Path, repo: Path, responses: int = 1) -> tuple[Any, StubAdapter]:
    """A Project Session s1 whose config agent shares its slug with an identity Agent."""
    identity_agent = StubAgent(id=AGENT_ID, model=MODEL, allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None} for _ in range(responses)])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=identity_agent,
        adapter=adapter,
        project_agents={(PROJECT_ID, AGENT_ID): _config_agent(BODY)},
        projects=_projects(repo),
    )
    runtime.chat_sessions.create(AGENT_ID, session_id="s1", project_id=PROJECT_ID)
    return runtime, adapter


def _identity_runtime(
    tmp_path: Path,
    repo: Path,
    *,
    working_project_id: str | None,
    responses: list[dict[str, Any]] | None = None,
    tools: ToolRegistry | None = None,
    allowed_tools: tuple[str, ...] = ("*",),
) -> tuple[Any, StubAdapter]:
    """An identity Session s1 working in *working_project_id*.

    The Agent's Workspace is the Project repository.
    """
    agent = StubAgent(id=AGENT_ID, model=MODEL, allowed_tools=list(allowed_tools), workspace=repo)
    adapter = StubAdapter(responses or [{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools, projects=_projects(repo)
    )
    runtime.chat_sessions.create(AGENT_ID, session_id="s1", working_project_id=working_project_id)
    return runtime, adapter


def _system_message(adapter: StubAdapter, request: int = 0) -> str:
    request_messages = adapter.requests[request]["messages"]
    assert request_messages[0]["role"] == "system"
    return str(request_messages[0]["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "body", "working_project", "skill_pool", "personal_pins"),
    [
        ("project", BODY, True, (PROJECT_ID, None), False),
        ("identity-in-a-project", "", True, (PROJECT_ID, AGENT_ID), True),
        # Sharing the repository path does not select a Project.
        ("identity", "", False, (None, AGENT_ID), True),
    ],
)
async def test_session_scope_selects_body_working_project_skills_and_pins(
    tmp_path: Path,
    scope: str,
    body: str,
    working_project: bool,
    skill_pool: tuple[str | None, str | None],
    personal_pins: bool,
) -> None:
    from core.prompts.pinned_context import PINNED_MEMORY_FILES_SLOT, PINNED_SOUL_CONTEXT_SLOT

    repo = _repo(tmp_path)
    project_id = PROJECT_ID if scope == "project" else None
    if scope == "project":
        runtime, adapter = _project_runtime(tmp_path, repo)
    else:
        working_project_id = PROJECT_ID if scope == "identity-in-a-project" else None
        runtime, adapter = _identity_runtime(tmp_path, repo, working_project_id=working_project_id)

    await build_chat_loop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=project_id)

    system = _system_message(adapter)
    agent_id, agent_body, project_context = runtime.system_prompts.build_calls[-1]
    # The config-agent body reaches the builder verbatim, braces included.
    assert (agent_id, agent_body) == (AGENT_ID, body)
    # A Project Run never pulls a same-named identity Agent's private Skill home.
    assert set(runtime.skills_for_calls) == {skill_pool}
    # A Project Agent has shared Memory but no personal SOUL.
    address = session_address(AGENT_ID, "s1", project_id)
    assert (
        runtime.chat_sessions.prompt_pin(address, PINNED_SOUL_CONTEXT_SLOT) is not None
    ) is personal_pins
    assert runtime.chat_sessions.prompt_pin(address, PINNED_MEMORY_FILES_SLOT) is not None
    agents_md = (repo / "AGENTS.md").resolve()
    if not working_project:
        assert project_context is None
        assert "Team rules" not in system
        assert runtime.file_read_state.check_stale("s1", agents_md) is not None
        return
    assert isinstance(project_context, ProjectPromptContext)
    assert (project_context.cwd, project_context.auto_load) == (repo, ("AGENTS.md",))
    assert "## Working Project" in system
    assert f"- Project ID: `{PROJECT_ID}`" in system
    assert ' <file name="AGENTS.md">\nTeam rules\n </file>' in system
    # Auto-loaded files count as read for this Session only, so the Agent may edit them.
    assert runtime.file_read_state.check_stale("s1", agents_md) is None
    assert runtime.file_read_state.check_stale("other", agents_md) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["project", "identity-in-a-project", "identity"])
async def test_working_project_memory_reaches_models_for_each_agent_kind(
    tmp_path: Path, scope: str
) -> None:
    from tests.core.prompts.prompts_test_support import _agent, _facade_manager

    repo = _repo(tmp_path)
    project_id = PROJECT_ID if scope == "project" else None
    if project_id:
        runtime, adapter = _project_runtime(tmp_path, repo)
    else:
        runtime, adapter = _identity_runtime(
            tmp_path,
            repo,
            working_project_id=PROJECT_ID if scope == "identity-in-a-project" else None,
        )
        runtime.agents._agent = _agent(repo, agent_id=AGENT_ID, project_memory_enabled=True)
    memory_workspace = runtime.projects.memory_workspace(PROJECT_ID)
    memory_workspace.mkdir(parents=True)
    (memory_workspace / "MEMORY.md").write_text("- SHARED_PROJECT_SENTINEL", encoding="utf-8")
    runtime.system_prompts = _facade_manager(tmp_path)

    await build_chat_loop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=project_id)

    system = _system_message(adapter)
    assert ("SHARED_PROJECT_SENTINEL" in system) is (scope != "identity")
    assert system.count("<memory>") == 1


@pytest.mark.asyncio
async def test_soul_memory_and_skill_catalog_are_pinned_per_session(tmp_path: Path) -> None:
    # A Session's first Run renders SOUL, memory and the Skill catalog once and pins them;
    # its later Runs reuse the pins even after a Skill is added, while a Session that
    # starts afterwards pins the then-current catalog.
    from core.prompts.pinned_context import (
        PINNED_MEMORY_FILES_SLOT,
        PINNED_SKILL_CATALOG_SLOT,
        PINNED_SOUL_CONTEXT_SLOT,
    )

    agent = StubAgent(
        id="coder",
        model=MODEL,
        allowed_tools=["*"],
        workspace=tmp_path / "workspace",
    )
    adapter = StubAdapter(
        [{"content": content, "tool_calls": None} for content in ("One", "Two", "Three")]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    runtime.chat_sessions.create("coder", session_id="s1")
    runtime.chat_sessions.create("coder", session_id="s2")
    loop = build_chat_loop(runtime)
    prompts = runtime.system_prompts

    await loop.send("coder", "First", session_id="s1")
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    await loop.send("coder", "Second", session_id="s1")

    assert (
        prompts.render_soul_calls,
        prompts.render_memory_files_calls,
        prompts.render_skill_catalog_calls,
    ) == (1, 1, 1)
    address = session_address("coder", "s1")
    assert runtime.chat_sessions.prompt_pin(address, PINNED_SOUL_CONTEXT_SLOT) == {
        "text": "Soul of coder"
    }
    assert runtime.chat_sessions.prompt_pin(address, PINNED_MEMORY_FILES_SLOT) == {
        "text": "Memory of coder",
        "mode": "agent_user",
        "memory_project_id": None,
    }
    catalog = runtime.chat_sessions.prompt_pin(address, PINNED_SKILL_CATALOG_SLOT)
    assert catalog is not None and catalog["catalog_text"] == "catalog:1"
    assert [
        (pins["soul_context"], pins["memory_files_context"]) for pins in prompts.build_pin_calls
    ] == [("Soul of coder", "Memory of coder")] * 2

    await loop.send("coder", "Third", session_id="s2")

    later_catalog = runtime.chat_sessions.prompt_pin(
        session_address("coder", "s2"), PINNED_SKILL_CATALOG_SLOT
    )
    assert later_catalog is not None and later_catalog["catalog_text"] == "catalog:2"


@pytest.mark.asyncio
async def test_system_prompt_describes_the_pinned_tools_for_the_whole_epoch(
    tmp_path: Path,
) -> None:
    # Tool-owned blocks and the opt-in tools list follow the prompt epoch's pinned Tool
    # list: a pinned Tool that stops being ready keeps its guidance, and a Tool enabled
    # mid-epoch gets none until Compaction, so the System Prompt stays byte-identical.
    from core.prompts.blocks import BlockDefinition, LayoutEntry
    from tests.core.prompts.prompts_test_support import StubBlockStore, _agent, _manager

    ready = {"probe": True}
    tools = ToolRegistry()
    tools.register(
        "probe",
        "Probe Tool.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({}),
        ready=lambda: ready["probe"],
    )
    agent = _agent(tmp_path / "workspace")
    adapter = StubAdapter([{"content": text, "tool_calls": None} for text in ("One", "Two")])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=cast(Any, agent), adapter=adapter, tools=tools
    )
    runtime.system_prompts = _manager(
        tmp_path,
        tools=tools,
        block_store=StubBlockStore(
            layouts={"default": [LayoutEntry(id="core:tools_list", enabled=True, source="core")]}
        ),
        block_definitions=[
            BlockDefinition(id="tool:probe", owner="tool:probe", default_text="Probe guidance."),
            BlockDefinition(id="tool:late", owner="tool:late", default_text="Late guidance."),
        ],
    )
    runtime.chat_sessions.create(agent.id, session_id="s1")
    loop = build_chat_loop(runtime)

    await loop.send(agent.id, "First", session_id="s1")
    ready["probe"] = False
    tools.register(
        "late", "Late Tool.", {"type": "object"}, lambda _context, _arguments: tool_success({})
    )
    await loop.send(agent.id, "Second", session_id="s1")

    first, second = (str(request["messages"][0]["content"]) for request in adapter.requests)
    assert first == second
    assert "Probe guidance." in first and "- probe: Probe Tool." in first
    assert "Late guidance." not in first and "- late:" not in first


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_inventory", [False, True], ids=["complete", "older-pin"])
async def test_dynamic_blocks_stay_pinned_and_each_change_is_announced_once(
    tmp_path: Path,
    partial_inventory: bool,
) -> None:
    # Project choices live in the Tool definition; Sub-Agent targets and Channels
    # live in prompt blocks. Adding one of each preserves both pinned surfaces and
    # announces the changes once through their respective change mechanism.
    from types import SimpleNamespace

    from core.channels import ChannelConfig
    from core.chat._tool_epoch import tool_change_from_note
    from core.projects import ProjectStore
    from core.prompts import BlockDefinition
    from core.prompts.pinned_context import PINNED_DYNAMIC_BLOCKS_SLOT, PINNED_SKILL_CATALOG_SLOT
    from core.sessions import is_prompt_block_change_note
    from core.subagents import SubAgentPromptTarget
    from core.tools.file_state import FileReadState, StaleReason
    from core.tools.project import register_project_tool
    from core.tools.subagent import register_subagent_tools
    from core.tools.tools import ToolPromptBlockRegistry
    from tests.core.chat.chat_loop_support import history
    from tests.core.prompts.prompts_test_support import _agent, _manager

    projects = ProjectStore(tmp_path / "project-store")
    for project_id in ("vbot", "web"):
        (tmp_path / project_id).mkdir()
    projects.create("vbot", "vBot", tmp_path / "vbot")
    targets = [SubAgentPromptTarget(agent_id="reviewer", name="Reviewer", description="")]
    tools = ToolRegistry()
    prompt_blocks = ToolPromptBlockRegistry()
    register_project_tool(tools, projects, lambda: cast(Any, None), lambda _id: [], FileReadState())
    register_subagent_tools(
        tools,
        cast(
            Any,
            SimpleNamespace(
                spawn=lambda _context, _arguments: tool_success({}),
                message_parent=lambda _context, _arguments: tool_success({}),
                prompt_targets=lambda _agent, _project_id: list(targets),
            ),
        ),
        prompt_blocks,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    included = workspace / "STATIC.md"
    included.write_text("ORIGINAL_INCLUDE", encoding="utf-8")
    agent = _agent(workspace)

    def channel(channel_id: str, chat_ids: list[str]) -> ChannelConfig:
        return ChannelConfig(
            id=channel_id,
            platform="telegram",
            agent_id=agent.id,
            allowed_chat_ids=chat_ids,
            token_env_var="TELEGRAM_BOT_TOKEN",
        )

    channels = [channel("tg-private", ["111"])]
    adapter = StubAdapter([{"content": str(n), "tool_calls": None} for n in range(4)])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=cast(Any, agent), adapter=adapter, tools=tools
    )
    runtime.system_prompts = _manager(
        tmp_path,
        tools=tools,
        channels=cast(Any, SimpleNamespace(list_channels=lambda: list(channels))),
        block_definitions=[
            *prompt_blocks.block_definitions(),
            BlockDefinition(
                id="extension:files",
                owner="always",
                kind="text",
                default_text="{include:STATIC.md}\nSTATIC_SKILLS:{generated:skill_catalog}",
            ),
        ],
    )
    runtime.chat_sessions.create(agent.id, session_id="s1")
    address = session_address(agent.id, "s1")
    runtime.chat_sessions.ensure_prompt_pin(
        address,
        PINNED_SKILL_CATALOG_SLOT,
        {"catalog_text": "PINNED_SKILL_SENTINEL", "working_project_id": None},
        lambda _current: False,
    )
    loop = build_chat_loop(runtime)

    await loop.send(agent.id, "First", session_id="s1")
    assert runtime.file_read_state.check_stale("s1", included.resolve()) is None
    original_pin = runtime.chat_sessions.prompt_pin(address, PINNED_DYNAMIC_BLOCKS_SLOT)
    assert original_pin is not None
    if partial_inventory:
        older_pin = {key: value for key, value in original_pin.items() if key != "order"}
        older_pin["blocks"] = {
            key: value for key, value in older_pin["blocks"].items() if key != "extension:files"
        }
        runtime.chat_sessions.ensure_prompt_pin(
            address, PINNED_DYNAMIC_BLOCKS_SLOT, older_pin, lambda _current: False
        )
        # The newly captured include must count as read even when adoption
        # happens in change planning before the ordinary request build.
        runtime.file_read_state = FileReadState()
        loop = build_chat_loop(runtime)
    await loop.send(agent.id, "Unchanged", session_id="s1")
    assert _system_message(adapter, 0) == _system_message(adapter, 1)
    assert runtime.file_read_state.check_stale("s1", included.resolve()) is None
    adopted = runtime.chat_sessions.prompt_pin(address, PINNED_DYNAMIC_BLOCKS_SLOT)
    assert adopted == original_pin
    assert not any(is_prompt_block_change_note(m) for m in history(runtime, "s1", agent.id))
    included.write_text("CHANGED_INCLUDE_WITH_DIFFERENT_SIZE", encoding="utf-8")
    projects.create("web", "Web", tmp_path / "web")
    targets.append(SubAgentPromptTarget(agent_id="writer", name="Writer", description="Drafts."))
    channels.append(channel("tg-group", ["111", "222"]))
    await loop.send(agent.id, "Second", session_id="s1")
    await loop.send(agent.id, "Third", session_id="s1")

    systems = [_system_message(adapter, request) for request in range(4)]
    assert all(system == systems[0] for system in systems)
    assert "ORIGINAL_INCLUDE" in systems[0]
    assert "STATIC_SKILLS:PINNED_SKILL_SENTINEL" in systems[0]
    assert runtime.file_read_state.check_stale("s1", included.resolve()) == StaleReason.MODIFIED
    assert '<project id="vbot"' not in systems[0] and "`reviewer`" in systems[0]
    assert "- tg-private: telegram" in systems[0]
    assert '<project id="web"' not in systems[0] and "`writer`" not in systems[0]
    assert "tg-group" not in systems[0]
    notes = [
        message
        for message in history(runtime, "s1", agent.id)
        if is_prompt_block_change_note(message)
    ]
    assert len(notes) == 1
    changes = [tool_change_from_note(m) for m in history(runtime, "s1", agent.id)]
    assert [(change.change, change.tool) for change in changes if change is not None] == [
        ("changed", "project")
    ]
    first_tools = adapter.requests[0]["kwargs"]["tools"]
    assert all(request["kwargs"]["tools"] == first_tools for request in adapter.requests)
    project_definition = next(t for t in first_tools if t["name"] == "project")
    assert (
        "- vbot: vBot"
        in project_definition["parameters"]["properties"]["project_id"]["description"]
    )
    for request in adapter.requests[2:]:
        sent = "\n".join(str(message["content"]) for message in request["messages"])
        assert sent.count("- `writer` — Writer — Drafts.") == 1
        assert sent.count("- tg-group: telegram") == 1
        assert sent.count("- web: Web") == 1


@pytest.mark.asyncio
async def test_config_agent_session_pins_body_and_working_project_across_runs(
    tmp_path: Path,
) -> None:
    from core.prompts.pinned_context import (
        PINNED_AGENT_BODY_SLOT,
        PINNED_WORKING_PROJECT_CONTEXT_SLOT,
    )

    repo = _repo(tmp_path, "Original rules")
    runtime, adapter = _project_runtime(tmp_path, repo, responses=2)
    loop = build_chat_loop(runtime)

    await loop.send(AGENT_ID, "First", session_id="s1", project_id=PROJECT_ID)
    (repo / "AGENTS.md").write_text("Changed between runs", encoding="utf-8")
    runtime.agent_resolver._project_agents[(PROJECT_ID, AGENT_ID)] = _config_agent("Edited body")
    await loop.send(AGENT_ID, "Second", session_id="s1", project_id=PROJECT_ID)

    # A Project Config Agent's body and auto-load files stay pinned for the prompt
    # epoch: an edited agent file or an on-disk change must not alter the System
    # Prompt prefix mid-session.
    second_system = _system_message(adapter, 1)
    assert _system_message(adapter) == second_system
    assert "Original rules" in second_system
    assert BODY in second_system and "Edited body" not in second_system
    assert len(runtime.system_prompts.render_working_project_context_calls) == 1
    address = session_address(AGENT_ID, "s1", PROJECT_ID)
    assert runtime.chat_sessions.prompt_pin(address, PINNED_AGENT_BODY_SLOT) == {"text": BODY}
    project_pin = runtime.chat_sessions.prompt_pin(address, PINNED_WORKING_PROJECT_CONTEXT_SLOT)
    assert project_pin is not None
    assert project_pin["text"] in second_system


@pytest.mark.asyncio
async def test_working_project_context_stays_pinned_across_project_tool_call(
    tmp_path: Path,
) -> None:
    from core.prompts.pinned_context import PINNED_WORKING_PROJECT_CONTEXT_SLOT

    repo = _repo(tmp_path, "Original rules")

    def project_tool(_context: ToolContext, _arguments: dict[str, Any]) -> dict[str, Any]:
        (repo / "AGENTS.md").write_text("Changed during project Tool call", encoding="utf-8")
        return tool_success({"status": "loaded"})

    tools = ToolRegistry()
    tools.register(
        "project",
        "Load Project Context.",
        {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        project_tool,
    )
    call = {"id": "call-project", "name": "project", "arguments": {"project_id": PROJECT_ID}}
    runtime, adapter = _identity_runtime(
        tmp_path,
        repo,
        working_project_id=PROJECT_ID,
        responses=[
            {"content": None, "tool_calls": [call]},
            {"content": "Done", "tool_calls": None},
        ],
        tools=tools,
        allowed_tools=("project",),
    )

    await build_chat_loop(runtime).send(AGENT_ID, "Hi", session_id="s1")

    first_system = _system_message(adapter)
    assert "Original rules" in first_system
    assert _system_message(adapter, 1) == first_system
    assert len(runtime.system_prompts.render_working_project_context_calls) == 1
    project_pin = runtime.chat_sessions.prompt_pin(
        session_address(AGENT_ID, "s1"), PINNED_WORKING_PROJECT_CONTEXT_SLOT
    )
    assert project_pin is not None
    assert project_pin["text"] in first_system


@pytest.mark.asyncio
async def test_a_session_keeps_its_working_project_when_the_default_changes(
    tmp_path: Path,
) -> None:
    # The working Project is fixed when a Session is created: a later change of the
    # Agent's default Project starts new Sessions in the new Project but never moves
    # an existing Session, its Working Project block or its Skill catalog.
    from dataclasses import replace

    from core.prompts.pinned_context import (
        PINNED_SKILL_CATALOG_SLOT,
        PINNED_WORKING_PROJECT_CONTEXT_SLOT,
    )

    projects: dict[str, StubProject] = {}
    for project_id in ("alpha", "beta"):
        repo = tmp_path / project_id
        repo.mkdir()
        (repo / "AGENTS.md").write_text(f"{project_id} rules", encoding="utf-8")
        projects[project_id] = StubProject(
            project_id=project_id,
            cwd=str(repo),
            auto_load=["AGENTS.md"],
            display_name=project_id.title(),
        )
    agent = StubAgent(
        id="coder",
        model=MODEL,
        allowed_tools=["*"],
        workspace=tmp_path / "ws",
        root_project_id="alpha",
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None} for _ in range(3)])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, projects=StubProjects(projects)
    )
    loop = build_chat_loop(runtime)

    # Each send to a missing Session creates it in the Agent's then-default Project.
    await loop.send("coder", "Hi", session_id="s1")
    runtime.agents._agent = replace(agent, root_project_id="beta")
    await loop.send("coder", "Again", session_id="s1")
    await loop.send("coder", "Hi", session_id="s2")

    for request, session_id, project_id in ((1, "s1", "alpha"), (2, "s2", "beta")):
        system = _system_message(adapter, request)
        other = "beta" if project_id == "alpha" else "alpha"
        assert f"- Project ID: `{project_id}`" in system
        assert f"{project_id} rules" in system
        assert f"{other} rules" not in system
        address = session_address("coder", session_id)
        assert runtime.chat_sessions.metadata_value(address, "working_project_id") == project_id
        for slot in (PINNED_SKILL_CATALOG_SLOT, PINNED_WORKING_PROJECT_CONTEXT_SLOT):
            pin = runtime.chat_sessions.prompt_pin(address, slot)
            assert pin is not None
            assert pin["working_project_id"] == project_id
    assert _system_message(adapter, 0) == _system_message(adapter, 1)
    rendered_projects = [
        call.project_id for call in runtime.system_prompts.render_working_project_context_calls
    ]
    assert rendered_projects == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_project_run_reads_its_working_project_off_the_event_loop(tmp_path: Path) -> None:
    import threading

    runtime, _adapter = _project_runtime(tmp_path, _repo(tmp_path))
    get_project = runtime.projects.get
    threads: list[int] = []

    def recording_get(project_id: str) -> Any:
        threads.append(threading.get_ident())
        return get_project(project_id)

    runtime.projects.get = recording_get

    await build_chat_loop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=PROJECT_ID)

    assert threads
    assert threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_identity_session_without_its_project_repository_fails_before_the_user_message(
    tmp_path: Path,
) -> None:
    runtime, _adapter = _identity_runtime(
        tmp_path, tmp_path / "missing-repo", working_project_id=PROJECT_ID
    )

    with pytest.raises(ChatError):
        await build_chat_loop(runtime).send(AGENT_ID, "must not persist", session_id="s1")

    session = runtime.chat_sessions.get(session_address(AGENT_ID, "s1"))
    assert [message.role for message in session.load()] == ["error", "run_summary"]


@pytest.mark.asyncio
async def test_a_session_whose_project_is_gone_refuses_runs_until_the_project_returns(
    tmp_path: Path,
) -> None:
    # The Session never falls back to the Workspace: it refuses Runs while its
    # Project is gone and works there again once the Project is restored.
    runtime, adapter = _identity_runtime(tmp_path, _repo(tmp_path), working_project_id=PROJECT_ID)
    project = runtime.projects._projects.pop(PROJECT_ID)
    loop = build_chat_loop(runtime)

    with pytest.raises(WorkingProjectMissingError) as refused:
        await loop.send(AGENT_ID, "must not persist", session_id="s1")

    assert str(refused.value) == (
        "This Session works in Project vbot, which no longer exists. Restore the Project "
        "to continue this Session, or start a new Session."
    )
    address = session_address(AGENT_ID, "s1")
    assert runtime.chat_sessions.get(address).load() == []
    assert adapter.requests == []

    runtime.projects._projects[PROJECT_ID] = project
    await loop.send(AGENT_ID, "Hi", session_id="s1")

    assert f"- Project ID: `{PROJECT_ID}`" in _system_message(adapter)
