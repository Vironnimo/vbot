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

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatMessage, CommandAction
from core.projects import AgentResolutionError, format_agent_address
from core.runs import ActiveRunError
from server.events import ServerEventBus
from server.rpc.chat_methods import (
    _chat_queue_remove,
    _chat_queue_update,
    _handle_move_session_command,
    _handle_rename_session_command,
    _handle_set_model_command,
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
# /model set: identity vs project routing + usable-model validation.
# ---------------------------------------------------------------------------


class _RecordingAgents:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update(self, agent_id: str, **changes: Any) -> Any:
        self.updates.append((agent_id, changes))
        return SimpleNamespace(id=agent_id, **changes)


class _RecordingProjects:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, str]] = []
        self.clear_calls: list[tuple[str, str]] = []

    def set_model_override(self, project_id: str, agent_id: str, model: str) -> Any:
        self.set_calls.append((project_id, agent_id, model))
        return SimpleNamespace(project_id=project_id)

    def clear_model_override(self, project_id: str, agent_id: str) -> Any:
        self.clear_calls.append((project_id, agent_id))
        return SimpleNamespace(project_id=project_id)


class _ModelResolver:
    """``is_model_configured`` stub: only the configured set is usable."""

    def __init__(self, configured: set[str]) -> None:
        self._configured = configured

    def is_model_configured(self, model: str) -> bool:
        return model in self._configured


def _make_model_state(
    *, configured: set[str], agents: _RecordingAgents, projects: _RecordingProjects, models: Any
) -> SimpleNamespace:
    runtime = SimpleNamespace(
        agent_resolver=_ModelResolver(configured),
        agents=agents,
        projects=projects,
        models=models,
    )
    return SimpleNamespace(runtime=runtime)


def test_set_model_identity_updates_agent_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-5"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    result = _handle_set_model_command(state, "coder", "s1", "openai/gpt-5", project_id=None)

    # Identity session writes the agent's own model; the project store is untouched.
    assert agents.updates == [("coder", {"model": "openai/gpt-5"})]
    assert projects.set_calls == []
    assert result["data"] == {"command": "model", "agent_id": "coder", "model": "openai/gpt-5"}
    assert result["output"] == "toast"


def test_set_model_identity_reset_clears_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured=set(), agents=agents, projects=projects, models=SimpleNamespace()
    )

    result = _handle_set_model_command(state, "coder", "s1", "reset", project_id=None)

    # reset writes an empty model (falls to the global default) and skips validation.
    assert agents.updates == [("coder", {"model": ""})]
    assert result["data"]["model"] == ""


def test_set_model_project_writes_override() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-mini"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    _handle_set_model_command(state, "builder", "s1", "openai/gpt-mini", project_id="vbot")

    # Project session writes a per-agent override; the identity store is untouched.
    assert projects.set_calls == [("vbot", "builder", "openai/gpt-mini")]
    assert agents.updates == []


# ---------------------------------------------------------------------------
# /rename command -> rename_session action
# ---------------------------------------------------------------------------


class _RecordingTitleSessions:
    def __init__(self) -> None:
        self.renamed: list[tuple[str, str, str, str | None]] = []

    def set_title(
        self, agent_id: str, session_id: str, title: str, project_id: str | None = None
    ) -> str | None:
        self.renamed.append((agent_id, session_id, title, project_id))
        normalized = " ".join(title.split())
        return normalized or None


def _make_rename_state(sessions: _RecordingTitleSessions) -> SimpleNamespace:
    runtime = SimpleNamespace(chat_sessions=sessions)
    return SimpleNamespace(runtime=runtime, event_bus=ServerEventBus())


def test_rename_command_sets_title_with_toast() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    result = _handle_rename_session_command(state, "coder", "s1", "Release planning")

    assert sessions.renamed == [("coder", "s1", "Release planning", None)]
    assert result["output"] == "toast"
    assert "Release planning" in result["reply"]
    assert result["data"] == {
        "command": "rename",
        "session_id": "s1",
        "title": "Release planning",
    }


def test_rename_command_without_argument_clears() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    result = _handle_rename_session_command(state, "coder", "s1", None)

    # No argument clears: the handler passes "" and reports the cleared name.
    assert sessions.renamed == [("coder", "s1", "", None)]
    assert result["data"]["title"] is None
    assert "cleared" in result["reply"].lower()


def test_rename_command_project_session_scopes_to_project() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    _handle_rename_session_command(state, "builder", "s1", "Docs", project_id="vbot")

    assert sessions.renamed == [("builder", "s1", "Docs", "vbot")]


def test_set_model_project_reset_clears_override() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured=set(), agents=agents, projects=projects, models=SimpleNamespace()
    )

    # The reset token is case-insensitive.
    _handle_set_model_command(state, "builder", "s1", "RESET", project_id="vbot")

    assert projects.clear_calls == [("vbot", "builder")]
    assert projects.set_calls == []


