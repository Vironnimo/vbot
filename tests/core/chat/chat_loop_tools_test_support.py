"""Shared builders for chat loop Tool-cycle tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from core.chat import ChatMessage
from core.tools import ToolRegistry
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime

JsonObject = dict[str, Any]

# Safety net for event waits; a passing test never waits this long.
WAIT_SECONDS = 10.0


def tool_turn(*calls: tuple[str, str] | tuple[str, str, Any], **fields: Any) -> JsonObject:
    """One Provider response requesting ``(call_id, name[, arguments])`` Tool Calls."""
    return {
        "content": fields.pop("content", None),
        "tool_calls": [
            {"id": entry[0], "name": entry[1], "arguments": entry[2] if len(entry) > 2 else {}}
            for entry in calls
        ],
        **fields,
    }


def final(content: str) -> JsonObject:
    return {"content": content, "tool_calls": None}


def tool_runtime(
    tmp_path: Path,
    tools: ToolRegistry | None,
    responses: list[JsonObject],
    *,
    allowed_tools: list[str] | None = None,
    model: str = "openai/gpt-5.2",
    workspace: Path | None = None,
    tool_loading: dict[str, Any] | None = None,
    **runtime_options: Any,
) -> Any:
    """A StubRuntime whose Agent ``coder`` may use every registered Tool by default."""
    agent = StubAgent(
        id="coder",
        model=model,
        allowed_tools=["*"] if allowed_tools is None else allowed_tools,
        workspace=workspace,
        tool_loading=tool_loading,
    )
    adapter = runtime_options.pop("adapter", None) or StubAdapter(responses)
    return StubRuntime(
        data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools, **runtime_options
    )


def tool_results(messages: list[ChatMessage]) -> list[JsonObject]:
    return [
        cast(JsonObject, json.loads(cast(str, message.content)))
        for message in messages
        if message.role == "tool"
    ]
