"""Project-scoped chat Runs: Session scope, Tool working directory, ToolContext and Agent.

A Run started with a ``project_id`` works in that Project's Session scope, repository and
config Agent; a Run without one keeps the identity scope, the Agent workspace and the
store Agent, even when a Project exists. The runtime carries a real ``ProjectStore`` so the
Project's ``cwd`` travels the same way it does in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.projects import AgentResolutionError, ProjectStore
from core.tools import (
    FileReadState,
    ToolContext,
    ToolRegistry,
    register_apply_patch_tool,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)

JsonObject = dict[str, Any]

PROJECT = "acme"
PROJECT_SESSION = session_address("coder", "session-one", PROJECT)
IDENTITY_SESSION = session_address("coder", "session-one")


def _runtime(
    tmp_path: Path,
    responses: list[JsonObject],
    *,
    tools: ToolRegistry | None = None,
    **options: Any,
) -> tuple[Any, StubAdapter]:
    """A runtime whose ProjectStore holds the ``acme`` Project rooted at ``tmp_path/repo``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    adapter = StubAdapter(responses)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"]),
        adapter=adapter,
        tools=tools or ToolRegistry(),
        **options,
    )
    runtime.projects = ProjectStore(tmp_path)
    runtime.projects.create(PROJECT, "Acme", repo)
    return runtime, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", [PROJECT, None], ids=["project", "identity"])
async def test_run_works_in_its_scopes_session_directory_and_tool_context(
    tmp_path: Path, project_id: str | None
) -> None:
    seen_project_ids: list[str | None] = []

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        seen_project_ids.append(context.project_id)
        return tool_success({})

    tools = ToolRegistry()
    register_apply_patch_tool(tools, file_state=FileReadState())
    tools.register("project_probe", "Report the Project.", {"type": "object"}, probe)
    patch = "*** Add File: out.txt\n+written\n\\ No newline at end of file"
    runtime, _ = _runtime(
        tmp_path,
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_patch", "name": "apply_patch", "arguments": {"patch": patch}},
                    {"id": "call_probe", "name": "project_probe", "arguments": {}},
                ],
            },
            {"content": "Done.", "tool_calls": None},
        ],
        tools=tools,
    )

    await build_chat_loop(runtime).send(
        "coder", "Write a file", session_id="session-one", project_id=project_id
    )

    repo_file = tmp_path / "repo" / "out.txt"
    workspace_file = tmp_path / "agents" / "coder" / "workspace" / "out.txt"
    address, other_address = (
        (PROJECT_SESSION, IDENTITY_SESSION) if project_id else (IDENTITY_SESSION, PROJECT_SESSION)
    )
    written, untouched = (repo_file, workspace_file) if project_id else (workspace_file, repo_file)
    persisted = runtime.chat_sessions.get(address).load()
    assert runtime.chat_runs.get(persisted[-1].run_id).project_id == project_id
    assert not runtime.chat_sessions.exists(other_address)
    assert [message.role for message in persisted] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert written.read_text(encoding="utf-8") == "written"
    assert not untouched.exists()
    assert seen_project_ids == [project_id]


@pytest.mark.asyncio
async def test_project_run_persists_relative_assistant_output_file_reference(
    tmp_path: Path,
) -> None:
    runtime, _ = _runtime(tmp_path, [{"content": "Done: file:result.png", "tool_calls": None}])
    image = tmp_path / "repo" / "result.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    result = await build_chat_loop(runtime).send(
        "coder", "Show the image", session_id="session-one", project_id=PROJECT
    )

    assert result.output_files is not None
    assert [reference.to_dict() for reference in result.output_files] == [
        {"line_index": 0, "path": str(image.resolve()), "start_index": 6, "end_index": 21}
    ]
    persisted = runtime.chat_sessions.get(PROJECT_SESSION).load()
    assistant = next(message for message in persisted if message.role == "assistant")
    assert assistant.output_files == result.output_files


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_id", "model_id"),
    [(PROJECT, "gpt-5.2-config"), (None, "gpt-5.2")],
    ids=["project-config-agent", "identity-store-agent"],
)
async def test_run_resolves_its_agent_in_its_scope(
    tmp_path: Path, project_id: str | None, model_id: str
) -> None:
    # The config Agent shares the Agent id but carries its own model, so the model on
    # the wire shows which profile the Run used.
    config_agent = StubAgent(id="coder", model="openai/gpt-5.2-config", allowed_tools=["*"])
    runtime, adapter = _runtime(
        tmp_path,
        [{"content": "Hello", "tool_calls": None}],
        project_agents={(PROJECT, "coder"): config_agent},
    )

    await build_chat_loop(runtime).send(
        "coder", "Hi", session_id="session-one", project_id=project_id
    )

    assert set(runtime.agent_resolver.calls) == {(project_id, "coder")}
    assert adapter.requests[0]["model_id"] == model_id


@pytest.mark.asyncio
async def test_unresolvable_project_agent_raises_a_resolution_error(tmp_path: Path) -> None:
    runtime, adapter = _runtime(
        tmp_path,
        [{"content": "unused", "tool_calls": None}],
        unresolvable_agents={(PROJECT, "coder")},
    )

    with pytest.raises(AgentResolutionError):
        await build_chat_loop(runtime).send(
            "coder", "Hi", session_id="session-one", project_id=PROJECT
        )

    assert adapter.requests == []
