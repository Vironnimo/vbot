"""Tests for server delegate run event routing constants and history filtering."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import server.delegates as delegates
from core.chat import (
    ChatMessage,
    ChatSessionManager,
    CommandDispatcher,
    CommandHandled,
)
from core.chat.content_blocks import TextBlock
from core.runs import (
    RUN_STARTED_EVENT,
    TOOL_CALL_STDERR_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    ActiveRunError,
    ChatRunManager,
    QueuedRunItem,
    Run,
)
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


def _history_state(tmp_path: Path) -> tuple[SimpleNamespace, ChatSessionManager]:
    chat_sessions = ChatSessionManager(tmp_path)
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            agents=HistoryAgentStore(),
            chat_sessions=chat_sessions,
        )
    )
    return state, chat_sessions


def _history_message(index: int) -> ChatMessage:
    message = ChatMessage.user(f"Message {index}")
    return replace(message, id=f"message-{index:03d}")


class RetryLoopStub:
    def __init__(self, run: Run) -> None:
        self._run = run
        self.calls: list[tuple[str, str]] = []

    async def retry_run(self, agent_id: str, session_id: str) -> Run:
        self.calls.append((agent_id, session_id))
        return self._run


class CommandHandledDispatcher(CommandDispatcher):
    def __init__(self, reply: str) -> None:
        super().__init__(ChatRunManager())
        self._reply = reply
        self.calls: list[tuple[str, str, str]] = []

    def dispatch(self, agent_id: str, session_id: str, message_text: str) -> CommandHandled:
        self.calls.append((agent_id, session_id, message_text))
        return CommandHandled(reply=self._reply)


class QueueManagerStub:
    def __init__(
        self,
        *,
        items: list[QueuedRunItem] | None = None,
        remove_result: bool = True,
        update_result: bool = True,
    ) -> None:
        self._items = list(items or [])
        self._remove_result = remove_result
        self._update_result = update_result
        self.list_calls: list[tuple[str, str]] = []
        self.remove_calls: list[tuple[str, str, str]] = []
        self.update_calls: list[tuple[str, str, str, Any, str]] = []

    def list_queued(self, agent_id: str, session_id: str) -> list[QueuedRunItem]:
        self.list_calls.append((agent_id, session_id))
        return list(self._items)

    def remove_queued(self, agent_id: str, session_id: str, item_id: str) -> bool:
        self.remove_calls.append((agent_id, session_id, item_id))
        return self._remove_result

    def update_queued(
        self,
        agent_id: str,
        session_id: str,
        item_id: str,
        new_executor: Any,
        new_display_content: str,
    ) -> bool:
        self.update_calls.append((agent_id, session_id, item_id, new_executor, new_display_content))
        return self._update_result


def _make_queued_item(*, item_id: str, content: str, internal: bool = False) -> QueuedRunItem:
    async def _executor(_run: Run) -> None:
        return None

    return QueuedRunItem(
        item_id=item_id,
        display_content=content,
        executor=_executor,
        internal=internal,
        future=asyncio.get_running_loop().create_future(),
        created_at="2026-05-22T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_chat_retry_last_turn_returns_streaming_run_response() -> None:
    run = Run(run_id="run-retry", agent_id="parent", session_id="session-one")
    retry_loop = RetryLoopStub(run)
    state = SimpleNamespace(streaming_chat_loop=retry_loop)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.retry_last_turn",
            "params": {"agent_id": "parent", "session_id": "session-one"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["run_id"] == "run-retry"
    assert response["result"]["sse_url"] == "/api/runs/run-retry/events"
    assert retry_loop.calls == [("parent", "session-one")]


@pytest.mark.asyncio
async def test_chat_retry_last_turn_requires_agent_id() -> None:
    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.retry_last_turn",
            "params": {"session_id": "session-one"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


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
            "params": {"agent_id": "parent", "limit": delegates.MAX_CHAT_HISTORY_LIMIT + 1},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_chat_commands_returns_combined_command_and_skill_items() -> None:
    skills = [
        SimpleNamespace(name="debugging", description="Debug failures."),
        SimpleNamespace(name="alpha", description="Alpha helper."),
    ]
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            skills=SimpleNamespace(
                list_all=lambda: skills,
            )
        )
    )

    response = await dispatch_rpc(state, {"method": "chat.commands", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "items": [
                {
                    "name": "compact",
                    "description": "Compact the current session's context immediately.",
                    "type": "command",
                },
                {
                    "name": "new",
                    "description": "Start a new session for the current agent.",
                    "type": "command",
                },
                {
                    "name": "status",
                    "description": "Show current session and runtime status.",
                    "type": "command",
                },
                {
                    "name": "stop",
                    "description": "Cancel the active run for this session.",
                    "type": "command",
                },
                {
                    "name": "alpha",
                    "description": "Alpha helper.",
                    "type": "skill",
                },
                {
                    "name": "debugging",
                    "description": "Debug failures.",
                    "type": "skill",
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_chat_stream_slash_command_returns_handled_result_without_starting_run() -> None:
    streaming_chat_loop = SimpleNamespace(start_run=AsyncMock())
    command_dispatcher = CommandHandledDispatcher(reply="Run cancelled.")
    state = SimpleNamespace(
        command_dispatcher=command_dispatcher,
        streaming_chat_loop=streaming_chat_loop,
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "/stop",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "command_handled": True,
            "reply": "Run cancelled.",
        },
    }
    assert command_dispatcher.calls == [("agent-1", "session-1", "/stop")]
    streaming_chat_loop.start_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_stream_returns_queued_response_when_session_is_busy() -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    streaming_chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    state = SimpleNamespace(streaming_chat_loop=streaming_chat_loop)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Queued message",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "queued": True,
            "item": queued_item.to_dict(),
        },
    }
    streaming_chat_loop.start_run.assert_awaited_once_with(
        "agent-1",
        "Queued message",
        session_id="session-1",
    )
    streaming_chat_loop.queue_run.assert_awaited_once_with(
        "agent-1",
        "Queued message",
        session_id="session-1",
    )


@pytest.mark.asyncio
async def test_chat_send_busy_queue_bridges_started_run_to_event_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    bridged_runs: list[Run] = []
    monkeypatch.setattr(
        delegates,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )
    state = SimpleNamespace(chat_loop=chat_loop)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Queued message",
            },
        },
    )

    dequeued_run = Run(
        run_id="run-queued-send",
        agent_id="agent-1",
        session_id="session-1",
    )
    queued_item.future.set_result(dequeued_run)
    await asyncio.sleep(0)

    assert response["ok"] is True
    assert response["result"]["queued"] is True
    assert bridged_runs == [dequeued_run]


@pytest.mark.asyncio
async def test_chat_stream_busy_queue_bridges_started_run_to_event_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    streaming_chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    bridged_runs: list[Run] = []
    monkeypatch.setattr(
        delegates,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )
    state = SimpleNamespace(streaming_chat_loop=streaming_chat_loop)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Queued message",
            },
        },
    )

    dequeued_run = Run(
        run_id="run-queued-stream",
        agent_id="agent-1",
        session_id="session-1",
    )
    queued_item.future.set_result(dequeued_run)
    await asyncio.sleep(0)

    assert response["ok"] is True
    assert response["result"]["queued"] is True
    assert bridged_runs == [dequeued_run]


@pytest.mark.asyncio
async def test_run_event_bridge_observes_publish_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingEventBus:
        def publish(self, _event_type: str, _payload: dict[str, Any]) -> None:
            raise RuntimeError("publish failed")

    run = Run(run_id="run-one", agent_id="agent-1", session_id="session-1")
    run.emit(RUN_STARTED_EVENT, {"status": "running"})
    warnings: list[tuple[str, bool]] = []

    def record_warning(message: str, *args: Any, **kwargs: Any) -> None:
        warnings.append((message, kwargs.get("exc_info") is True))

    monkeypatch.setattr(delegates._LOGGER, "warning", record_warning)
    delegates._bridge_run_to_event_bus(SimpleNamespace(event_bus=FailingEventBus()), run)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert warnings == [("Run event bridge failed", True)]


@pytest.mark.asyncio
async def test_chat_queue_list_returns_queued_items(monkeypatch: pytest.MonkeyPatch) -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    queue_manager = QueueManagerStub(items=[queued_item])
    monkeypatch.setattr(delegates, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_list",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "items": [queued_item.to_dict()],
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1")]


@pytest.mark.asyncio
async def test_chat_queue_list_hides_internal_items(monkeypatch: pytest.MonkeyPatch) -> None:
    public_item = _make_queued_item(item_id="queue-public", content="Visible")
    internal_item = _make_queued_item(item_id="queue-internal", content="Hidden", internal=True)
    queue_manager = QueueManagerStub(items=[public_item, internal_item])
    monkeypatch.setattr(delegates, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_list",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "items": [public_item.to_dict()],
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1")]


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        remove_result=True,
    )
    monkeypatch.setattr(delegates, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_remove",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-1",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "ok": True,
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1")]
    assert queue_manager.remove_calls == [("agent-1", "session-1", "queue-1")]


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_error_for_unknown_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        remove_result=False,
    )
    monkeypatch.setattr(delegates, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_remove",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-404",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "queue_item_not_found"
    assert queue_manager.list_calls == [("agent-1", "session-1")]
    assert queue_manager.remove_calls == []


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_not_found_for_internal_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-internal", content="Hidden", internal=True)],
        remove_result=True,
    )
    monkeypatch.setattr(delegates, "_state_chat_runs", lambda _state: queue_manager)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_remove",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-internal",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "queue_item_not_found"
    assert queue_manager.list_calls == [("agent-1", "session-1")]
    assert queue_manager.remove_calls == []


@pytest.mark.asyncio
async def test_chat_queue_update_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        update_result=True,
    )
    monkeypatch.setattr(delegates, "_state_chat_runs", lambda _state: queue_manager)

    captured: dict[str, Any] = {}
    fake_executor = object()

    def fake_build_streaming_queue_update(
        _state: Any,
        agent_id: str,
        session_id: str,
        content: str | list[TextBlock],
    ) -> tuple[str, Any, str]:
        captured["agent_id"] = agent_id
        captured["session_id"] = session_id
        captured["content"] = content
        return session_id, fake_executor, "Updated queued message"

    monkeypatch.setattr(
        delegates,
        "_build_streaming_queue_update",
        fake_build_streaming_queue_update,
    )

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_update",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-1",
                "content": [{"type": "text", "text": "Edited queued text"}],
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "ok": True,
        },
    }
    assert queue_manager.list_calls == [("agent-1", "session-1")]
    assert captured == {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "content": [TextBlock(type="text", text="Edited queued text")],
    }
    assert queue_manager.update_calls == [
        ("agent-1", "session-1", "queue-1", fake_executor, "Updated queued message")
    ]


@pytest.mark.asyncio
async def test_chat_queue_update_returns_not_found_for_internal_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-internal", content="Hidden", internal=True)],
        update_result=True,
    )
    monkeypatch.setattr(delegates, "_state_chat_runs", lambda _state: queue_manager)

    build_called = False

    def fail_if_called(*_args: Any, **_kwargs: Any) -> tuple[str, Any, str]:
        nonlocal build_called
        build_called = True
        return "session-1", object(), "should-not-build"

    monkeypatch.setattr(delegates, "_build_streaming_queue_update", fail_if_called)

    response = await dispatch_rpc(
        SimpleNamespace(),
        {
            "method": "chat.queue_update",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "item_id": "queue-internal",
                "content": "Edited queued text",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "queue_item_not_found"
    assert queue_manager.list_calls == [("agent-1", "session-1")]
    assert build_called is False
    assert queue_manager.update_calls == []
