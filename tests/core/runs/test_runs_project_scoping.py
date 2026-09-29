"""Run and queue scoping by project anchor, Agent, Session and Working Project."""

from __future__ import annotations

import asyncio

import pytest

from core.runs import ActiveRunError, ChatRunManager, Run, RunAdmission, RunCancelledError
from core.sessions import SessionAddress
from tests.core.runs.runs_test_support import SESSION, held

pytestmark = pytest.mark.asyncio


async def test_working_project_is_internal_and_snapshotted_through_queue() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def blocked(_run: Run) -> str:
        await release.wait()
        return "done"

    active = await manager.start(
        SESSION,
        blocked,
        admission=RunAdmission(working_project_id="vbot"),
    )
    queued = await manager.enqueue(
        SESSION,
        lambda run: asyncio.sleep(0, result=run.working_project_id),
        admission=RunAdmission(working_project_id="other"),
    )

    assert manager.has_activity_for_working_project("vbot") is True
    assert manager.has_activity_for_working_project("other") is True
    assert "working_project_id" not in queued.to_dict()

    release.set()
    await active.wait()
    queued_run = await queued.future
    assert queued_run.project_id is None
    assert queued_run.working_project_id == "other"
    assert await queued_run.wait() == "other"


async def test_project_and_identity_sessions_with_same_ids_never_collide() -> None:
    """The run key is ``(project_id, agent_id, session_id)`` — anchors stay apart.

    ``session.create`` accepts caller-chosen session ids, so identity ``builder``
    and project ``builder@vbot`` can both own a session named the same. The two
    must never block, cancel, or guard each other.
    """
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        return run.id

    project_run = await manager.start(
        SessionAddress(project_id="acme", agent_id="coder", session_id="main"),
        execute,
    )
    await asyncio.sleep(0)

    # The project anchor rides the Run, available to its session I/O.
    assert project_run.project_id == "acme"

    # The project-scoped lookup finds it; the identity scope does not.
    assert manager.active_run(agent_id="coder", session_id="main", project_id="acme") is project_run
    assert manager.active_run(agent_id="coder", session_id="main", project_id=None) is None

    # An identity run on the same (agent, session) ids starts fine in parallel …
    identity_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="main"),
        execute,
    )
    # Let the executor task actually start before cancelling it (a same-tick
    # cancel would close the never-run task without terminal bookkeeping).
    await asyncio.sleep(0)
    # … while a second start in the *same* anchor is still rejected.
    with pytest.raises(ActiveRunError):
        await manager.start(
            SessionAddress(project_id="acme", agent_id="coder", session_id="main"),
            execute,
        )

    # Cancelling the identity session leaves the project run untouched.
    cancelled = manager.cancel_by_session("coder", "main", project_id=None)
    assert cancelled is identity_run
    assert project_run.cancel_requested is False

    release.set()
    with pytest.raises(RunCancelledError):
        await identity_run.wait()
    assert await project_run.wait() == project_run.id


@pytest.mark.parametrize("project_id", [None, "acme"], ids=["identity", "project"])
async def test_run_and_its_events_carry_the_project_anchor(project_id: str | None) -> None:
    """The WebSocket backstop rebuilds the outside `agent@project` address from events."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        run.emit("visible", {"content": "hello"})
        await release.wait()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=project_id, agent_id="coder", session_id="sess-uuid"), execute
    )
    await asyncio.sleep(0)

    assert run.project_id == project_id
    assert (
        manager.active_run(agent_id="coder", session_id="sess-uuid", project_id=project_id) is run
    )
    release.set()
    assert await run.wait() == "done"
    assert all(event.project_id == project_id for event in run.events)
    assert all(event.to_dict()["project_id"] == project_id for event in run.events)


async def test_activity_lookups_cover_active_and_queued_work_of_one_anchor() -> None:
    """Activity is keyed by (project, Agent, Session), never by a bare id.

    A false positive used to block deleting an unrelated identity Agent or Project.
    """
    manager = ChatRunManager()
    address = SessionAddress(project_id="acme", agent_id="coder", session_id="session-one")
    active_execute, active_release = held("active")
    queued_release = asyncio.Event()
    drained: list[Run] = []

    async def queued_execute(run: Run) -> str:
        drained.append(run)
        await queued_release.wait()
        return "queued"

    def agent_activity() -> list[bool]:
        return [
            manager.has_activity_for_agent(agent_id, project_id=project_id)
            for agent_id, project_id in [
                ("coder", "acme"),
                ("coder", None),
                ("coder", "other"),
                ("writer", "acme"),
            ]
        ]

    def session_activity() -> list[bool]:
        return [
            manager.has_activity_for_session(agent_id, session_id, project_id=project_id)
            for agent_id, session_id, project_id in [
                ("coder", "session-one", "acme"),
                ("coder", "session-one", None),
                ("coder", "session-two", "acme"),
                ("writer", "session-one", "acme"),
            ]
        ]

    active_run = await manager.start(address, active_execute)
    item = await manager.enqueue(address, queued_execute, display_content="queued")

    assert agent_activity() == [True, False, False, False]
    assert session_activity() == [True, False, False, False]
    assert [
        queued.item_id for queued in manager.list_queued("coder", "session-one", project_id="acme")
    ] == [item.item_id]
    assert manager.list_queued("coder", "session-one", project_id=None) == []

    active_release.set()
    assert await active_run.wait() == "active"
    queued_run = await asyncio.wait_for(item.future, timeout=1)
    assert agent_activity()[0] is True
    queued_release.set()
    assert await queued_run.wait() == "queued"

    assert drained == [queued_run]
    assert queued_run.project_id == "acme"
    assert agent_activity() == [False] * 4
    assert session_activity() == [False] * 4
