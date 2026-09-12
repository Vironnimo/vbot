"""Tests for chat methods queue dispatch."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.chat import (
    CommandDispatcher,
    ReplySurface,
)
from core.chat.content_blocks import TextBlock
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    QueuedRunItem,
    Run,
)
from server.rpc import chat_methods, event_bridge
from server.rpc.methods import dispatch_rpc


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
        self.list_calls: list[tuple[str, str, str | None]] = []
        self.remove_calls: list[tuple[str, str, str, str | None]] = []
        self.update_calls: list[tuple[str, str, str, Any, str, str | None, bool | None]] = []

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None
    ) -> list[QueuedRunItem]:
        self.list_calls.append((agent_id, session_id, project_id))
        return list(self._items)

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None
    ) -> bool:
        self.remove_calls.append((agent_id, session_id, item_id, project_id))
        return self._remove_result

    def update_queued(
        self,
        agent_id: str,
        session_id: str,
        item_id: str,
        new_executor: Any,
        new_display_content: str,
        *,
        project_id: str | None,
        editable: bool | None = None,
    ) -> bool:
        self.update_calls.append(
            (
                agent_id,
                session_id,
                item_id,
                new_executor,
                new_display_content,
                project_id,
                editable,
            )
        )
        return self._update_result


def _make_queued_item(
    *, item_id: str, content: str, internal: bool = False, editable: bool = True
) -> QueuedRunItem:
    async def _executor(_run: Run) -> None:
        return None

    return QueuedRunItem(
        item_id=item_id,
        display_content=content,
        executor=_executor,
        internal=internal,
        future=asyncio.get_running_loop().create_future(),
        editable=editable,
        created_at="2026-05-22T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_chat_stream_returns_queued_response_when_session_is_busy() -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    streaming_chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    state = SimpleNamespace(
        streaming_chat_loop=streaming_chat_loop,
        command_dispatcher=CommandDispatcher(ChatRunManager()),
    )

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
        reply_surface=ReplySurface.webui(),
        project_id=None,
    )
    streaming_chat_loop.queue_run.assert_awaited_once_with(
        "agent-1",
        "Queued message",
        session_id="session-1",
        reply_surface=ReplySurface.webui(),
        project_id=None,
    )


@pytest.mark.asyncio
async def test_chat_edit_starts_an_idle_streaming_run_without_queue_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Run(
        run_id="run-edit",
        agent_id="agent-1",
        session_id="session-1",
    )
    streaming_chat_loop = SimpleNamespace(edit_run=AsyncMock(return_value=run))
    bridged_runs: list[Run] = []
    monkeypatch.setattr(
        chat_methods,
        "_bridge_run_to_event_bus",
        lambda _state, started_run: bridged_runs.append(started_run),
    )
    state = SimpleNamespace(streaming_chat_loop=streaming_chat_loop)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.edit",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "message_id": "message-1",
                "content": "edited request",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["run_id"] == "run-edit"
    assert response["result"]["sse_url"] == "/api/runs/run-edit/events"
    streaming_chat_loop.edit_run.assert_awaited_once_with(
        "agent-1",
        "edited request",
        session_id="session-1",
        message_id="message-1",
        reply_surface=ReplySurface.webui(),
        project_id=None,
    )
    assert bridged_runs == [run]


@pytest.mark.asyncio
async def test_chat_edit_rejects_busy_session_without_queue_fallback() -> None:
    streaming_chat_loop = SimpleNamespace(
        edit_run=AsyncMock(side_effect=ActiveRunError("session already has an active run"))
    )
    state = SimpleNamespace(streaming_chat_loop=streaming_chat_loop)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.edit",
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "message_id": "message-1",
                "content": "edited request",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "active_run"
    assert not hasattr(streaming_chat_loop, "queue_run")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "loop_attribute"),
    (("chat.send", "chat_loop"), ("chat.stream", "streaming_chat_loop")),
)
async def test_chat_enqueue_cancellation_returns_error_envelope(
    method: str,
    loop_attribute: str,
) -> None:
    queued_item = _make_queued_item(item_id="queue-cancelled", content="Cancelled message")
    queued_item.future.cancel()
    chat_loop = SimpleNamespace(
        start_run=AsyncMock(side_effect=ActiveRunError("session already has an active run")),
        queue_run=AsyncMock(return_value=queued_item),
    )
    state = SimpleNamespace(
        command_dispatcher=CommandDispatcher(ChatRunManager()),
        **{loop_attribute: chat_loop},
    )

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "agent-1",
                "session_id": "session-1",
                "content": "Cancelled message",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "run_cancelled"
    assert "queue-cancelled" in response["error"]["message"]


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
        event_bridge,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )
    state = SimpleNamespace(
        chat_loop=chat_loop,
        command_dispatcher=CommandDispatcher(ChatRunManager()),
    )

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
        event_bridge,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )
    state = SimpleNamespace(
        streaming_chat_loop=streaming_chat_loop,
        command_dispatcher=CommandDispatcher(ChatRunManager()),
    )

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
async def test_chat_queue_list_returns_queued_items(monkeypatch: pytest.MonkeyPatch) -> None:
    queued_item = _make_queued_item(item_id="queue-1", content="Queued message")
    queue_manager = QueueManagerStub(items=[queued_item])
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

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
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]


@pytest.mark.asyncio
async def test_chat_queue_list_hides_internal_items(monkeypatch: pytest.MonkeyPatch) -> None:
    public_item = _make_queued_item(item_id="queue-public", content="Visible")
    internal_item = _make_queued_item(item_id="queue-internal", content="Hidden", internal=True)
    queue_manager = QueueManagerStub(items=[public_item, internal_item])
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

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
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        remove_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

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
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert queue_manager.remove_calls == [("agent-1", "session-1", "queue-1", None)]


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_error_for_unknown_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        remove_result=False,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

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
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert queue_manager.remove_calls == []


@pytest.mark.asyncio
async def test_chat_queue_remove_returns_not_found_for_internal_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-internal", content="Hidden", internal=True)],
        remove_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

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
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert queue_manager.remove_calls == []


@pytest.mark.asyncio
async def test_chat_queue_update_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-1", content="Queued message")],
        update_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    captured: dict[str, Any] = {}
    fake_executor = object()

    def fake_build_streaming_queue_update(
        _state: Any,
        agent_id: str,
        session_id: str,
        content: str | list[TextBlock],
        queued_item: QueuedRunItem,
        *,
        input_origin: str | None = None,
        project_id: str | None = None,
    ) -> tuple[str, Any, str]:
        captured["agent_id"] = agent_id
        captured["session_id"] = session_id
        captured["content"] = content
        captured["queued_item_id"] = queued_item.item_id
        captured["input_origin"] = input_origin
        captured["project_id"] = project_id
        return session_id, fake_executor, "Updated queued message"

    monkeypatch.setattr(
        chat_methods,
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
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert captured == {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "content": [TextBlock(type="text", text="Edited queued text")],
        "queued_item_id": "queue-1",
        "input_origin": None,
        "project_id": None,
    }
    assert queue_manager.update_calls == [
        (
            "agent-1",
            "session-1",
            "queue-1",
            fake_executor,
            "Updated queued message",
            None,
            False,
        )
    ]


@pytest.mark.asyncio
async def test_chat_queue_update_returns_not_found_for_internal_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_manager = QueueManagerStub(
        items=[_make_queued_item(item_id="queue-internal", content="Hidden", internal=True)],
        update_result=True,
    )
    monkeypatch.setattr(chat_methods, "_state_chat_runs", lambda _state: queue_manager)

    build_called = False

    def fail_if_called(*_args: Any, **_kwargs: Any) -> tuple[str, Any, str]:
        nonlocal build_called
        build_called = True
        return "session-1", object(), "should-not-build"

    monkeypatch.setattr(chat_methods, "_build_streaming_queue_update", fail_if_called)

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
    assert queue_manager.list_calls == [("agent-1", "session-1", None)]
    assert build_called is False
    assert queue_manager.update_calls == []
