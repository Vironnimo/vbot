"""``/agent <address> [task]``: move the current Session with its full history.

The dispatcher runs against recorded collaborators: the Session store, the
Agent store, Run admission, the Agent resolver, the Trigger service and the
Terminal manager.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
    CommandDispatcher,
    CommandOutcome,
    CommandResourceChange,
    ReplySurface,
)
from core.projects import AgentResolutionError, format_agent_address
from core.runs import ChatRunManager, RunAdmissionBlockedError
from core.sessions import SessionAddress
from core.tools.terminal_manager import TerminalOwner
from tests.core.chat.commands_test_support import _execute

pytestmark = pytest.mark.asyncio


class _MovedSession:
    def __init__(self) -> None:
        self.appended: list[ChatMessage] = []
        self.notes: list[str] = []

    def append(self, message: ChatMessage) -> None:
        self.appended.append(message)

    def add_note(self, content: str) -> None:
        self.notes.append(content)


class _OpenLock:
    async def __aenter__(self) -> _OpenLock:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _BlockedAdmissionGuard:
    async def __aenter__(self) -> None:
        raise RunAdmissionBlockedError("guarded")

    async def __aexit__(self, *args: Any) -> None:
        return None


class _MoveSessions:
    """Records the move call and serves the relocated Session's two writers."""

    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self._metadata = metadata or {}
        self.move_calls: list[dict[str, Any]] = []
        self.destination = _MovedSession()
        self.move_started: asyncio.Event | None = None
        self.move_release: asyncio.Event | None = None

    async def move(self, source: SessionAddress, target: SessionAddress) -> _MovedSession:
        self.move_calls.append(
            {
                "source_agent_id": source.agent_id,
                "session_id": source.session_id,
                "target_agent_id": target.agent_id,
                "source_project_id": source.project_id,
                "target_project_id": target.project_id,
                "target_session_id": target.session_id,
            }
        )
        if self.move_started is not None:
            self.move_started.set()
        if self.move_release is not None:
            await self.move_release.wait()
        return self.destination

    def get_metadata(self, address: SessionAddress) -> dict[str, Any]:
        return dict(self._metadata)

    def write_lock(self, address: SessionAddress) -> _OpenLock:
        return _OpenLock()

    def get(self, address: SessionAddress) -> _MovedSession:
        return self.destination


class _MoveAgents:
    def __init__(self) -> None:
        self.reset_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def reset_current_after_session_removed(self, agent_id: str, removed_session_id: str) -> None:
        self.reset_calls.append((agent_id, removed_session_id))

    def update(self, agent_id: str, **changes: Any) -> None:
        self.update_calls.append((agent_id, changes))


class _MoveRuns:
    def __init__(
        self, active: Any = None, queued: list[Any] | None = None, *, guard_blocked: bool = False
    ) -> None:
        self._active = active
        self._queued = queued or []
        self._guard_blocked = guard_blocked

    def active_run(self, *, agent_id: str, session_id: str, project_id: str | None) -> Any:
        return self._active

    def list_queued(self, agent_id: str, session_id: str, *, project_id: str | None) -> list[Any]:
        return list(self._queued)

    def session_admission_guard(self, *session_keys: SessionAddress) -> Any:
        return _BlockedAdmissionGuard() if self._guard_blocked else _OpenLock()


class _Resolver:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        if self._error is not None:
            raise self._error
        return SimpleNamespace(id=agent_id)


