"""Chat in a Session with a goal: the ``update_goal`` grant and the retained goal note."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat._message_history import effective_compaction_messages
from core.sessions import SESSION_GOAL_META_KEY
from core.tools import ToolRegistry, tool_success
from core.tools.availability import TOOL_ACTIVATION_SESSION_GRANT, UPDATE_GOAL_TOOL_NAME
from tests.core.chat.chat_loop_compaction_test_support import auto_compact, compaction_runtime
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubRuntime,
    build_chat_loop,
    session_address,
)

pytestmark = pytest.mark.asyncio

_GOAL_NOTE = 'The user set a goal for this session with /goal:\n"Ship it"\nWhen your turn ends ...'


def _tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        UPDATE_GOAL_TOOL_NAME,
        "Mark the session goal complete, blocked or paused.",
        {"type": "object", "properties": {"status": {"type": "string"}}},
        lambda _context, _arguments: tool_success({"content": "ok"}),
        open_input_schema=True,
        catalog_visible=False,
        session_scoped=True,
        activation=TOOL_ACTIVATION_SESSION_GRANT,
    )
    return tools


def _set_goal(runtime: Any, session_id: str, status: str | None) -> None:
    if status is None:
        return
    runtime.chat_sessions.mutate_metadata(
        session_address("coder", session_id),
        lambda metadata: metadata.update(
            {SESSION_GOAL_META_KEY: {"objective": "Ship it", "status": status}}
        ),
    )


@pytest.mark.parametrize(
    ("status", "offered"),
    [("active", True), ("blocked", True), ("paused", True), ("complete", False), (None, False)],
)
async def test_update_goal_is_offered_while_the_session_goal_is_open(
    tmp_path: Path, status: str | None, offered: bool
) -> None:
    adapter = StubAdapter([{"content": "Answer", "tool_calls": None}])
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=_tools())
    runtime.chat_sessions.create("coder", session_id="goal")
    _set_goal(runtime, "goal", status)
    loop = build_chat_loop(runtime)

    await (await loop.start_run("coder", "Hi", session_id="goal")).wait()

    [request] = adapter.requests
    tools = {tool["name"] for tool in request["kwargs"].get("tools") or []}
    assert (UPDATE_GOAL_TOOL_NAME in tools) is offered


@pytest.mark.parametrize(("status", "retained"), [("active", True), ("complete", False)])
async def test_compaction_keeps_the_note_of_an_open_goal(
    tmp_path: Path, status: str, retained: bool
) -> None:
    runtime = compaction_runtime(tmp_path, tools=_tools())
    session = runtime.chat_sessions.create("coder", session_id="goal")
    _set_goal(runtime, "goal", status)
    note = ChatMessage.note(_GOAL_NOTE)
    session.append_many(
        [
            note,
            ChatMessage.user("Work"),
            ChatMessage.assistant(model="openai/gpt-5.2", content="Done"),
        ]
    )
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Summary", projection=[], compacted_token_count=10
    )
    loop = build_chat_loop(
        runtime, compaction_service=StubCompactionService(should_auto=True, checkpoint=checkpoint)
    )

    await auto_compact(loop, runtime.agents.get("coder"), session, usage={"input_tokens": 90})

    effective = effective_compaction_messages(session.load_active())
    assert any(message.id == note.id for message in effective) is retained
