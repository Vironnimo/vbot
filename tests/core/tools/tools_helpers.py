"""Shared fixtures and fakes for tools behavior tests."""

from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, cast

from core.tools import (
    Tool,
    ToolContext,
    ToolExecutionConfig,
    ToolRegistry,
    tool_success,
)

JsonObject = dict[str, Any]

READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


def make_context(tool_name: str = "read_file", tool_call_id: str = "call_1") -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_call_index=0,
        workspace=Path("workspace"),
        vbot_root=Path("app"),
        data_root=Path("data"),
    )


def make_execution_config(
    *,
    allowed_tools: list[str] | None = None,
    workspace: Path = Path("workspace"),
) -> ToolExecutionConfig:
    return ToolExecutionConfig(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        workspace=workspace,
        vbot_root=Path("app"),
        data_root=Path("data"),
        allowed_tools=allowed_tools,
    )


def read_file_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return tool_success(
        {
            "content": f"read {arguments['path']}",
            "tool_call_id": context.tool_call_id,
        }
    )


def register_read_file(registry: ToolRegistry) -> Tool:
    return registry.register(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        parameters=READ_FILE_SCHEMA,
        handler=read_file_handler,
    )


def clock_at(moment: datetime) -> type[datetime]:
    """A ``datetime`` whose ``now`` is ``moment``, to patch into a module that reads the clock."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Clock:
            return cast(Clock, moment.astimezone(tz))

    return Clock
