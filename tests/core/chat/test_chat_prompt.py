"""System Prompt inputs the chat loop supplies per Session scope, and their Session pins.

A Project Session hands its config-agent body and Working Project to the prompt builder,
a Rooted Identity Agent supplies its selected Project, and any other identity Session
supplies neither. Foreign Project Context is loaded only by the explicit ``project``
Tool and is therefore outside Chat's request-building path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatError
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
        source_format="opencode",
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
    root_project_id: str | None,
    responses: list[dict[str, Any]] | None = None,
    tools: ToolRegistry | None = None,
    allowed_tools: tuple[str, ...] = ("*",),
) -> tuple[Any, StubAdapter]:
    """An identity Session s1 whose Agent workspace is the Project repository."""
    agent = StubAgent(
        id=AGENT_ID,
        model=MODEL,
        allowed_tools=list(allowed_tools),
        workspace=repo,
        root_project_id=root_project_id,
    )
    adapter = StubAdapter(responses or [{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools, projects=_projects(repo)
    )
    runtime.chat_sessions.create(AGENT_ID, session_id="s1")
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
        ("rooted-identity", "", True, (PROJECT_ID, AGENT_ID), True),
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
        root_project_id = PROJECT_ID if scope == "rooted-identity" else None
        runtime, adapter = _identity_runtime(tmp_path, repo, root_project_id=root_project_id)

    await build_chat_loop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=project_id)

    system = _system_message(adapter)
    agent_id, agent_body, project_context = runtime.system_prompts.build_calls[-1]
    # The config-agent body reaches the builder verbatim, braces included.
    assert (agent_id, agent_body) == (AGENT_ID, body)
    # A Project Run never pulls a same-named identity Agent's private Skill home.
    assert set(runtime.skills_for_calls) == {skill_pool}
    # A config agent has no workspace, so it has no SOUL or memory to pin.
    address = session_address(AGENT_ID, "s1", project_id)
    for slot in (PINNED_SOUL_CONTEXT_SLOT, PINNED_MEMORY_FILES_SLOT):
        assert (runtime.chat_sessions.prompt_pin(address, slot) is not None) is personal_pins
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
async def test_config_agent_session_pins_working_project_across_runs(tmp_path: Path) -> None:
    from core.prompts.pinned_context import PINNED_WORKING_PROJECT_CONTEXT_SLOT

    repo = _repo(tmp_path, "Original rules")
    runtime, adapter = _project_runtime(tmp_path, repo, responses=2)
    loop = build_chat_loop(runtime)

    await loop.send(AGENT_ID, "First", session_id="s1", project_id=PROJECT_ID)
    (repo / "AGENTS.md").write_text("Changed between runs", encoding="utf-8")
    await loop.send(AGENT_ID, "Second", session_id="s1", project_id=PROJECT_ID)

    # A Project Config Agent's auto-load files stay pinned for the prompt epoch:
    # an on-disk change must not alter the System Prompt prefix mid-session.
    second_system = _system_message(adapter, 1)
    assert _system_message(adapter) == second_system
    assert "Original rules" in second_system
    assert len(runtime.system_prompts.render_working_project_context_calls) == 1
    project_pin = runtime.chat_sessions.prompt_pin(
        session_address(AGENT_ID, "s1", PROJECT_ID), PINNED_WORKING_PROJECT_CONTEXT_SLOT
    )
    assert project_pin is not None
    assert project_pin["text"] in second_system


@pytest.mark.asyncio
async def test_rooted_project_context_stays_pinned_across_project_tool_call(
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
        root_project_id=PROJECT_ID,
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
@pytest.mark.parametrize("roots", [("alpha", "beta"), ("alpha", None, "beta")])
async def test_rerooting_replaces_project_dependent_pins(
    tmp_path: Path, roots: tuple[str | None, ...]
) -> None:
    # The working Project is re-resolved per Run: re-rooting A->B (directly or via
    # an unrooted Run) must never keep showing A's Working Project or Skill catalog.
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
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["*"], workspace=tmp_path / "ws")
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None} for _ in roots])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, projects=StubProjects(projects)
    )
    runtime.chat_sessions.create("coder", session_id="s1")
    loop = build_chat_loop(runtime)

    for index, root in enumerate(roots):
        runtime.agents._agent = replace(agent, root_project_id=root)
        await loop.send("coder", "Hi", session_id="s1")

        system = _system_message(adapter, index)
        address = session_address("coder", "s1")
        catalog_pin = runtime.chat_sessions.prompt_pin(address, PINNED_SKILL_CATALOG_SLOT)
        assert catalog_pin is not None
        assert catalog_pin["working_project_id"] == root
        if root is None:
            assert "## Working Project" not in system
            assert "rules" not in system
            continue
        other = "beta" if root == "alpha" else "alpha"
        assert f"- Project ID: `{root}`" in system
        assert f"{root} rules" in system
        assert f"{other} rules" not in system
        project_pin = runtime.chat_sessions.prompt_pin(address, PINNED_WORKING_PROJECT_CONTEXT_SLOT)
        assert project_pin is not None
        assert project_pin["working_project_id"] == root

    prompts = runtime.system_prompts
    assert prompts.render_skill_catalog_calls == len(roots)
    rendered_projects = [call.project_id for call in prompts.render_working_project_context_calls]
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
@pytest.mark.parametrize(
    ("root_project_id", "error"),
    [(PROJECT_ID, ChatError), ("missing", KeyError)],
    ids=["missing-repository", "missing-project"],
)
async def test_rooted_identity_without_its_project_fails_before_the_user_message(
    tmp_path: Path, root_project_id: str, error: type[Exception]
) -> None:
    runtime, _adapter = _identity_runtime(
        tmp_path, tmp_path / "missing-repo", root_project_id=root_project_id
    )

    with pytest.raises(error):
        await build_chat_loop(runtime).send(AGENT_ID, "must not persist", session_id="s1")

    session = runtime.chat_sessions.get(session_address(AGENT_ID, "s1"))
    assert [message.role for message in session.load()] == ["error", "run_summary"]