class _MoveHarness:
    """The move's collaborators plus what the dispatcher reported to them."""

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        runs: Any = None,
        resolver_error: Exception | None = None,
    ) -> None:
        self.sessions = _MoveSessions(metadata)
        self.agents = _MoveAgents()
        self.runs: Any = runs if runs is not None else _MoveRuns()
        self.resolver = _Resolver(resolver_error)
        self.trigger_calls: list[dict[str, Any]] = []
        self.terminal_transfers: list[tuple[TerminalOwner, TerminalOwner]] = []
        self.changes: list[CommandResourceChange] = []

    async def _trigger_run(self, agent_id: str, message: Any, **kwargs: Any) -> Any:
        self.trigger_calls.append({"agent_id": agent_id, "message": message, **kwargs})
        return SimpleNamespace(id="task-run")

    async def move(self, message: str, *, project_id: str | None = None) -> CommandOutcome:
        dispatcher = CommandDispatcher(
            self.runs,
            agent_resolver=cast(Any, self.resolver),
            sessions=cast(Any, self.sessions),
            agents=cast(Any, self.agents),
            trigger_service=SimpleNamespace(trigger_run=self._trigger_run),
            terminal_manager=cast(
                Any,
                SimpleNamespace(
                    transfer_scope=lambda source, target: self.terminal_transfers.append(
                        (source, target)
                    )
                ),
            ),
        )
        return await _execute(
            dispatcher,
            message,
            agent_id="builder",
            session_id="s1",
            project_id=project_id,
            on_change=self.changes.append,
        )


@pytest.mark.parametrize(
    ("source_project", "target_address", "target_agent", "target_project", "reset", "update"),
    [
        (None, "planner", "planner", None, True, True),  # identity -> identity
        (None, "planner@vbot", "planner", "vbot", True, False),  # identity -> project
        ("vbot", "assistant", "assistant", None, False, True),  # project -> identity
        ("vbot", "planner@acme", "planner", "acme", False, False),  # project -> project
    ],
)
async def test_move_directions_relocate_and_re_home_pointers(
    source_project: str | None,
    target_address: str,
    target_agent: str,
    target_project: str | None,
    reset: bool,
    update: bool,
) -> None:
    harness = _MoveHarness()

    outcome = await harness.move(f"/agent {target_address}", project_id=source_project)

    # The Session keeps its id; the command passes only the two addresses because
    # Sessions leaves the source Agent's prompt pins and seen Skills behind itself.
    assert harness.sessions.move_calls == [
        {
            "source_agent_id": "builder",
            "session_id": "s1",
            "target_agent_id": target_agent,
            "source_project_id": source_project,
            "target_project_id": target_project,
            "target_session_id": "s1",
        }
    ]
    # The "current" pointer follows the Session on each identity side only.
    assert (harness.agents.reset_calls == [("builder", "s1")]) is reset
    expected_updates = [(target_agent, {"current_session_id": "s1"})] if update else []
    assert harness.agents.update_calls == expected_updates

    # The relocation is announced like session.create/delete: a sessions change
    # for each side's list, plus one agents change when an identity current
    # pointer was re-aimed on either side. An identity side omits the project.
    changes = [
        {"kind": change.kind, **({"scope": dict(change.scope)} if change.scope else {})}
        for change in harness.changes
    ]
    source_scope = {"agent_id": "builder", "session_id": "s1"}
    if source_project is not None:
        source_scope["project_id"] = source_project
    target_scope = {"agent_id": target_agent, "session_id": "s1"}
    if target_project is not None:
        target_scope["project_id"] = target_project
    assert {"kind": "sessions", "scope": source_scope} in changes
    assert {"kind": "sessions", "scope": target_scope} in changes
    agents_changes = [change for change in changes if change["kind"] == "agents"]
    assert (agents_changes == [{"kind": "agents"}]) is (reset or update)

    # A visible takeover divider and the silent note are persisted at the destination.
    [divider] = harness.sessions.destination.appended
    assert divider.role == "agent_takeover"
    assert json.loads(str(divider.content))["to"] == target_address
    assert harness.sessions.destination.notes
    assert harness.terminal_transfers == [
        (
            TerminalOwner(source_project, "builder", "s1"),
            TerminalOwner(target_project, target_agent, "s1"),
        )
    ]

    # Without a task the target waits; the outcome offers the same Session.
    assert harness.trigger_calls == []
    assert outcome.runs == ()
    assert outcome.navigation is not None
    assert outcome.navigation.kind == "offer_session"
    assert outcome.facts == {"session_id": "s1", "agent_id": target_address}


