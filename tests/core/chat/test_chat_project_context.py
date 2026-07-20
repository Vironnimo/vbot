"""Chat boundary tests for explicit, never path-triggered Project Context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.tools import JsonObject as ToolJsonObject
from core.tools import ToolContext, ToolRegistry, tool_success
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
        {"type": "object", "properties": {"path": {"type": "string"}}},
        read,
    )
    return tools


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
