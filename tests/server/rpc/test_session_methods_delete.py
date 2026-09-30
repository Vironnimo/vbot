"""session.delete: archive, landing, refresh events and refusals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatSessionError
from core.runs import RunAdmissionBlockedError
from core.sessions import SessionAddress
from core.tools.terminal_manager import TerminalOwner
from tests.server.rpc.session_methods_test_support import FakeSessions, stub_session_state
from tests.server.rpc_test_support import JsonObject, resource_changes, rpc_error, rpc_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_session_id", "refreshed_kinds"),
    [
        pytest.param("other", ["sessions"], id="not-current"),
        # Re-aiming the current pointer is an Agent-config change, so Agent state
        # refreshes too.
        pytest.param("s1", ["sessions", "agents"], id="current"),
    ],
)
async def test_delete_identity_session_archives_and_lands_on_the_reaimed_current(
    current_session_id: str, refreshed_kinds: list[str]
) -> None:
    state, resolver, sessions = stub_session_state()
    state._agent_current["current_session_id"] = current_session_id

    result = await rpc_result(state, "session.delete", agent_id="builder", session_id="s1")

    assert result == {"agent_id": "builder", "session_id": "s1", "next_session_id": "landing"}
    assert resolver.resolved == [(None, "builder")]
    # Archived (not hard-deleted) under the identity scope.
    assert sessions.archived == [("builder", "s1", None)]
    assert state.runtime.terminal_manager.closed_scopes == [TerminalOwner(None, "builder", "s1")]
    # Identity pointer re-aimed through the shared seam; the landing is its result.
    assert state._resets == [("builder", "s1")]
    # Dropped from the recall index immediately.
    assert state._recall_removals == [("builder", "s1", None)]
    events = resource_changes(state)
    assert [event["kind"] for event in events] == refreshed_kinds
    # Other windows learn which Session was archived and where to land.
    assert events[0]["scope"] == {
        "agent_id": "builder",
        "project_id": None,
        "deleted_session_id": "s1",
        "next_session_id": "landing",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remaining", "landing"),
    [
        pytest.param(
            [
                {"id": "old", "last_active_at": "2026-01-01T00:00:00+00:00"},
                {"id": "recent", "last_active_at": "2026-06-01T00:00:00+00:00"},
            ],
            "recent",
            id="most-recent-remaining",
        ),
        pytest.param([], "new-session", id="fresh-when-none-remain"),
    ],
)
async def test_delete_project_session_lands_without_a_current_pointer(
    remaining: list[JsonObject], landing: str
) -> None:
    state, _resolver, sessions = stub_session_state()
    sessions.metadata_rows = remaining

    result = await rpc_result(state, "session.delete", agent_id="builder@vbot", session_id="s1")

    assert result["next_session_id"] == landing
    assert sessions.archived == [("builder", "s1", "vbot")]
    assert [created["project_id"] for created in sessions.created] == (
        ["vbot"] if not remaining else []
    )
    # A Project Agent has no identity current pointer to re-aim.
    assert state._resets == []
    assert state._recall_removals == [("builder", "s1", "vbot")]
    assert resource_changes(state) == [
        {
            "kind": "sessions",
            "scope": {
                "agent_id": "builder",
                "project_id": "vbot",
                "deleted_session_id": "s1",
                "next_session_id": landing,
            },
        }
    ]


@pytest.mark.asyncio
async def test_delete_busy_session_is_rejected() -> None:
    state, _resolver, sessions = stub_session_state()

    async with state.chat_runs.session_admission_guard(
        SessionAddress(project_id=None, agent_id="builder", session_id="s1")
    ):
        error = await rpc_error(state, "session.delete", agent_id="builder", session_id="s1")

    assert error["code"] == "session_busy"
    # The guard fires before any file work: nothing archived, nothing re-aimed.
    assert sessions.archived == []
    assert state._resets == []


def _jobs(service: str, **fields: Any) -> Callable[[Any], None]:
    """Install a Bootstrap or Cron service listing one job pinned by default to builder/s1."""
    job = SimpleNamespace(
        **{
            "id": "job-1",
            "name": "Daily report",
            "agent_id": "builder",
            "project_id": None,
            "session_id": "s1",
            "status": "active",
            **fields,
        }
    )

    def arrange(runtime: Any) -> None:
        setattr(runtime, service, SimpleNamespace(list_jobs=lambda: [job]))

    return arrange


def _calendar_action(**fields: Any) -> Callable[[Any], None]:
    """Install a Calendar listing one action pinned by default to builder/s1."""
    action = {
        "id": "act-1",
        "event_id": "evt-1",
        "target": "builder",
        "session": "s1",
        **fields,
    }

    def arrange(runtime: Any) -> None:
        runtime.calendar_service = SimpleNamespace(
            actions=SimpleNamespace(list_actions=lambda: [action]),
            list_events=lambda: [SimpleNamespace(id="evt-1", title="Weekly review")],
        )

    return arrange


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "reference"),
    [
        pytest.param(
            _jobs("bootstrap_service", id="boot-1", name="Warm up"),
            {"kind": "bootstrap", "id": "boot-1", "name": "Warm up"},
            id="bootstrap",
        ),
        # A failed job can be enabled again, so its pin still counts.
        pytest.param(
            _jobs("cron_service", id="cron-1", status="failed"),
            {"kind": "cron", "id": "cron-1", "name": "Daily report"},
            id="cron",
        ),
        # An action is named by its event's title.
        pytest.param(
            _calendar_action(),
            {"kind": "calendar", "id": "act-1", "name": "Weekly review"},
            id="calendar",
        ),
    ],
)
async def test_delete_session_pinned_by_an_automation_is_rejected(
    arrange: Callable[[Any], None], reference: JsonObject
) -> None:
    state, _resolver, sessions = stub_session_state()
    arrange(state.runtime)

    error = await rpc_error(state, "session.delete", agent_id="builder", session_id="s1")

    assert error["code"] == "session_in_use"
    assert f"{reference['kind']}:{reference['id']}" in error["message"]
    assert error["data"] == {"references": [reference]}
    assert sessions.archived == []
    assert resource_changes(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arrange",
    [
        # Terminal history never starts another Run.
        pytest.param(_jobs("cron_service", status="completed"), id="cron-terminal"),
        pytest.param(_jobs("bootstrap_service", status="completed"), id="bootstrap-terminal"),
        # The same Session id in another scope is another Session.
        pytest.param(_jobs("cron_service", project_id="vbot"), id="cron-other-scope"),
        pytest.param(_calendar_action(target="builder@vbot"), id="calendar-other-scope"),
    ],
)
async def test_delete_ignores_automations_that_cannot_start_in_the_session(
    arrange: Callable[[Any], None],
) -> None:
    state, _resolver, sessions = stub_session_state()
    arrange(state.runtime)

    await rpc_result(state, "session.delete", agent_id="builder", session_id="s1")

    assert sessions.archived == [("builder", "s1", None)]


@pytest.mark.asyncio
async def test_delete_guard_rejects_run_while_archive_is_waiting() -> None:
    state, _resolver, sessions = stub_session_state()
    sessions.archive_started = asyncio.Event()
    sessions.archive_release = asyncio.Event()

    delete_task = asyncio.create_task(
        rpc_result(state, "session.delete", agent_id="builder", session_id="s1")
    )
    await asyncio.wait_for(sessions.archive_started.wait(), timeout=1)

    with pytest.raises(RunAdmissionBlockedError):
        await state.chat_runs.start(
            SessionAddress(project_id=None, agent_id="builder", session_id="s1"),
            lambda _run: asyncio.sleep(0),
        )
    sessions.archive_release.set()
    result = await asyncio.wait_for(delete_task, timeout=1)
    assert result["session_id"] == "s1"


def _missing(sessions: FakeSessions) -> None:
    sessions.missing = {"s1"}


def _owner_managed(sessions: FakeSessions) -> None:
    sessions.archive_error = ChatSessionError(
        "This Session is managed by an Extension. Use that Extension to resume it."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "message", "archived"),
    [
        (_missing, "session does not exist: s1", []),
        (
            _owner_managed,
            "This Session is managed by an Extension. Use that Extension to resume it.",
            [("builder", "s1", None)],
        ),
    ],
    ids=["missing", "owner-managed"],
)
async def test_a_refused_delete_is_a_domain_error_without_a_refresh(
    arrange: Callable[[FakeSessions], None],
    message: str,
    archived: list[tuple[str, str, str | None]],
) -> None:
    state, _resolver, sessions = stub_session_state()
    arrange(sessions)

    error = await rpc_error(state, "session.delete", agent_id="builder", session_id="s1")

    assert error == {"code": "domain_error", "message": message}
    assert sessions.archived == archived
    assert resource_changes(state) == []
