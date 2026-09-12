"""Tests for chat methods move."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
    ReplySurface,
)
from core.projects import AgentResolutionError, format_agent_address
from core.runs import ChatRunManager, RunAdmissionBlockedError
from core.sessions import (
    SESSION_MOVE_STRIP_META_KEYS,
    SessionAddress,
)
from core.tools.terminal_manager import TerminalOwner
from server.events import ServerEventBus
from tests.server.rpc.chat_methods_test_support import (
    _execute_core_command,
    _FakeRun,
    _NoCommandDispatcher,
    _RecordingLoop,
)


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
        self.move_started: asyncio.Event | None = None
        self.move_release: asyncio.Event | None = None

    async def move(
        self,
        source: SessionAddress,
        target: SessionAddress,
        *,
        strip_meta_keys: Any = frozenset(),
    ) -> _FakeMovedSession:
        self.move_calls.append(
            {
                "source_agent_id": source.agent_id,
                "session_id": source.session_id,
                "target_agent_id": target.agent_id,
                "source_project_id": source.project_id,
                "target_project_id": target.project_id,
                "strip_meta_keys": set(strip_meta_keys),
            }
        )
        if self.move_started is not None:
            self.move_started.set()
        if self.move_release is not None:
            await self.move_release.wait()
        return self.destination

    def get_metadata(self, address: SessionAddress) -> dict:
        return dict(self._metadata)

    def write_lock(self, address: SessionAddress) -> _FakeWriteLock:
        return _FakeWriteLock()

    def get(self, address: SessionAddress) -> _FakeMovedSession:
        return self.destination


class _FakeMoveAgents:
    def __init__(self) -> None:
        self.reset_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def reset_current_after_session_removed(self, agent_id: str, removed_session_id: str) -> None:
        self.reset_calls.append((agent_id, removed_session_id))

    def update(self, agent_id: str, **changes: Any) -> None:
        self.update_calls.append((agent_id, changes))


class _FakeMoveRuns:
    def __init__(
        self,
        active: Any = None,
        queued: list[Any] | None = None,
        *,
        guard_blocked: bool = False,
    ) -> None:
        self._active = active
        self._queued = queued or []
        self._guard_blocked = guard_blocked
        self.guarded_sessions: list[tuple[SessionAddress, ...]] = []

    def active_run(self, *, agent_id: str, session_id: str, project_id: str | None) -> Any:
        return self._active

    def list_queued(self, agent_id: str, session_id: str, *, project_id: str | None) -> list[Any]:
        return list(self._queued)

    def session_admission_guard(self, *session_keys: SessionAddress) -> Any:
        self.guarded_sessions.append(session_keys)
        if self._guard_blocked:
            return _FakeBlockedAdmissionGuard()
        return _FakeWriteLock()


class _FakeBlockedAdmissionGuard:
    async def __aenter__(self) -> None:
        raise RunAdmissionBlockedError("guarded")

    async def __aexit__(self, *args: Any) -> None:
        return None


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
    guard_blocked: bool = False,
) -> SimpleNamespace:
    sessions = _FakeMoveSessions(metadata)
    agents = _FakeMoveAgents()
    task_loop = _RecordingLoop()  # project-target task run path (chat_loop.start_run)
    trigger_calls: list[dict[str, Any]] = []
    terminal_transfers: list[tuple[TerminalOwner, TerminalOwner]] = []

    async def trigger_run(agent_id: str, message: Any, **kwargs: Any) -> _FakeRun:
        trigger_calls.append({"agent_id": agent_id, "message": message, **kwargs})
        return _FakeRun()

    runtime = SimpleNamespace(
        chat_sessions=sessions,
        agents=agents,
        agent_resolver=_ConfigurableResolver(resolver_error),
        trigger_service=SimpleNamespace(trigger_run=trigger_run),
        terminal_manager=SimpleNamespace(
            transfer_scope=lambda source, target: terminal_transfers.append((source, target))
        ),
    )
    state = SimpleNamespace(
        chat_loop=task_loop,
        streaming_chat_loop=task_loop,
        runtime=runtime,
        chat_runs=_FakeMoveRuns(active, queued, guard_blocked=guard_blocked),
        event_bus=ServerEventBus(),
        command_dispatcher=_NoCommandDispatcher(),
    )
    state._sessions = sessions  # type: ignore[attr-defined]
    state._agents = agents  # type: ignore[attr-defined]
    state._trigger_calls = trigger_calls  # type: ignore[attr-defined]
    state._task_loop = task_loop  # type: ignore[attr-defined]
    state._terminal_transfers = terminal_transfers  # type: ignore[attr-defined]
    state._command_changes = []  # type: ignore[attr-defined]
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

    result = await _execute_core_command(
        state, f"/agent {target_address}", project_id=source_project
    )

    move_call = state._sessions.move_calls[0]
    assert move_call["source_agent_id"] == "builder"
    assert move_call["source_project_id"] == source_project
    assert move_call["target_agent_id"] == target_agent
    assert move_call["target_project_id"] == target_project
    # A move is always cross-agent, so the source Agent's pinned Skill catalog and
    # seen-Skills set are stripped; the target re-pins its own catalog.
    assert move_call["strip_meta_keys"] == set(SESSION_MOVE_STRIP_META_KEYS)
    assert {"pinned_skill_catalog", "seen_skills"} <= move_call["strip_meta_keys"]

    # The "current" pointer follows the session on each identity side only.
    assert (state._agents.reset_calls == [("builder", "s1")]) is reset
    if update:
        assert state._agents.update_calls == [(target_agent, {"current_session_id": "s1"})]
    else:
        assert state._agents.update_calls == []

    # The relocation is announced like session.create/delete: a sessions signal
    # for each side's list, plus one agents signal when an identity current
    # pointer was re-aimed on either side.
    resource_events = [
        {"kind": change.kind, **({"scope": dict(change.scope)} if change.scope else {})}
        for change in state._command_changes
    ]
    assert {"kind": "sessions", "scope": {"agent_id": "builder"}} in resource_events
    assert {"kind": "sessions", "scope": {"agent_id": target_agent}} in resource_events
    agents_events = [event for event in resource_events if event["kind"] == "agents"]
    assert (agents_events == [{"kind": "agents"}]) is (reset or update)

    # A visible takeover divider and the silent note are persisted at the destination.
    assert len(state._sessions.destination.appended) == 1
    divider = state._sessions.destination.appended[0]
    assert divider.role == "agent_takeover"
    assert json.loads(divider.content)["to"] == target_address
    assert state._sessions.destination.notes  # silent takeover note added
    assert state._terminal_transfers == [
        (
            TerminalOwner(source_project, "builder", "s1"),
            TerminalOwner(target_project, target_agent, "s1"),
        )
    ]

    # No task → the target waits; payload lands the accessor on the same session.
    assert state._trigger_calls == []
    assert state._task_loop.start_calls == []
    assert result.navigation is not None
    assert result.navigation.kind == "offer_session"
    assert result.facts == {"session_id": "s1", "agent_id": target_address}


@pytest.mark.asyncio
async def test_move_to_same_pair_is_a_no_op_hint() -> None:
    state = _make_move_state()

    result = await _execute_core_command(state, "/agent builder", project_id=None)

    assert result.feedback is not None
    assert state._sessions.move_calls == []
    # A refused move announces nothing — the signals fire only after relocation.
    assert state._command_changes == []


@pytest.mark.asyncio
async def test_move_refused_while_run_active() -> None:
    state = _make_move_state(active=_FakeRun())

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_while_run_queued() -> None:
    state = _make_move_state(queued=[SimpleNamespace(item_id="q-1")])

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_when_admission_guard_wins_after_idle_check() -> None:
    state = _make_move_state(guard_blocked=True)

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_guard_rejects_source_and_destination_runs_during_storage_wait() -> None:
    state = _make_move_state()
    state.chat_runs = ChatRunManager()
    state._sessions.move_started = asyncio.Event()
    state._sessions.move_release = asyncio.Event()

    move_task = asyncio.create_task(
        _execute_core_command(state, "/agent planner@vbot", project_id=None)
    )
    await asyncio.wait_for(state._sessions.move_started.wait(), timeout=1)

    with pytest.raises(RunAdmissionBlockedError):
        await state.chat_runs.start(
            SessionAddress(project_id=None, agent_id="builder", session_id="s1"),
            lambda _run: asyncio.sleep(0),
        )
    with pytest.raises(RunAdmissionBlockedError):
        await state.chat_runs.start(
            SessionAddress(project_id="vbot", agent_id="planner", session_id="s1"),
            lambda _run: asyncio.sleep(0),
        )

    state._sessions.move_release.set()
    result = await asyncio.wait_for(move_task, timeout=1)
    assert result.facts == {"session_id": "s1", "agent_id": "planner@vbot"}


@pytest.mark.asyncio
async def test_move_refused_for_unknown_target() -> None:
    state = _make_move_state(resolver_error=AgentResolutionError("no such agent"))

    result = await _execute_core_command(state, "/agent ghost@vbot", project_id=None)

    assert result.feedback is not None
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_for_invalid_address() -> None:
    state = _make_move_state()

    result = await _execute_core_command(state, "/agent agent:planner", project_id=None)

    assert result.feedback is not None
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

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_with_task_auto_runs_identity_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_move_state()
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    result = await _execute_core_command(state, "/agent planner do the thing", project_id=None)

    # The task rides as the receiving agent's first visible turn (identity → trigger).
    assert state._trigger_calls == [
        {
            "agent_id": "planner",
            "message": "do the thing",
            "session_id": "s1",
            "project_id": None,
            "internal": False,
            "reply_surface": ReplySurface.webui(),
        }
    ]
    assert result.feedback is not None


@pytest.mark.asyncio
async def test_move_with_task_auto_runs_project_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_move_state()
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _execute_core_command(state, "/agent planner@vbot ship it", project_id=None)

    # Core uses the same trigger seam for identity and project targets.
    call = state._trigger_calls[-1]
    assert call["agent_id"] == "planner"
    assert call["message"] == "ship it"
    assert call["project_id"] == "vbot"
    assert call["internal"] is False
    assert call["reply_surface"] == ReplySurface.webui()
    assert state._task_loop.start_calls == []


@pytest.mark.asyncio
async def test_move_without_task_waits() -> None:
    state = _make_move_state()

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert state._trigger_calls == []
    assert state._task_loop.start_calls == []


@pytest.mark.asyncio
async def test_move_divider_and_note_carry_both_addresses() -> None:
    state = _make_move_state()

    await _execute_core_command(state, "/agent planner@vbot", project_id="acme")

    divider = state._sessions.destination.appended[0]
    assert json.loads(divider.content) == {
        "from": format_agent_address("builder", "acme"),
        "to": format_agent_address("planner", "vbot"),
    }
    # The silent note names the source so the receiver knows who it took over from.
    assert "builder@acme" in state._sessions.destination.notes[0]
