"""Tests for project-aware addressing on the chat RPC handlers.

Coverage (AAA):
- ``chat.send`` with a bare agent id starts an identity run (``project_id=None``),
  byte-identical to before,
- ``chat.send`` with ``agent@projekt`` parses the address and runs project-scoped
  (``project_id`` threaded into ``start_run``),
- ``chat.send`` with a malformed address is ``invalid_request`` before any run,
- ``chat.stream`` threads the same ``project_id`` into the streaming loop,
- a ``/handoff agent:orchestrator@vbot`` targets the project: the receiving run
  and the new session are created under that project,
- a bare ``/handoff`` stays in the source scope.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatMessage, CommandAction
from core.runs import ActiveRunError
from server.events import ServerEventBus
from server.rpc.chat_methods import (
    _chat_queue_remove,
    _chat_queue_update,
    _send_chat,
    _stream_chat,
)
from server.rpc.errors import RpcError


class _FakeRun:
    def __init__(self, run_id: str = "run-1") -> None:
        self.id = run_id
        self.agent_id = "builder"
        self.session_id = "s1"
        # ``_run_response`` reads ``status.value`` and ``events``; a finished run
        # with no events is enough for these address-threading assertions.
        self.status = SimpleNamespace(value="completed")
        self.events: list[Any] = []

    async def wait(self) -> ChatMessage:
        return ChatMessage.assistant(content="handoff text", model="openai/gpt-5.2")


class _RecordingLoop:
    """Records the ``project_id`` each public entry was called with."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> _FakeRun:
        self.start_calls.append({"agent_id": agent_id, "content": content, **kwargs})
        return _FakeRun()


class _NoCommandDispatcher:
    """Treats every message as plain chat (no slash command recognized)."""

    def dispatch(
        self, agent_id: str, session_id: str, text: str, project_id: str | None = None
    ) -> None:
        return None


def _make_state(loop: _RecordingLoop) -> SimpleNamespace:
    # The bridge helper reads the event bus; a no-op namespace is enough since the
    # tests assert on the recorded loop call, not on bridged events.
    event_bus = SimpleNamespace(publish=lambda *a, **k: None)
    runtime = SimpleNamespace()
    return SimpleNamespace(
        chat_loop=loop,
        streaming_chat_loop=loop,
        runtime=runtime,
        event_bus=event_bus,
        chat_runs=SimpleNamespace(),
        command_dispatcher=_NoCommandDispatcher(),
    )


@pytest.mark.asyncio
async def test_send_bare_agent_runs_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "builder"
    assert loop.start_calls[0]["project_id"] is None


@pytest.mark.asyncio
async def test_send_qualified_agent_runs_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder@vbot", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "builder"
    assert loop.start_calls[0]["project_id"] == "vbot"


@pytest.mark.asyncio
async def test_send_invalid_address_is_invalid_request() -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)

    with pytest.raises(RpcError) as exc_info:
        await _send_chat(
            state, {"agent_id": "builder@bad project", "session_id": "s1", "content": "hi"}
        )

    assert exc_info.value.code == "invalid_request"
    assert loop.start_calls == []