def test_set_model_rejects_unusable_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-5"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    with pytest.raises(RpcError) as exc_info:
        _handle_set_model_command(state, "coder", "s1", "openai/ghost", project_id=None)

    assert exc_info.value.code == "invalid_request"
    assert agents.updates == []  # nothing is written when the model is rejected


def test_set_model_rejects_forbidden_pinned_connection() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()

    class _Model:
        connections = ["api-key"]

        def allows_connection(self, connection_id: str) -> bool:
            return connection_id in self.connections

    models = SimpleNamespace(get=lambda _provider, _model: _Model())
    state = _make_model_state(
        configured={"openai/gpt-5::subscription"},
        agents=agents,
        projects=projects,
        models=models,
    )

    with pytest.raises(RpcError) as exc_info:
        _handle_set_model_command(
            state, "coder", "s1", "openai/gpt-5::subscription", project_id=None
        )

    assert exc_info.value.code == "invalid_request"
    assert agents.updates == []


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


# ---------------------------------------------------------------------------
# /agent move: relocate the current session (full history) to another agent.
# ---------------------------------------------------------------------------


class _FakeMovedSession:
    def __init__(self) -> None:
        self.appended: list[ChatMessage] = []
        self.notes: list[str] = []

    def append(self, message: ChatMessage) -> None:
        self.appended.append(message)

    def add_note(self, content: str) -> None:
        self.notes.append(content)


class _FakeWriteLock:
    async def __aenter__(self) -> _FakeWriteLock:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeMoveSessions:
    """Records the move call and serves the relocated session's two writers."""

    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self._metadata = metadata or {}
        self.move_calls: list[dict[str, Any]] = []
        self.destination = _FakeMovedSession()

    async def move(
        self,
        source_agent_id: str,
        session_id: str,
        target_agent_id: str,
        *,
        source_project_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: Any = frozenset(),
    ) -> _FakeMovedSession:
        self.move_calls.append(
            {
                "source_agent_id": source_agent_id,
                "session_id": session_id,
                "target_agent_id": target_agent_id,
                "source_project_id": source_project_id,
                "target_project_id": target_project_id,
                "strip_meta_keys": set(strip_meta_keys),
            }
        )
        return self.destination

    def get_metadata(self, agent_id: str, session_id: str, project_id: str | None = None) -> dict:
        return dict(self._metadata)

    def write_lock(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> _FakeWriteLock:
        return _FakeWriteLock()

    def get(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> _FakeMovedSession:
        return self.destination


class _FakeMoveAgents:
    def __init__(self) -> None:
        self.reset_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def reset_current_after_move(self, agent_id: str, moved_session_id: str) -> None:
        self.reset_calls.append((agent_id, moved_session_id))

    def update(self, agent_id: str, **changes: Any) -> None:
        self.update_calls.append((agent_id, changes))


class _FakeMoveRuns:
    def __init__(self, active: Any = None, queued: list[Any] | None = None) -> None:
        self._active = active
        self._queued = queued or []

    def active_run(self, *, agent_id: str, session_id: str) -> Any:
        return self._active

    def list_queued(self, agent_id: str, session_id: str) -> list[Any]:
        return list(self._queued)


class _ConfigurableResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.resolved: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        self.resolved.append((project_id, agent_id))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(id=agent_id)


def _make_move_state(
    *,
    metadata: dict[str, Any] | None = None,
    active: Any = None,
    queued: list[Any] | None = None,
    resolver_error: Exception | None = None,
) -> SimpleNamespace:
    sessions = _FakeMoveSessions(metadata)
    agents = _FakeMoveAgents()
    task_loop = _RecordingLoop()  # project-target task run path (chat_loop.start_run)
    trigger_calls: list[dict[str, Any]] = []

    async def trigger_run(agent_id: str, message: Any, **kwargs: Any) -> _FakeRun:
        trigger_calls.append({"agent_id": agent_id, "message": message, **kwargs})
        return _FakeRun()

    runtime = SimpleNamespace(
        chat_sessions=sessions,
        agents=agents,
        agent_resolver=_ConfigurableResolver(resolver_error),
        trigger_service=SimpleNamespace(trigger_run=trigger_run),
    )
    state = SimpleNamespace(
        chat_loop=task_loop,
        streaming_chat_loop=task_loop,
        runtime=runtime,
        chat_runs=_FakeMoveRuns(active, queued),
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
        command_dispatcher=_NoCommandDispatcher(),
    )
    state._sessions = sessions  # type: ignore[attr-defined]
    state._agents = agents  # type: ignore[attr-defined]
    state._trigger_calls = trigger_calls  # type: ignore[attr-defined]
    state._task_loop = task_loop  # type: ignore[attr-defined]
    return state


@pytest.mark.parametrize(
    ("source_project", "target_address", "target_agent", "target_project", "reset", "update"),
    [
        (None, "planner", "planner", None, True, True),  # identity -> identity
        (None, "planner@vbot", "planner", "vbot", True, False),  # identity -> project
        ("vbot", "assistant", "assistant", None, False, True),  # project -> identity
        ("vbot", "planner@acme", "planner", "acme", False, False),  # project -> project
    ],
)
@pytest.mark.asyncio
async def test_move_directions_relocate_and_re_home_pointers(
    source_project: str | None,
    target_address: str,
    target_agent: str,
    target_project: str | None,
    reset: bool,
    update: bool,
) -> None:
    state = _make_move_state()

    result = await _handle_move_session_command(
        state, "builder", "s1", target_address, project_id=source_project
    )

    move_call = state._sessions.move_calls[0]
    assert move_call["source_agent_id"] == "builder"
    assert move_call["source_project_id"] == source_project
    assert move_call["target_agent_id"] == target_agent
    assert move_call["target_project_id"] == target_project
    # Cross-world identity residue is stripped on the move.
    assert "visited_projects" in move_call["strip_meta_keys"]

    # The "current" pointer follows the session on each identity side only.
    assert (state._agents.reset_calls == [("builder", "s1")]) is reset
    if update:
        assert state._agents.update_calls == [(target_agent, {"current_session_id": "s1"})]
    else:
        assert state._agents.update_calls == []

    # A visible takeover divider and the silent note are persisted at the destination.
    assert len(state._sessions.destination.appended) == 1
    divider = state._sessions.destination.appended[0]
    assert divider.role == "agent_takeover"
    assert json.loads(divider.content)["to"] == target_address
    assert state._sessions.destination.notes  # silent takeover note added

    # No task → the target waits; payload lands the accessor on the same session.
    assert state._trigger_calls == []
    assert state._task_loop.start_calls == []
    assert result["output"] == "action"
    assert result["data"] == {
        "command": "agent",
        "session_id": "s1",
        "agent_id": target_address,
    }


@pytest.mark.asyncio
async def test_move_to_same_pair_is_a_no_op_hint() -> None:
    state = _make_move_state()

    result = await _handle_move_session_command(state, "builder", "s1", "builder", project_id=None)

    assert "already belongs" in result["reply"]
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_while_run_active() -> None:
    state = _make_move_state(active=_FakeRun())

    result = await _handle_move_session_command(state, "builder", "s1", "planner", project_id=None)

    assert "current run" in result["reply"]
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_while_run_queued() -> None:
    state = _make_move_state(queued=[SimpleNamespace(item_id="q-1")])

    result = await _handle_move_session_command(state, "builder", "s1", "planner", project_id=None)

    assert "queued run" in result["reply"]
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_for_unknown_target() -> None:
    state = _make_move_state(resolver_error=AgentResolutionError("no such agent"))

    result = await _handle_move_session_command(
        state, "builder", "s1", "ghost@vbot", project_id=None
    )

    assert "unknown agent" in result["reply"]
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_for_invalid_address() -> None:
    state = _make_move_state()

    result = await _handle_move_session_command(
        state, "builder", "s1", "agent:planner", project_id=None
    )

    assert "invalid agent address" in result["reply"]
    assert state._sessions.move_calls == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_channel_id": "telegram-1"},
        {"is_subagent_session": True},
        {"subagent_parent": "parent-run-1"},
    ],
)
@pytest.mark.asyncio
async def test_move_refused_for_excluded_sessions(metadata: dict[str, Any]) -> None:
    state = _make_move_state(metadata=metadata)

    result = await _handle_move_session_command(state, "builder", "s1", "planner", project_id=None)

    assert "cannot be moved" in result["reply"]
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_with_task_auto_runs_identity_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_move_state()
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    result = await _handle_move_session_command(
        state, "builder", "s1", "planner do the thing", project_id=None
    )

    # The task rides as the receiving agent's first visible turn (identity → trigger).
    assert state._trigger_calls == [
        {"agent_id": "planner", "message": "do the thing", "session_id": "s1", "internal": False}
    ]
    assert "running your task" in result["reply"]


