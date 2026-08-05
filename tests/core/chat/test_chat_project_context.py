"""Chat boundary tests for explicit, never path-triggered Project Context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.sessions import skill_tool_activation
from core.skills import SkillRegistry
from core.tools import JsonObject as ToolJsonObject
from core.tools import ToolContext, ToolRegistry, tool_success
from core.tools.skill import register_skill_tool
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.core.chat.test_chat_loop import (
    StubAdapter,
    StubAgent,
    StubProject,
    StubProjects,
    StubRuntime,
)


def _read_tool_registry() -> ToolRegistry:
    def read(_context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        return tool_success({"content": "read"})

    tools = ToolRegistry()
    tools.register(
        "read",
        "Read a file.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        read,
    )
    return tools


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Project workflow.\n---\nUse the Project workflow.",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_absolute_file_access_does_not_auto_load_project_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("Project-only rules", encoding="utf-8")
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-one",
                        "name": "read",
                        "arguments": {"path": str(agents_file)},
                    }
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["read"])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=_read_tool_registry(),
        projects=StubProjects(
            {
                "vbot": StubProject(
                    project_id="vbot",
                    cwd=str(repo),
                    auto_load=["AGENTS.md"],
                    display_name="vBot",
                )
            }
        ),
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    await build_chat_loop(runtime).send("coder", "Read the absolute file", session_id="s1")

    request_text = str(adapter.requests[1]["messages"])
    assert "Project-only rules" not in request_text
    assert "system-reminder" not in request_text
    assert not any(
        message.role == "note" for message in runtime.chat_sessions.get("coder", "s1").load()
    )
    assert "visited_projects" not in runtime.chat_sessions.get_metadata("coder", "s1")


@pytest.mark.asyncio
@pytest.mark.parametrize("nesting_depth", [0, 1])
async def test_project_tool_grants_project_skill_in_current_run_without_prompt_change(
    tmp_path: Path,
    nesting_depth: int,
) -> None:
    global_skill_root = tmp_path / "global-skills"
    project_skill_root = tmp_path / "project-skills"
    _write_skill(project_skill_root, "deploy")
    global_skills = SkillRegistry.load(global_skill_root, environment={})
    project_skills = SkillRegistry.load(
        project_skill_root,
        environment={},
        always_allowed=frozenset({"deploy"}),
    )
    skill_resolutions: list[tuple[str | None, str | None]] = []

    def resolve_skills(project_id: str | None, agent_id: str | None) -> SkillRegistry:
        skill_resolutions.append((project_id, agent_id))
        return project_skills if project_id == "vbot" else global_skills

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
        lambda _context, arguments: tool_success(
            {
                "status": "loaded",
                "project_id": arguments["project_id"],
                "skills": [{"name": "deploy"}],
            }
        ),
    )
    register_skill_tool(tools, resolve_skills, lambda: None)
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-project",
                        "name": "project",
                        "arguments": {"project_id": "vbot"},
                    }
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-skill",
                        "name": "skill",
                        "arguments": {"name": "deploy"},
                    }
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["project", "skill"],
        allowed_skills=[],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
    )
    runtime.skills_for = resolve_skills
    runtime.chat_sessions.create("coder", session_id="s1")

    loop = build_chat_loop(runtime)
    if nesting_depth > 0:
        loop = loop.child_loop(nesting_depth=nesting_depth)
    await loop.send("coder", "Deploy the Project", session_id="s1")

    messages = runtime.chat_sessions.get("coder", "s1").load()
    loaded = [
        activation
        for message in messages
        if (activation := skill_tool_activation(message)) is not None
    ]
    assert len(loaded) == 1
    assert loaded[0][0] == "deploy"
    assert "Use the Project workflow." in loaded[0][1]
    assert ("vbot", "coder") in skill_resolutions
    system_prompts = [str(request["messages"][0]["content"]) for request in adapter.requests]
    assert system_prompts[0] == system_prompts[1] == system_prompts[2]


@pytest.mark.asyncio
async def test_loaded_project_skill_grant_is_recovered_in_later_run(tmp_path: Path) -> None:
    global_skills = SkillRegistry.load(tmp_path / "global-skills", environment={})
    project_skill_root = tmp_path / "project-skills"
    _write_skill(project_skill_root, "deploy")
    project_skills = SkillRegistry.load(
        project_skill_root,
        environment={},
        always_allowed=frozenset({"deploy"}),
    )

    def resolve_skills(project_id: str | None, _agent_id: str | None) -> SkillRegistry:
        return project_skills if project_id == "vbot" else global_skills

    tools = ToolRegistry()
    register_skill_tool(tools, resolve_skills, lambda: None)
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-skill",
                        "name": "skill",
                        "arguments": {"name": "deploy"},
                    }
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["skill"],
        allowed_skills=[],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
    )
    runtime.skills_for = resolve_skills
    session = runtime.chat_sessions.create("coder", session_id="s1")
    session.append(
        ChatMessage.tool(
            tool_call_id="call-project",
            name="project",
            content=json.dumps(tool_success({"status": "loaded", "project_id": "vbot"})),
        )
    )

    await (
        build_chat_loop(runtime)
        .child_loop(nesting_depth=1)
        .send("coder", "Continue Project work", session_id="s1")
    )

    activations = [
        activation
        for message in session.load()
        if (activation := skill_tool_activation(message)) is not None
    ]
    assert [activation[0] for activation in activations] == ["deploy"]


@pytest.mark.asyncio
async def test_loaded_project_skill_scope_is_recovered_per_session(tmp_path: Path) -> None:
    skills = SkillRegistry.load(tmp_path / "skills", environment={})
    resolutions: list[tuple[str | None, str | None]] = []

    def resolve_skills(project_id: str | None, agent_id: str | None) -> SkillRegistry:
        resolutions.append((project_id, agent_id))
        return skills

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {"content": "First", "tool_calls": None},
            {"content": "Second", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills_for = resolve_skills
    loaded_session = runtime.chat_sessions.create("coder", session_id="loaded")
    clean_session = runtime.chat_sessions.create("coder", session_id="clean")
    loaded_session.append(
        ChatMessage.tool(
            tool_call_id="call-project",
            name="project",
            content=json.dumps(tool_success({"status": "loaded", "project_id": "vbot"})),
        )
    )
    loop = build_chat_loop(runtime)

    await loop.send("coder", "Continue", session_id=loaded_session.id)
    loaded_resolutions = list(resolutions)
    resolutions.clear()
    await loop.send("coder", "Continue", session_id=clean_session.id)

    assert ("vbot", "coder") in loaded_resolutions
    assert ("vbot", "coder") not in resolutions
