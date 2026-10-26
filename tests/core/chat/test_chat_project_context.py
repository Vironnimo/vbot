"""Chat boundary tests for explicit, never path-triggered Project Context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.sessions import is_skill_context_note, skill_tool_activation
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


def _write_skill(root: Path, name: str, *, requirements: str = "") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Project workflow.\n{requirements}"
        "---\nUse the Project workflow.",
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
    *,
    requires_env: bool = False,
) -> tuple[Any, list[tuple[str | None, str | None]]]:
    """Resolve the Project "vbot" to a pool with an always-allowed "deploy" Skill."""
    _write_skill(
        tmp_path / "project-skills",
        "deploy",
        requirements=(
            "metadata:\n  vbot:\n    requirements:\n      env: PROJECT_TOKEN\n"
            if requires_env
            else ""
        ),
    )
    global_skills = SkillRegistry.load(tmp_path / "global-skills", environment={})
    project_skills = SkillRegistry.load(
        tmp_path / "project-skills",
        environment={"PROJECT_TOKEN": "test-owned"} if requires_env else {},
        always_allowed=frozenset({"deploy"}),
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
@pytest.mark.parametrize("programmatic", [False, True])
async def test_project_tool_grants_project_skill_in_current_run_without_prompt_change(
    tmp_path: Path,
    programmatic: bool,
) -> None:
    resolve_skills, resolutions = _project_skill_resolver(tmp_path, requires_env=programmatic)
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
                "project_path": "/explicit/project",
                "content": "Project-only instructions.",
                "skills": [{"name": "deploy"}],
            }
        ),
    )
    register_skill_tool(tools, resolve_skills, lambda: None)
    observed: list[ToolContext] = []

    async def program(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        project = await context.call_tool("project", {"project_id": "vbot"})
        assert project["ok"], project
        skill = await context.call_tool("skill", {"name": "deploy"})
        assert skill["ok"], skill
        # These context effects must survive a restart before the outer result.
        persisted = runtime.chat_sessions.get(session_address("coder", "s1")).load()
        assert any(is_skill_context_note(message) for message in persisted)
        assert any(
            message.role == "note" and "Project-only instructions." in str(message.content)
            for message in persisted
        )
        await context.call_tool("probe", {})
        return tool_success({"output": "Only final summary."})

    def probe(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        observed.append(context)
        return tool_success({"content": "private intermediate marker"})

    tools.register("program", "test-sentinel", {"type": "object"}, program)
    tools.register("probe", "test-sentinel", {"type": "object"}, probe)
    responses = (
        [_call("call-program", "program", {})]
        if programmatic
        else [
            _call("call-project", "project", {"project_id": "vbot"}),
            _call("call-skill", "skill", {"name": "deploy"}),
        ]
    )
    runtime, adapter = _skill_runtime(
        tmp_path,
        tools,
        [*responses, {"content": "Done", "tool_calls": None}],
        resolve_skills,
        ["project", "skill", "program", "probe"],
    )
    runtime.chat_sessions.create("coder", session_id="s1")

    await build_chat_loop(runtime).send("coder", "Deploy the Project", session_id="s1")

    [(name, body)] = _loaded_skills(runtime, "s1")
    assert name == "deploy"
    assert "Use the Project workflow." in body
    assert ("vbot", "coder") in resolutions
    system_prompts = [str(request["messages"][0]["content"]) for request in adapter.requests]
    assert len(set(system_prompts)) == 1
    if programmatic:
        assert len(observed) == 1
        assert observed[0].skill_project_id == "vbot"
        assert tuple(observed[0].skill_env_keys) == ("PROJECT_TOKEN",)
        request_text = str(adapter.requests[-1]["messages"])
        assert request_text.count("Project-only instructions.") == 1
        assert "/explicit/project" in request_text
        assert "Use the Project workflow." in request_text
        assert "private intermediate marker" not in request_text


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
    loop = build_chat_loop(runtime)

    await loop.send("coder", "Continue Project work", session_id="loaded")
    loaded_resolutions = list(resolutions)
    resolutions.clear()
    await loop.send("coder", "Continue", session_id="clean")

    assert [name for name, _body in _loaded_skills(runtime, "loaded")] == ["deploy"]
    assert ("vbot", "coder") in loaded_resolutions
    assert ("vbot", "coder") not in resolutions


def _read_call(call_id: str, path: str) -> dict[str, Any]:
    return {"id": call_id, "name": "read", "arguments": {"path": path}}


@pytest.mark.asyncio
async def test_tool_batch_in_a_subfolder_shows_its_folder_instructions_once(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "app" / "ui").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Root rules", encoding="utf-8")
    (repo / "app" / "AGENTS.md").write_text("App rules", encoding="utf-8")
    (repo / "app" / "ui" / "AGENTS.md").write_text("UI rules", encoding="utf-8")

    def read(context: ToolContext, arguments: ToolJsonObject) -> ToolJsonObject:
        context.record_worked_paths(repo / str(arguments["path"]))
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
    adapter = StubAdapter(
        [
            {"content": None, "tool_calls": [_read_call("call-one", "app/ui/x.py")]},
            {"content": None, "tool_calls": [_read_call("call-two", "app/y.py")]},
            {"content": "Done", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["read"]),
        adapter=adapter,
        tools=tools,
        projects=StubProjects(
            {
                "vbot": StubProject(
                    project_id="vbot",
                    cwd=str(repo),
                    auto_load=["AGENTS.md"],
                    display_name="vBot",
                    folder_instructions=True,
                )
            }
        ),
    )
    runtime.chat_sessions.create("coder", session_id="s1", working_project_id="vbot")

    await build_chat_loop(runtime).send("coder", "Work on the UI", session_id="s1")

    # The notes follow the batch's Tool Result, outermost folder first.
    after_first = adapter.requests[1]["messages"]
    tool_index = next(i for i, m in enumerate(after_first) if m.get("role") == "tool")
    reminder = str(after_first[tool_index + 1]["content"])
    assert reminder.index("App rules") < reminder.index("UI rules")
    assert "Folder instructions for `app/ui/` from `app/ui/AGENTS.md`." in reminder
    # The second batch works in a folder already covered: nothing is repeated,
    # and the System Prompt's own file never counts as folder instructions.
    final_request = str(adapter.requests[2]["messages"])
    assert final_request.count("App rules") == 1
    assert final_request.count("UI rules") == 1
    assert "Folder instructions for `.`" not in final_request
    stored = runtime.chat_sessions.get(session_address("coder", "s1")).load()
    assert sum(message.role == "note" for message in stored) == 2
    # A shown file counts as read for the read-before-write guard.
    assert runtime.file_read_state.check_stale("s1", (repo / "app" / "AGENTS.md").resolve()) is None
