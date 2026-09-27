"""Chat boundary tests for explicit, never path-triggered Project Context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.sessions import skill_tool_activation
from core.skills import SkillRegistry
from core.tools import JsonObject as ToolJsonObject
from core.tools import ToolContext, ToolRegistry, tool_success
from core.tools.skill import register_skill_tool
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubProject,
    StubProjects,
    StubRuntime,
    build_chat_loop,
    session_address,
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
        message.role == "note"
        for message in runtime.chat_sessions.get(session_address("coder", "s1")).load()
    )
    assert "visited_projects" not in runtime.chat_sessions.get_metadata(
        session_address("coder", "s1")
    )


def _project_skill_resolver(
    tmp_path: Path,
) -> tuple[Any, list[tuple[str | None, str | None]]]:
    """Resolve the Project "vbot" to a pool with an always-allowed "deploy" Skill."""
    _write_skill(tmp_path / "project-skills", "deploy")
    global_skills = SkillRegistry.load(tmp_path / "global-skills", environment={})
    project_skills = SkillRegistry.load(
        tmp_path / "project-skills", environment={}, always_allowed=frozenset({"deploy"})
    )
    resolutions: list[tuple[str | None, str | None]] = []

    def resolve_skills(project_id: str | None, agent_id: str | None) -> SkillRegistry:
        resolutions.append((project_id, agent_id))
        return project_skills if project_id == "vbot" else global_skills

    return resolve_skills, resolutions


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": None, "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}]}


def _skill_runtime(
    tmp_path: Path,
    tools: ToolRegistry,
    responses: list[dict[str, Any]],
    resolve_skills: Any,
    allowed_tools: list[str],
) -> tuple[Any, StubAdapter]:
    adapter = StubAdapter(responses)
    agent = StubAgent(
        id="coder", model="openai/gpt-5.2", allowed_tools=allowed_tools, allowed_skills=[]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.skills_for = resolve_skills
    return runtime, adapter


def _loaded_skills(runtime: Any, session_id: str) -> list[tuple[str, str]]:
    return [
        (activation[0], activation[1])
        for message in runtime.chat_sessions.get(session_address("coder", session_id)).load()
        if (activation := skill_tool_activation(message)) is not None
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("nesting_depth", [0, 1])
async def test_project_tool_grants_project_skill_in_current_run_without_prompt_change(
    tmp_path: Path,
    nesting_depth: int,
) -> None:
    resolve_skills, resolutions = _project_skill_resolver(tmp_path)
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
    runtime, adapter = _skill_runtime(
        tmp_path,
        tools,
        [
            _call("call-project", "project", {"project_id": "vbot"}),
            _call("call-skill", "skill", {"name": "deploy"}),
            {"content": "Done", "tool_calls": None},
        ],
        resolve_skills,
        ["project", "skill"],
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    loop = build_chat_loop(runtime)
    if nesting_depth > 0:
        loop = loop.child_loop(nesting_depth=nesting_depth)
    await loop.send("coder", "Deploy the Project", session_id="s1")

    [(name, body)] = _loaded_skills(runtime, "s1")
    assert name == "deploy"
    assert "Use the Project workflow." in body
    assert ("vbot", "coder") in resolutions
    system_prompts = [str(request["messages"][0]["content"]) for request in adapter.requests]
    assert system_prompts[0] == system_prompts[1] == system_prompts[2]


@pytest.mark.asyncio
async def test_persisted_project_load_restores_the_skill_scope_of_its_session_only(
    tmp_path: Path,
) -> None:
    resolve_skills, resolutions = _project_skill_resolver(tmp_path)
    tools = ToolRegistry()
    register_skill_tool(tools, resolve_skills, lambda: None)
    runtime, _adapter = _skill_runtime(
        tmp_path,
        tools,
        [
            _call("call-skill", "skill", {"name": "deploy"}),
            {"content": "Done", "tool_calls": None},
            {"content": "Clean", "tool_calls": None},
        ],
        resolve_skills,
        ["skill"],
    )
    loaded = runtime.chat_sessions.create("coder", session_id="loaded")
    runtime.chat_sessions.create("coder", session_id="clean")
    loaded.append(
        ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[ToolCall(id="call-project", name="project", arguments={})],
        )
    )
    loaded.append(
        ChatMessage.tool(
            tool_call_id="call-project",
            name="project",
            content=json.dumps(tool_success({"status": "loaded", "project_id": "vbot"})),
        )
    )
    loop = build_chat_loop(runtime).child_loop(nesting_depth=1)

    await loop.send("coder", "Continue Project work", session_id="loaded")
    loaded_resolutions = list(resolutions)
    resolutions.clear()
    await loop.send("coder", "Continue", session_id="clean")

    assert [name for name, _body in _loaded_skills(runtime, "loaded")] == ["deploy"]
    assert ("vbot", "coder") in loaded_resolutions
    assert ("vbot", "coder") not in resolutions
