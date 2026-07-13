"""Phase 4 chat-loop wiring: project files in the system prompt vs. reminder.

These tests cover how the chat loop feeds the prompt builder:

- a **project-born** session (``project_id`` set) hands the config-agent body and
  the project context into ``build_system_prompt`` → body + project files land in
  the **system prompt**;
- an **identity** session (no project) passes empty body / no context → the
  prompt is unchanged;
- the **visiting** path renders the same project files but delivers them as a
  ``<system-reminder>`` (a ``role: "note"``), never in the system prompt.

The doubles are shared with ``test_chat_loop`` (the canonical chat-loop stubs);
only the project-specific wiring is asserted here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatError, ChatLoop
from core.projects.resolver import ConfigAgent
from core.prompts import ProjectPromptContext
from tests.core.chat.test_chat_loop import (
    StubAdapter,
    StubAgent,
    StubProject,
    StubProjects,
    StubRuntime,
)

PROJECT_ID = "vbot"
AGENT_ID = "orchestrator"
MODEL = "openai/gpt-5.2"


def _config_agent(body: str) -> ConfigAgent:
    """A scanned config agent with a verbatim prompt body and a configured model."""
    return ConfigAgent(
        id=AGENT_ID,
        name="Orchestrator",
        model=MODEL,
        temperature=0.1,
        allowed_tools=["*"],
        allowed_skills=["*"],
        body=body,
        source_path=Path(".opencode/agents/orchestrator.md"),
        source_format="opencode",
    )


def _project_runtime(tmp_path: Path, repo: Path, auto_load: list[str], body: str) -> Any:
    identity_agent = StubAgent(id=AGENT_ID, model=MODEL, allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=identity_agent,
        adapter=adapter,
        project_agents={(PROJECT_ID, AGENT_ID): _config_agent(body)},
        projects=StubProjects(
            {PROJECT_ID: StubProject(project_id=PROJECT_ID, cwd=str(repo), auto_load=auto_load)}
        ),
    )
    return runtime, adapter


def _system_message(adapter: StubAdapter) -> str:
    request_messages = adapter.requests[0]["messages"]
    assert request_messages[0]["role"] == "system"
    return str(request_messages[0]["content"])


@pytest.mark.asyncio
async def test_project_session_puts_body_and_files_in_system_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_text("Project context", encoding="utf-8")
    runtime, adapter = _project_runtime(
        tmp_path, repo, ["AGENTS.md", "CONTEXT.md"], body="You are the orchestrator."
    )
    runtime.chat_sessions.create(AGENT_ID, session_id="s1", project_id=PROJECT_ID)

    await ChatLoop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=PROJECT_ID)

    system = _system_message(adapter)
    assert "You are the orchestrator." in system
    assert '<file name="AGENTS.md">\nTeam rules\n</file>' in system
    assert '<file name="CONTEXT.md">\nProject context\n</file>' in system
    # Body and project context were handed to the builder verbatim.
    agent_id, agent_body, project_context = runtime.system_prompts.build_calls[-1]
    assert agent_id == AGENT_ID
    assert agent_body == "You are the orchestrator."
    assert isinstance(project_context, ProjectPromptContext)
    assert project_context.cwd == repo
    assert project_context.auto_load == ("AGENTS.md", "CONTEXT.md")


@pytest.mark.asyncio
async def test_project_session_stamps_project_files_read_before_write(tmp_path: Path) -> None:
    # A project's auto-load files are auto-shown in the system prompt, so they count
    # as read for the session — the agent can edit one directly, with no prior read.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    runtime, _adapter = _project_runtime(
        tmp_path, repo, ["AGENTS.md"], body="You are the orchestrator."
    )
    runtime.chat_sessions.create(AGENT_ID, session_id="s1", project_id=PROJECT_ID)

    await ChatLoop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=PROJECT_ID)

    agents_md = (repo / "AGENTS.md").resolve()
    # Known read for this session; a different session has not seen it.
    assert runtime.file_read_state.check_stale("s1", agents_md) is None
    assert runtime.file_read_state.check_stale("other", agents_md) is not None


@pytest.mark.asyncio
async def test_project_session_body_braces_handed_over_verbatim(tmp_path: Path) -> None:
    # The chat loop passes the body as-is; the builder (tested in test_prompts)
    # guarantees no re-expansion. Here we assert the loop does not mangle braces.
    repo = tmp_path / "repo"
    repo.mkdir()
    body = "Use {memory} and {project_files} literally."
    runtime, adapter = _project_runtime(tmp_path, repo, [], body=body)
    runtime.chat_sessions.create(AGENT_ID, session_id="s1", project_id=PROJECT_ID)

    await ChatLoop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=PROJECT_ID)

    _agent_id, agent_body, _context = runtime.system_prompts.build_calls[-1]
    assert agent_body == body


@pytest.mark.asyncio
async def test_identity_session_passes_no_body_or_project(tmp_path: Path) -> None:
    # An identity session (no project_id) hands empty body / no context to the
    # builder — the prompt stays the unchanged identity prompt.
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Hi", session_id="s1")

    agent_id, agent_body, project_context = runtime.system_prompts.build_calls[-1]
    assert agent_id == "coder"
    assert agent_body == ""
    assert project_context is None


@pytest.mark.asyncio
async def test_visiting_injects_project_files_as_system_reminder(tmp_path: Path) -> None:
    # The visiting path: an identity session reaching into a project. The files
    # arrive as a <system-reminder> note, NOT in the system prompt.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="s1")
    context = ProjectPromptContext.from_project(repo, ["AGENTS.md"])

    loop = ChatLoop(runtime)
    injected = loop.inject_visiting_project_files(session, context, project_name="vBot")
    await loop.send("coder", "Hi", session_id="s1")

    assert injected is True
    # The reminder is embedded as a <system-reminder> synthetic user message, not
    # in the system prompt.
    system = _system_message(adapter)
    assert "Team rules" not in system
    request_messages = adapter.requests[0]["messages"]
    reminder_texts = [
        str(message.get("content", "")) for message in request_messages if message["role"] == "user"
    ]
    joined = "\n".join(reminder_texts)
    assert "<system-reminder>" in joined
    assert f"You are visiting project 'vBot' at '{repo}'." in joined
    assert "The auto-loaded files below are this project's instructions." in joined
    assert "Follow them for every action that affects this project" in joined
    assert "They apply only to this project" in joined
    assert "do not change your home Workspace or cwd" in joined
    assert "Team rules" in joined


@pytest.mark.asyncio
async def test_visiting_with_no_project_files_adds_no_reminder(tmp_path: Path) -> None:
    # A bare/empty project repo: render is empty → no reminder is injected.
    repo = tmp_path / "repo"
    repo.mkdir()
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="s1")
    context = ProjectPromptContext.from_project(repo, [])

    injected = ChatLoop(runtime).inject_visiting_project_files(
        session, context, project_name="vBot"
    )

    assert injected is False
    assert session.load() == []


@pytest.mark.asyncio
async def test_rooted_identity_agent_puts_project_files_in_system_prompt(tmp_path: Path) -> None:
    # A Rooted Identity Agent keeps an identity Session while its explicitly
    # selected Project supplies the auto-load files and working repository.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model=MODEL,
        allowed_tools=["*"],
        workspace=repo,
        root_project_id=PROJECT_ID,
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        projects=StubProjects(
            {PROJECT_ID: StubProject(project_id=PROJECT_ID, cwd=str(repo), auto_load=["AGENTS.md"])}
        ),
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Hi", session_id="s1")

    system = _system_message(adapter)
    assert '<file name="AGENTS.md">\nTeam rules\n</file>' in system
    agent_id, agent_body, project_context = runtime.system_prompts.build_calls[-1]
    assert agent_id == "coder"
    assert agent_body == ""
    assert isinstance(project_context, ProjectPromptContext)
    assert project_context.cwd == repo
    assert project_context.auto_load == ("AGENTS.md",)


@pytest.mark.asyncio
async def test_rooted_identity_agent_resolves_skills_against_home_project(tmp_path: Path) -> None:
    # A Rooted Identity Agent resolves the selected Project's skill pool while
    # retaining its own private Agent layer.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model=MODEL,
        allowed_tools=["*"],
        workspace=repo,
        root_project_id=PROJECT_ID,
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        projects=StubProjects(
            {PROJECT_ID: StubProject(project_id=PROJECT_ID, cwd=str(repo), auto_load=["AGENTS.md"])}
        ),
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Hi", session_id="s1")

    assert (PROJECT_ID, "coder") in runtime.skills_for_calls
    assert (None, "coder") not in runtime.skills_for_calls


@pytest.mark.asyncio
async def test_project_run_resolves_skills_without_identity_agent_layer(tmp_path: Path) -> None:
    # A project run executes a config agent. Its project-local slug must never pull
    # a same-named identity agent's private skill home into the run (private skills
    # bypass the project skill whitelist as always-allowed), so the loop resolves
    # skills with no identity agent id — the project pool only.
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime, _adapter = _project_runtime(tmp_path, repo, [], body="Body.")
    runtime.chat_sessions.create(AGENT_ID, session_id="s1", project_id=PROJECT_ID)

    await ChatLoop(runtime).send(AGENT_ID, "Hi", session_id="s1", project_id=PROJECT_ID)

    assert (PROJECT_ID, None) in runtime.skills_for_calls
    assert all(identity_agent is None for _pid, identity_agent in runtime.skills_for_calls)


@pytest.mark.asyncio
async def test_identity_agent_workspace_not_a_project_stays_unchanged(tmp_path: Path) -> None:
    # An identity agent whose workspace is its own home (not any registered repo)
    # gets no project context — the prompt is the unchanged identity prompt.
    home = tmp_path / "workspace-coder"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["*"], workspace=home)
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        projects=StubProjects(
            {PROJECT_ID: StubProject(project_id=PROJECT_ID, cwd=str(repo), auto_load=["AGENTS.md"])}
        ),
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Hi", session_id="s1")

    _agent_id, agent_body, project_context = runtime.system_prompts.build_calls[-1]
    assert agent_body == ""
    assert project_context is None


@pytest.mark.asyncio
async def test_same_path_identity_agent_without_selection_has_no_project_context(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["*"], workspace=repo)
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        projects=StubProjects(
            {PROJECT_ID: StubProject(project_id=PROJECT_ID, cwd=str(repo), auto_load=["AGENTS.md"])}
        ),
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Hi", session_id="s1")

    assert runtime.system_prompts.build_calls[-1][2] is None
    assert "Team rules" not in _system_message(adapter)


@pytest.mark.asyncio
async def test_rooted_identity_missing_repository_fails_before_user_message(
    tmp_path: Path,
) -> None:
    missing_repo = tmp_path / "missing-repo"
    agent = StubAgent(
        id="coder",
        model=MODEL,
        allowed_tools=["*"],
        workspace=tmp_path / "workspace",
        root_project_id=PROJECT_ID,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([{"content": "Hello", "tool_calls": None}]),
        projects=StubProjects(
            {
                PROJECT_ID: StubProject(
                    project_id=PROJECT_ID,
                    cwd=str(missing_repo),
                    auto_load=["AGENTS.md"],
                )
            }
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="s1")

    with pytest.raises(ChatError, match="Project repository is unavailable"):
        await ChatLoop(runtime).send("coder", "must not persist", session_id="s1")

    assert session.load() == []


@pytest.mark.asyncio
async def test_rooted_identity_missing_project_fails_before_user_message(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model=MODEL,
        allowed_tools=["*"],
        workspace=tmp_path / "workspace",
        root_project_id="missing",
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([{"content": "Hello", "tool_calls": None}]),
        projects=StubProjects({}),
    )
    session = runtime.chat_sessions.create("coder", session_id="s1")

    with pytest.raises(KeyError, match="missing"):
        await ChatLoop(runtime).send("coder", "must not persist", session_id="s1")

    assert session.load() == []
