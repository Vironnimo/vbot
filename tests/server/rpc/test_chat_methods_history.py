"""Tests for chat methods history."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
    ChatSessionManager,
)
from core.chat.continuation import CONTINUATION_RECORD_VERSION
from core.runs import (
    ChatRunManager,
)
from core.sessions import ChatSession
from core.sessions.format import write_bootstrap_marker
from core.tools.tools import tool_success
from server.rpc import chat_methods
from server.rpc.methods import dispatch_rpc


class HistoryAgentStore:
    def get(self, _agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(current_session_id="session-one")


def _history_state(tmp_path: Path) -> tuple[SimpleNamespace, ChatSessionManager]:
    write_bootstrap_marker(tmp_path)
    chat_sessions = ChatSessionManager(tmp_path)
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            agents=HistoryAgentStore(),
            chat_sessions=chat_sessions,
        ),
        chat_runs=ChatRunManager(),
    )
    return state, chat_sessions


def _history_message(index: int) -> ChatMessage:
    message = ChatMessage.user(f"Message {index}")
    return replace(message, id=f"message-{index:03d}")


@pytest.mark.asyncio
async def test_chat_history_hides_subagent_batch_completion_note(tmp_path: Path) -> None:
    # Arrange
    write_bootstrap_marker(tmp_path)
    chat_sessions = ChatSessionManager(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.add_note("Sub-agent batch completed.\n\nResults:\n- worker/sub-session: Done")
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Continuing"))
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            agents=HistoryAgentStore(),
            chat_sessions=chat_sessions,
        ),
        chat_runs=ChatRunManager(),
    )

    # Act
    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent"}},
    )

    # Assert
    assert response["ok"] is True
    assert [message["role"] for message in response["result"]["messages"]] == ["assistant"]


@pytest.mark.asyncio
async def test_chat_history_hides_internal_continuation_checkpoint(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.append_continuation_records(
        [
            {
                "version": CONTINUATION_RECORD_VERSION,
                "type": "run_started",
                "run_id": "run-one",
                "timestamp": "2026-07-11T12:00:00+00:00",
                "checkpoint_id": "checkpoint-one",
                "origin_run_id": "run-one",
                "request": "work",
            },
            {
                "version": CONTINUATION_RECORD_VERSION,
                "type": "run_interrupted",
                "run_id": "run-one",
                "timestamp": "2026-07-11T12:00:01+00:00",
                "cause": "network",
            },
        ]
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "session_id": "session-one"},
        },
    )

    assert response["ok"] is True
    assert "continuation" not in response["result"]


@pytest.mark.asyncio
async def test_chat_history_projects_only_active_edit_lineage_but_keeps_raw_usage(
    tmp_path: Path,
) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    first_user = replace(ChatMessage.user("original"), id="user-original")
    first_answer = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="old answer",
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    edited_user = replace(ChatMessage.user("edited"), id="user-edited")
    edited_answer = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="new answer",
        usage={"input_tokens": 20, "output_tokens": 3},
    )
    session.append_many(
        [
            first_user,
            first_answer,
            ChatMessage.history_edit(first_user.id),
            edited_user,
            edited_answer,
        ]
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "session_id": "session-one"},
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["content"] for message in result["messages"]] == [
        "edited",
        "new answer",
    ]
    assert result["messages"][0]["editable"] is True
    assert result["session_usage"]["input_tokens"] == 30
    assert result["session_usage"]["output_tokens"] == 5


@pytest.mark.asyncio
async def test_chat_history_projects_durable_background_bash_statuses(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.append(
        ChatMessage.tool(
            tool_call_id="bash-one",
            name="bash",
            content=json.dumps(
                tool_success(
                    {
                        "process_id": "process-one",
                        "status": "running",
                        "delivery": "automatic",
                    }
                )
            ),
        )
    )
    session.add_note(
        "Automatic completion delivery\n\n"
        "### Bash process — failed\n"
        "Process ID: process-one\n"
        "Command: npm test"
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "session_id": "session-one"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["background_bash_statuses"] == {"process-one": "failed"}
    assert all(message["role"] != "note" for message in response["result"]["messages"])


@pytest.mark.asyncio
async def test_chat_history_limit_returns_newest_visible_messages(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    for index in range(1, 6):
        session.append(_history_message(index))

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 2}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "message-004",
        "message-005",
    ]
    assert result["has_more"] is True
    assert result["next_before"].startswith("vh1.")


@pytest.mark.asyncio
async def test_chat_history_cursor_is_unambiguous_when_message_ids_repeat(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    messages = [
        replace(ChatMessage.user("zero"), id="zero"),
        replace(ChatMessage.user("older duplicate"), id="duplicate"),
        replace(ChatMessage.user("two"), id="two"),
        replace(ChatMessage.user("newer duplicate"), id="duplicate"),
        replace(ChatMessage.user("four"), id="four"),
    ]
    session.append_many(messages)

    first = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 2}},
    )
    second = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {
                "agent_id": "parent",
                "limit": 2,
                "before": first["result"]["next_before"],
            },
        },
    )

    assert [message["content"] for message in second["result"]["messages"]] == [
        "older duplicate",
        "two",
    ]


@pytest.mark.asyncio
async def test_chat_history_does_not_load_a_complete_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    for index in range(1, 6):
        session.append(_history_message(index))

    def fail_full_load(self: ChatSession) -> list[ChatMessage]:
        raise AssertionError("chat.history must use its bounded Session read model")

    monkeypatch.setattr(ChatSession, "load", fail_full_load)
    monkeypatch.setattr(ChatSession, "load_active", fail_full_load)

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 2}},
    )

    assert response["ok"] is True
    assert [message["id"] for message in response["result"]["messages"]] == [
        "message-004",
        "message-005",
    ]


@pytest.mark.asyncio
async def test_chat_history_expands_limit_to_complete_oldest_run_segment(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    timing = {
        "started_at": "2026-07-24T10:00:00+00:00",
        "completed_at": "2026-07-24T10:00:01+00:00",
        "duration_ms": 1000,
    }
    messages = [
        replace(ChatMessage.user("first"), id="first-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="first result"),
            id="first-assistant",
        ),
        replace(
            ChatMessage.run_summary(
                run_id="run-one",
                status="completed",
                timing=timing,
                iteration_count=1,
            ),
            id="first-summary",
        ),
        replace(ChatMessage.user("second"), id="second-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="second result"),
            id="second-assistant",
        ),
        replace(
            ChatMessage.run_summary(
                run_id="run-two",
                status="completed",
                timing=timing,
                iteration_count=1,
            ),
            id="second-summary",
        ),
    ]
    for message in messages:
        session.append(message)

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 2}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "second-user",
        "second-assistant",
        "second-summary",
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_chat_history_expanded_page_cursor_skips_excluded_run_boundary(
    tmp_path: Path,
) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    timing = {
        "started_at": "2026-07-24T10:00:00+00:00",
        "completed_at": "2026-07-24T10:00:01+00:00",
        "duration_ms": 1000,
    }
    session.append_many(
        [
            replace(ChatMessage.user("first"), id="first-user"),
            replace(
                ChatMessage.run_summary(
                    run_id="run-one",
                    status="completed",
                    timing=timing,
                    iteration_count=1,
                ),
                id="first-summary",
            ),
            replace(ChatMessage.note("internal boundary"), id="boundary-note"),
            replace(ChatMessage.user("second"), id="second-user"),
            replace(
                ChatMessage.run_summary(
                    run_id="run-two",
                    status="completed",
                    timing=timing,
                    iteration_count=1,
                ),
                id="second-summary",
            ),
        ]
    )

    newest = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 1}},
    )
    older = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {
                "agent_id": "parent",
                "limit": 1,
                "before": newest["result"]["next_before"],
            },
        },
    )

    assert newest["ok"] is True
    assert [message["id"] for message in newest["result"]["messages"]] == [
        "second-user",
        "second-summary",
    ]
    assert older["ok"] is True
    assert [message["id"] for message in older["result"]["messages"]] == [
        "first-user",
        "first-summary",
    ]
    assert older["result"]["has_more"] is False


@pytest.mark.asyncio
async def test_chat_history_keeps_the_active_tail_segment_together(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    timing = {
        "started_at": "2026-07-24T10:00:00+00:00",
        "completed_at": "2026-07-24T10:00:01+00:00",
        "duration_ms": 1000,
    }
    messages = [
        replace(ChatMessage.user("completed"), id="completed-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="done"),
            id="completed-assistant",
        ),
        replace(
            ChatMessage.run_summary(
                run_id="run-one",
                status="completed",
                timing=timing,
                iteration_count=1,
            ),
            id="completed-summary",
        ),
        replace(ChatMessage.user("active"), id="active-user"),
        replace(
            ChatMessage.assistant(model="openai/gpt-5.2", content="partial"),
            id="active-assistant",
        ),
    ]
    for message in messages:
        session.append(message)

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "parent", "limit": 1}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "active-user",
        "active-assistant",
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_subagent_inspect_dispatches_exact_qualified_work_address() -> None:
    class InspectStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, str | None]] = []

        def inspect(
            self,
            agent_id: str,
            session_id: str,
            work_id: str,
            *,
            project_id: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append((agent_id, session_id, work_id, project_id))
            return {
                "id": work_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "run_id": "child-run",
                "status": "completed",
                "result": "done",
            }

    subagents = InspectStub()
    state = SimpleNamespace(runtime=SimpleNamespace(subagents=subagents))

    response = await dispatch_rpc(
        state,
        {
            "method": "subagent.inspect",
            "params": {
                "id": "sub-work-one",
                "agent_id": "worker@project-one",
                "session_id": "child-session",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["result"] == "done"
    assert subagents.calls == [("worker", "child-session", "sub-work-one", "project-one")]


@pytest.mark.asyncio
async def test_chat_history_before_returns_older_visible_page(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    for index in range(1, 7):
        session.append(_history_message(index))

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {
                "agent_id": "parent",
                "limit": 2,
                "before": "message-005",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert [message["id"] for message in result["messages"]] == [
        "message-003",
        "message-004",
    ]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_chat_history_rejects_unknown_before_message(tmp_path: Path) -> None:
    state, chat_sessions = _history_state(tmp_path)
    session = chat_sessions.create("parent", session_id="session-one")
    session.append(_history_message(1))

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "before": "message-missing"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_chat_history_rejects_limit_above_maximum(tmp_path: Path) -> None:
    state, _chat_sessions = _history_state(tmp_path)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.history",
            "params": {"agent_id": "parent", "limit": chat_methods.MAX_CHAT_HISTORY_LIMIT + 1},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
