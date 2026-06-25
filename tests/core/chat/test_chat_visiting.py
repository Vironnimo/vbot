"""Visiting-project trigger: an identity agent reaching into a registered project.

A *visiting* identity agent stays home (cwd unchanged) but reaches into a
registered project's repo by absolute path. When a file tool does so, the chat
loop injects that project's auto-load house-rules (AGENTS.md seeded first) as a
``<system-reminder>`` — once per project per session, recorded in the session
meta. These tests cover the pure detection helpers plus the end-to-end loop
wiring; the reminder *mechanism* itself lives in ``test_chat_prompt``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatLoop, ToolCall
from core.chat.tool_dispatch import _project_containing_path, _visiting_candidate_paths
from core.tools import JsonObject as ToolJsonObject
from core.tools import ToolContext, ToolRegistry, tool_success
from tests.core.chat.test_chat_loop import (
    StubAdapter,
    StubAgent,
    StubProject,
    StubProjects,
    StubRuntime,
)

MODEL = "openai/gpt-5.2"


def _read_tool_registry() -> ToolRegistry:
    """A registry with a minimal ``read`` tool (visit detection reads the call, not the result)."""

    def read(_context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        return tool_success({"ok": True})

    tools = ToolRegistry()
    tools.register(
        "read",
        "Read a file.",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        read,
    )
    return tools


def _read_call(target: str, *, call_id: str = "call_1") -> dict[str, Any]:
    return {
        "content": None,
        "tool_calls": [{"id": call_id, "name": "read", "arguments": {"path": target}}],
    }


def _visiting_runtime(tmp_path: Path, repo: Path, adapter: StubAdapter) -> Any:
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["read"])
    return StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_read_tool_registry(),
        projects=StubProjects(
            {"vbot": StubProject(project_id="vbot", cwd=str(repo), auto_load=["AGENTS.md"])}
        ),
    )


def _reminder_texts(request: dict[str, Any]) -> list[str]:
    return [str(m.get("content", "")) for m in request["messages"] if m["role"] == "user"]


# --- pure detection helpers -------------------------------------------------


def test_candidate_paths_picks_absolute_file_tool_path(tmp_path: Path) -> None:
    target = tmp_path / "repo" / "file.py"
    calls = [ToolCall(id="1", name="read", arguments={"path": str(target)})]
    assert _visiting_candidate_paths(calls) == [target]


def test_candidate_paths_ignores_relative_path() -> None:
    calls = [ToolCall(id="1", name="read", arguments={"path": "src/file.py"})]
    assert _visiting_candidate_paths(calls) == []


def test_candidate_paths_ignores_non_file_tools(tmp_path: Path) -> None:
    # bash takes a free command line, not a single resolvable path: excluded.
    calls = [ToolCall(id="1", name="bash", arguments={"path": str(tmp_path / "x")})]
    assert _visiting_candidate_paths(calls) == []


def test_candidate_paths_ignores_missing_or_blank_path() -> None:
    calls = [
        ToolCall(id="1", name="read", arguments={}),
        ToolCall(id="2", name="read", arguments={"path": "   "}),
        ToolCall(id="3", name="read", arguments={"path": 5}),
    ]
    assert _visiting_candidate_paths(calls) == []


def test_project_containing_path_matches_file_inside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = StubProject(project_id="vbot", cwd=str(repo), auto_load=[])
    assert _project_containing_path(repo / "src" / "file.py", [project]) is project


def test_project_containing_path_matches_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = StubProject(project_id="vbot", cwd=str(repo), auto_load=[])
    assert _project_containing_path(repo, [project]) is project


def test_project_containing_path_returns_none_outside(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = StubProject(project_id="vbot", cwd=str(repo), auto_load=[])
    assert _project_containing_path(tmp_path / "other" / "file.py", [project]) is None


def test_project_containing_path_prefers_nested_project(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    outer_project = StubProject(project_id="outer", cwd=str(outer), auto_load=[])
    inner_project = StubProject(project_id="inner", cwd=str(inner), auto_load=[])
    matched = _project_containing_path(inner / "file.py", [outer_project, inner_project])
    assert matched is inner_project


# --- end-to-end loop wiring -------------------------------------------------


@pytest.mark.asyncio
async def test_file_tool_into_registered_project_injects_house_rules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    adapter = StubAdapter(
        [_read_call(str(repo / "AGENTS.md")), {"content": "Done", "tool_calls": None}]
    )
    runtime = _visiting_runtime(tmp_path, repo, adapter)
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Look at the project", session_id="s1")

    # The house-rules reach the model as a <system-reminder> on the next turn.
    reminders = _reminder_texts(adapter.requests[1])
    assert any("<system-reminder>" in text and "Team rules" in text for text in reminders)
    # They are NOT in the system prompt — the visiting agent stays home.
    assert "Team rules" not in str(adapter.requests[0]["messages"][0]["content"])
    # Persisted as a note and recorded in the session meta.
    persisted = runtime.chat_sessions.get("coder", "s1").load()
    assert any(m.role == "note" and "Team rules" in (m.content or "") for m in persisted)
    assert runtime.chat_sessions.get_metadata("coder", "s1")["visited_projects"] == ["vbot"]


@pytest.mark.asyncio
async def test_house_rules_shown_once_per_session(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    target = str(repo / "AGENTS.md")
    adapter = StubAdapter(
        [
            _read_call(target, call_id="call_1"),
            {"content": "First done", "tool_calls": None},
            _read_call(target, call_id="call_2"),
            {"content": "Second done", "tool_calls": None},
        ]
    )
    runtime = _visiting_runtime(tmp_path, repo, adapter)
    runtime.chat_sessions.create("coder", session_id="s1")

    loop = ChatLoop(runtime)
    await loop.send("coder", "first", session_id="s1")
    await loop.send("coder", "second", session_id="s1")

    persisted = runtime.chat_sessions.get("coder", "s1").load()
    house_rule_notes = [
        m for m in persisted if m.role == "note" and "Team rules" in (m.content or "")
    ]
    assert len(house_rule_notes) == 1


@pytest.mark.asyncio
async def test_file_tool_outside_any_project_injects_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    # The agent reads an absolute path that is NOT inside the registered repo.
    outside = str(tmp_path / "elsewhere" / "note.txt")
    adapter = StubAdapter([_read_call(outside), {"content": "Done", "tool_calls": None}])
    runtime = _visiting_runtime(tmp_path, repo, adapter)
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Read a scratch file", session_id="s1")

    persisted = runtime.chat_sessions.get("coder", "s1").load()
    assert not any(m.role == "note" for m in persisted)
    assert "visited_projects" not in runtime.chat_sessions.get_metadata("coder", "s1")


@pytest.mark.asyncio
async def test_rooted_agent_own_project_not_reinjected_as_reminder(tmp_path: Path) -> None:
    # A rooted identity agent (workspace == the repo) already carries its project's
    # files in the system prompt. Opening its own repo by absolute path must NOT
    # re-inject them as a <system-reminder>: the rooted project is suppressed from
    # the visit trigger (seeded as already-visited).
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Team rules", encoding="utf-8")
    agent = StubAgent(id="coder", model=MODEL, allowed_tools=["read"], workspace=repo)
    adapter = StubAdapter(
        [_read_call(str(repo / "AGENTS.md")), {"content": "Done", "tool_calls": None}]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_read_tool_registry(),
        projects=StubProjects(
            {"vbot": StubProject(project_id="vbot", cwd=str(repo), auto_load=["AGENTS.md"])}
        ),
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    await ChatLoop(runtime).send("coder", "Look at my own repo", session_id="s1")

    # The files are in the system prompt (rooted), and the visit trigger added no
    # reminder note or meta for the agent's own project.
    assert "Team rules" in str(adapter.requests[0]["messages"][0]["content"])
    persisted = runtime.chat_sessions.get("coder", "s1").load()
    assert not any(m.role == "note" and "Team rules" in (m.content or "") for m in persisted)
    assert "visited_projects" not in runtime.chat_sessions.get_metadata("coder", "s1")
