"""Tests for chat methods queue."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.runs import ActiveRunError
from server.events import ServerEventBus
from server.rpc.chat_methods import (
    _chat_queue_remove,
    _chat_queue_update,
    _send_chat,
    _stream_chat,
)
from server.rpc.errors import RpcError
from tests.server.rpc.chat_methods_test_support import (
    _FakeRun,
    _NoCommandDispatcher,
)


# ---------------------------------------------------------------------------
# Queue invalidation: RPC send/remove/update publish a scoped queue signal so
# other windows reload the queue live instead of waiting for a terminal event.
# ---------------------------------------------------------------------------
class _QueueOnBusyLoop:
    """``start_run`` reports the session busy; ``queue_run`` returns a queued item.

    ``build_queue_update`` stands in for the streaming loop's queue-update build:
    it returns the *resolved* target session id (which can differ from the raw
    input), letting the update test assert the signal is scoped on the resolved
    id.
    """

    def __init__(
        self,
        resolved_session_id: str = "s1",
        *,
        run_started_during_enqueue: _FakeRun | None = None,
    ) -> None:
        self._resolved_session_id = resolved_session_id
        self._run_started_during_enqueue = run_started_during_enqueue
        self.build_calls: list[dict[str, Any]] = []

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> Any:
        raise ActiveRunError("session already has an active run")

    async def queue_run(self, agent_id: str, content: Any, **kwargs: Any) -> Any:
        future = asyncio.get_running_loop().create_future()
        if self._run_started_during_enqueue is not None:
            future.set_result(self._run_started_during_enqueue)
        return SimpleNamespace(future=future, to_dict=lambda: {"id": "q-1"})

    def build_queue_update(
        self, agent_id: str, session_id: str, content: Any, queued_item: Any, **kwargs: Any
    ) -> tuple[str, object, str]:
        self.build_calls.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "queued_item": queued_item,
                **kwargs,
            }
        )
        return self._resolved_session_id, object(), "display"


class _FakeQueueRuns:
    """Minimal ChatRunManager stand-in for the queue remove/update handlers.

    The queue key carries the project anchor, so the fake records the
    ``project_id`` each call was scoped with (the handlers parse it from the
    agent address).
    """

    def __init__(self, *, editable: bool = True) -> None:
        self.list_project_ids: list[str | None] = []
        self.update_project_ids: list[str | None] = []
        self.editable = editable

    def list_queued(self, agent_id: str, session_id: str, *, project_id: str | None) -> list[Any]:
        self.list_project_ids.append(project_id)
        return [
            SimpleNamespace(
                item_id="q-1",
                internal=False,
                editable=self.editable,
            )
        ]

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None
    ) -> bool:
        return True

    def update_queued(self, *args: Any, **kwargs: Any) -> bool:
        self.update_project_ids.append(kwargs.get("project_id"))
        return True


def _make_queue_state(loop: Any) -> SimpleNamespace:
    return SimpleNamespace(
        chat_loop=loop,
        streaming_chat_loop=loop,
        runtime=SimpleNamespace(),
        event_bus=ServerEventBus(),
        chat_runs=_FakeQueueRuns(),
        command_dispatcher=_NoCommandDispatcher(),
    )


def _queue_resource_events(state: SimpleNamespace) -> list[dict[str, Any]]:
    return [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ]


@pytest.mark.asyncio
async def test_send_enqueue_publishes_queue_resource_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_queue_state(_QueueOnBusyLoop())
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_queued_item_to_event_bus", lambda *a, **k: None
    )

    result = await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["queued"] is True
    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]


@pytest.mark.asyncio
async def test_stream_enqueue_publishes_queue_resource_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_queue_state(_QueueOnBusyLoop())
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_queued_item_to_event_bus", lambda *a, **k: None
    )

    result = await _stream_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["queued"] is True
    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]


@pytest.mark.asyncio
async def test_send_busy_to_idle_race_returns_started_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_run = _FakeRun("run-race")
    state = _make_queue_state(_QueueOnBusyLoop(run_started_during_enqueue=started_run))
    bridged_runs: list[Any] = []
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )

    result = await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["run_id"] == "run-race"
    assert result["message"]["content"] == "handoff text"
    assert "queued" not in result
    assert bridged_runs == [started_run]
    assert _queue_resource_events(state) == []


@pytest.mark.asyncio
async def test_stream_busy_to_idle_race_returns_started_run_with_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_run = _FakeRun("run-race")
    state = _make_queue_state(_QueueOnBusyLoop(run_started_during_enqueue=started_run))
    bridged_runs: list[Any] = []
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )

    result = await _stream_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["run_id"] == "run-race"
    assert result["sse_url"] == "/api/runs/run-race/events"
    assert "queued" not in result
    assert bridged_runs == [started_run]
    assert _queue_resource_events(state) == []


def test_queue_remove_publishes_queue_resource_changed() -> None:
    state = _make_queue_state(_QueueOnBusyLoop())

    _chat_queue_remove(state, {"agent_id": "builder", "session_id": "s1", "item_id": "q-1"})

    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]


@pytest.mark.asyncio
async def test_queue_update_scopes_on_resolved_session_id() -> None:
    # build_queue_update resolves the target session, which can differ from the
    # raw input; the queue signal must be scoped on the resolved id, not the input.
    loop = _QueueOnBusyLoop(resolved_session_id="resolved-s1")
    state = _make_queue_state(loop)

    await _chat_queue_update(
        state,
        {"agent_id": "builder", "session_id": "s1", "item_id": "q-1", "content": "edit"},
    )

    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "resolved-s1"}}
    ]


@pytest.mark.asyncio
async def test_queue_update_rejects_attachment_items() -> None:
    state = _make_queue_state(_QueueOnBusyLoop())
    state.chat_runs = _FakeQueueRuns(editable=False)

    with pytest.raises(RpcError) as exc_info:
        await _chat_queue_update(
            state,
            {"agent_id": "builder", "session_id": "s1", "item_id": "q-1", "content": "edit"},
        )
    assert exc_info.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_queue_update_rebuilds_against_address_project() -> None:
    # The queue key carries the project anchor, so the edit's params name it in the
    # agent address. The rebuild must run against that same anchor; without this, a
    # project session is looked up in the identity anchor and fails.
    loop = _QueueOnBusyLoop()
    state = _make_queue_state(loop)

    await _chat_queue_update(
        state,
        {"agent_id": "builder@vbot", "session_id": "s1", "item_id": "q-1", "content": "edit"},
    )

    assert loop.build_calls[-1]["project_id"] == "vbot"
    assert loop.build_calls[-1]["queued_item"].item_id == "q-1"
    assert state.chat_runs.update_project_ids[-1] == "vbot"
    assert state.chat_runs.list_project_ids[-1] == "vbot"


@pytest.mark.asyncio
async def test_queue_update_identity_item_rebuilds_without_project() -> None:
    # A bare identity address keeps the rebuild and the queue key project-less.
    loop = _QueueOnBusyLoop()
    state = _make_queue_state(loop)

    await _chat_queue_update(
        state,
        {"agent_id": "builder", "session_id": "s1", "item_id": "q-1", "content": "edit"},
    )

    assert loop.build_calls[-1]["project_id"] is None
    assert loop.build_calls[-1]["queued_item"].item_id == "q-1"
    assert state.chat_runs.update_project_ids[-1] is None