@pytest.mark.parametrize(
    ("harness", "message"),
    [
        pytest.param(_MoveHarness, "/agent builder", id="same-pair"),
        pytest.param(
            lambda: _MoveHarness(runs=_MoveRuns(active=SimpleNamespace(id="run-1"))),
            "/agent planner",
            id="run-active",
        ),
        pytest.param(
            lambda: _MoveHarness(runs=_MoveRuns(queued=[SimpleNamespace(item_id="q-1")])),
            "/agent planner",
            id="run-queued",
        ),
        # The admission guard can win after the idle check.
        pytest.param(
            lambda: _MoveHarness(runs=_MoveRuns(guard_blocked=True)),
            "/agent planner",
            id="admission-guard",
        ),
        pytest.param(
            lambda: _MoveHarness(resolver_error=AgentResolutionError("no such agent")),
            "/agent ghost@vbot",
            id="unknown-target",
        ),
        pytest.param(_MoveHarness, "/agent agent:planner", id="invalid-address"),
        pytest.param(
            lambda: _MoveHarness(metadata={"source_channel_id": "telegram-1"}),
            "/agent planner",
            id="channel-session",
        ),
        pytest.param(
            lambda: _MoveHarness(metadata={"is_subagent_session": True}),
            "/agent planner",
            id="subagent-session",
        ),
        pytest.param(
            lambda: _MoveHarness(metadata={"subagent_parent": "parent-run-1"}),
            "/agent planner",
            id="subagent-child",
        ),
    ],
)
async def test_move_is_refused_without_relocating(harness: Any, message: str) -> None:
    subject = harness()

    outcome = await subject.move(message)

    assert outcome.feedback is not None
    assert subject.sessions.move_calls == []
    # A refused move announces nothing; the changes follow only a relocation.
    assert subject.changes == []


async def test_move_guard_rejects_source_and_destination_runs_during_storage_wait() -> None:
    runs = ChatRunManager()
    harness = _MoveHarness(runs=runs)
    harness.sessions.move_started = asyncio.Event()
    harness.sessions.move_release = asyncio.Event()

    move = asyncio.create_task(harness.move("/agent planner@vbot"))
    await asyncio.wait_for(harness.sessions.move_started.wait(), timeout=1)

    for address in (
        SessionAddress(project_id=None, agent_id="builder", session_id="s1"),
        SessionAddress(project_id="vbot", agent_id="planner", session_id="s1"),
    ):
        with pytest.raises(RunAdmissionBlockedError):
            await runs.start(address, lambda _run: asyncio.sleep(0))

    harness.sessions.move_release.set()
    outcome = await asyncio.wait_for(move, timeout=1)
    assert outcome.facts == {"session_id": "s1", "agent_id": "planner@vbot"}


@pytest.mark.parametrize(
    ("message", "agent_id", "project_id", "task"),
    [
        ("/agent planner do the thing", "planner", None, "do the thing"),
        ("/agent planner@vbot ship it", "planner", "vbot", "ship it"),
    ],
    ids=["identity-target", "project-target"],
)
async def test_move_with_task_starts_it_as_the_targets_first_visible_turn(
    message: str, agent_id: str, project_id: str | None, task: str
) -> None:
    harness = _MoveHarness()

    outcome = await harness.move(message)

    # Identity and project targets use the same trigger seam.
    assert harness.trigger_calls == [
        {
            "agent_id": agent_id,
            "message": task,
            "session_id": "s1",
            "project_id": project_id,
            "internal": False,
            "reply_surface": ReplySurface.webui(),
        }
    ]
    assert [(run.role, run.run.id) for run in outcome.runs] == [("follow_up", "task-run")]
    assert outcome.feedback is not None


async def test_move_divider_and_note_carry_both_addresses() -> None:
    harness = _MoveHarness()

    await harness.move("/agent planner@vbot", project_id="acme")

    [divider] = harness.sessions.destination.appended
    assert json.loads(str(divider.content)) == {
        "from": format_agent_address("builder", "acme"),
        "to": format_agent_address("planner", "vbot"),
    }
    # The silent note names the source so the receiver knows who it took over from.
    assert "builder@acme" in harness.sessions.destination.notes[0]