@pytest.mark.asyncio
async def test_move_with_task_auto_runs_project_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_move_state()
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _handle_move_session_command(
        state, "builder", "s1", "planner@vbot ship it", project_id=None
    )

    # A project target has no trigger service yet → it runs through the chat loop.
    assert state._trigger_calls == []
    call = state._task_loop.start_calls[-1]
    assert call["agent_id"] == "planner"
    assert call["content"] == "ship it"
    assert call["project_id"] == "vbot"
    assert call["internal"] is False


@pytest.mark.asyncio
async def test_move_without_task_waits() -> None:
    state = _make_move_state()

    result = await _handle_move_session_command(state, "builder", "s1", "planner", project_id=None)

    assert "waiting" in result["reply"]
    assert state._trigger_calls == []
    assert state._task_loop.start_calls == []


@pytest.mark.asyncio
async def test_move_divider_and_note_carry_both_addresses() -> None:
    state = _make_move_state()

    await _handle_move_session_command(state, "builder", "s1", "planner@vbot", project_id="acme")

    divider = state._sessions.destination.appended[0]
    assert json.loads(divider.content) == {
        "from": format_agent_address("builder", "acme"),
        "to": format_agent_address("planner", "vbot"),
    }
    # The silent note names the source so the receiver knows who it took over from.
    assert "builder@acme" in state._sessions.destination.notes[0]
