"""Tests for server delegate run event routing constants and history filtering."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.chat import ChatMessage, ChatSessionManager
from core.chat.runs import TOOL_CALL_STDERR_EVENT, TOOL_CALL_STDOUT_EVENT
from server.delegates import RUN_DELTA_EVENT_TYPES, SERVER_EVENT_TYPES, dispatch_rpc
from server.events import ALLOWED_SERVER_EVENT_TYPES


def test_process_output_deltas_are_sse_only_not_websocket_events() -> None:
    """Process stdout/stderr deltas stream over SSE and do not bridge to WebSocket."""
    process_delta_events = {TOOL_CALL_STDOUT_EVENT, TOOL_CALL_STDERR_EVENT}

    assert process_delta_events <= RUN_DELTA_EVENT_TYPES
    assert process_delta_events.isdisjoint(SERVER_EVENT_TYPES)
    assert process_delta_events.isdisjoint(ALLOWED_SERVER_EVENT_TYPES)


class HistoryAgentStore:
    def get(self, _agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(current_session_id="session-one")


@pytest.mark.asyncio
async def test_chat_history_hides_subagent_batch_completion_note(tmp_path: Path) -> None:
    # Arrange
    chat_sessions = ChatSessionManager(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.add_note("Sub-agent batch completed.\n\nResults:\n- worker/sub-session: Done")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Continuing"))
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            agents=HistoryAgentStore(),
            chat_sessions=chat_sessions,
        )
    )

    # Act
    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent"}},
    )

    # Assert
    assert response["ok"] is True
    assert [message["role"] for message in response["result"]["messages"]] == ["assistant"]
    assert all(
        "Sub-agent batch completed." not in (message.get("content") or "")
        for message in response["result"]["messages"]
    )