@pytest.mark.asyncio
async def test_stream_qualified_agent_runs_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _stream_chat(state, {"agent_id": "tester@vbot", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "tester"
    assert loop.start_calls[0]["project_id"] == "vbot"


# ---------------------------------------------------------------------------
# Handoff: target address resolution and project-scoped receiving run.
# ---------------------------------------------------------------------------


class _HandoffLoop:
    """Captures the handoff-writing and receiving runs with their project ids."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> _FakeRun:
        self.calls.append({"agent_id": agent_id, **kwargs})
        return _FakeRun()


class _FakeResolver:
    def __init__(self) -> None:
        self.resolved: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        self.resolved.append((project_id, agent_id))
        return SimpleNamespace(id=agent_id)


def _make_handoff_state(loop: _HandoffLoop, resolver: _FakeResolver) -> SimpleNamespace:
    created_sessions: list[str] = []

    def create_session(agent_id: str, *, session_id: Any = None, project_id: Any = None) -> Any:
        created_sessions.append(f"{agent_id}@{project_id}")
        return SimpleNamespace(id="new-session")

    chat_sessions = SimpleNamespace(create=create_session)

    async def trigger_run(agent_id: str, message: Any, **kwargs: Any) -> _FakeRun:
        # Identity (no project) handoff-writing run path; records nothing the tests
        # assert on, it only has to return a run whose ``wait`` yields handoff text.
        loop.calls.append({"agent_id": agent_id, "project_id": None, "via": "trigger"})
        return _FakeRun()

    runtime = SimpleNamespace(
        agent_resolver=resolver,
        chat_sessions=chat_sessions,
        agents=SimpleNamespace(update=lambda *a, **k: None),
        trigger_service=SimpleNamespace(trigger_run=trigger_run),
    )
    state = SimpleNamespace(
        chat_loop=loop,
        streaming_chat_loop=loop,
        runtime=runtime,
        chat_runs=SimpleNamespace(active_run=lambda **k: None),
        command_dispatcher=_HandoffDispatcher(),
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
    )
    state._created_sessions = created_sessions  # type: ignore[attr-defined]
    return state


class _HandoffDispatcher:
    def dispatch(
        self, agent_id: str, session_id: str, text: str, project_id: str | None = None
    ) -> CommandAction:
        # Pass the slash text after ``/handoff`` through as the action argument,
        # mirroring the real dispatcher's ``optional`` argument handling.
        argument = text[len("/handoff") :].strip() or None
        return CommandAction(name="handoff", argument=argument)


@pytest.mark.asyncio
async def test_handoff_targets_project_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _HandoffLoop()
    resolver = _FakeResolver()
    state = _make_handoff_state(loop, resolver)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(
        state,
        {
            "agent_id": "builder",
            "session_id": "s1",
            "content": "/handoff agent:orchestrator@vbot",
        },
    )

    # The receiving run targets orchestrator under project vbot, and the new
    # session was created under that project anchor.
    assert ("vbot", "orchestrator") in resolver.resolved
    assert loop.calls[-1]["agent_id"] == "orchestrator"
    assert loop.calls[-1]["project_id"] == "vbot"
    assert state._created_sessions[-1] == "orchestrator@vbot"


@pytest.mark.asyncio
async def test_handoff_bare_target_stays_in_source_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _HandoffLoop()
    resolver = _FakeResolver()
    state = _make_handoff_state(loop, resolver)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(
        state,
        {"agent_id": "builder@vbot", "session_id": "s1", "content": "/handoff"},
    )

    # No explicit target → receiving run stays in the source (builder, vbot) scope.
    assert loop.calls[-1]["agent_id"] == "builder"
    assert loop.calls[-1]["project_id"] == "vbot"
    assert state._created_sessions[-1] == "builder@vbot"


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

    def __init__(self, resolved_session_id: str = "s1") -> None:
        self._resolved_session_id = resolved_session_id

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> Any:
        raise ActiveRunError("session already has an active run")

    async def queue_run(self, agent_id: str, content: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(to_dict=lambda: {"id": "q-1"})

    def build_queue_update(
        self, agent_id: str, session_id: str, content: Any, **kwargs: Any
    ) -> tuple[str, object, str]:
        return self._resolved_session_id, object(), "display"


class _FakeQueueRuns:
    """Minimal ChatRunManager stand-in for the queue remove/update handlers."""

    def list_queued(self, agent_id: str, session_id: str) -> list[Any]:
        return [SimpleNamespace(item_id="q-1", internal=False)]

    def remove_queued(self, agent_id: str, session_id: str, item_id: str) -> bool:
        return True

    def update_queued(self, *args: Any, **kwargs: Any) -> bool:
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


def test_queue_remove_publishes_queue_resource_changed() -> None:
    state = _make_queue_state(_QueueOnBusyLoop())

    _chat_queue_remove(state, {"agent_id": "builder", "session_id": "s1", "item_id": "q-1"})

    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]


def test_queue_update_scopes_on_resolved_session_id() -> None:
    # build_queue_update resolves the target session, which can differ from the
    # raw input; the queue signal must be scoped on the resolved id, not the input.
    loop = _QueueOnBusyLoop(resolved_session_id="resolved-s1")
    state = _make_queue_state(loop)

    _chat_queue_update(
        state,
        {"agent_id": "builder", "session_id": "s1", "item_id": "q-1", "content": "edit"},
    )

    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "resolved-s1"}}
    ]
