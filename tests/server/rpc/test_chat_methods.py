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
from server.rpc.chat_methods import _send_chat, _stream_chat
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
