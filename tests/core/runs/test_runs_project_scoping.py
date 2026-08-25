"""Run and queue project-scoping tests."""

from __future__ import annotations

from core.sessions import SessionAddress

from .runs_test_support import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunAdmission,
    RunCancelledError,
    RunNotFoundError,
    asyncio,
    pytest,
)

pytestmark = pytest.mark.asyncio


async def test_working_project_is_internal_and_snapshotted_through_queue() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def blocked(_run: Run) -> str:
        await release.wait()
        return "done"

    active = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        blocked,
        admission=RunAdmission(working_project_id="vbot"),
    )
    queued = await manager.enqueue(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
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


async def test_has_activity_for_agent_reports_active_and_queued_work() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_execute,
    )
    queued_item = await manager.enqueue(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        queued_execute,
        display_content="Queued next",
    )

    assert manager.has_activity_for_agent("coder", project_id=None) is True
    assert manager.has_activity_for_agent("writer", project_id=None) is False

    active_release.set()
    assert await active_run.wait() == "active"
    queued_run = await queued_item.future
    assert manager.has_activity_for_agent("coder", project_id=None) is True
    assert await queued_run.wait() == "queued"

    assert manager.has_activity_for_agent("coder", project_id=None) is False


async def test_has_activity_for_session_is_scoped_to_one_session() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        active_execute,
    )
    queued_item = await manager.enqueue(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        queued_execute,
        display_content="Queued next",
    )

    # Busy on the exact session, but not on the agent's other sessions nor on
    # the same session id under a different agent.
    assert manager.has_activity_for_session("coder", "session-one", project_id=None) is True
    assert manager.has_activity_for_session("coder", "session-two", project_id=None) is False
    assert manager.has_activity_for_session("writer", "session-one", project_id=None) is False

    active_release.set()
    assert await active_run.wait() == "active"
    queued_run = await queued_item.future
    assert await queued_run.wait() == "queued"

    assert manager.has_activity_for_session("coder", "session-one", project_id=None) is False


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


async def test_identity_run_leaves_project_id_none() -> None:
    """A run started without a project_id keeps run.project_id None (identity path)."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        return run.id

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="sess-uuid"),
        execute,
    )
    await asyncio.sleep(0)

    assert run.project_id is None
    assert manager.active_run(agent_id="coder", session_id="sess-uuid", project_id=None) is run

    release.set()
    await run.wait()


async def test_emitted_events_carry_run_project_id() -> None:
    """Every emitted event (and its to_dict) carries the run's project anchor.

    The WebSocket backstop rebuilds the outside ``agent@projekt`` address from
    this field, so a project run's events must surface it while an identity
    run's events keep it ``None`` (the byte-identical identity path).
    """
    manager = ChatRunManager()

    async def execute(run: Run) -> str:
        run.emit("visible", {"content": "hello"})
        return "done"

    project_run = await manager.start(
        SessionAddress(project_id="acme", agent_id="coder", session_id="sess-uuid"),
        execute,
    )
    await project_run.wait()
    assert all(event.project_id == "acme" for event in project_run.events)
    visible = next(event for event in project_run.events if event.type == "visible")
    assert visible.to_dict()["project_id"] == "acme"

    identity_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="sess-uuid-2"),
        execute,
    )
    await identity_run.wait()
    assert all(event.project_id is None for event in identity_run.events)
    assert identity_run.events[0].to_dict()["project_id"] is None


async def test_queued_project_run_carries_project_id_when_drained() -> None:
    """A project_id passed to enqueue rides the Run created when the item drains."""
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    drained: list[Run] = []

    async def queued_execute(run: Run) -> str:
        drained.append(run)
        return "queued"

    active_run = await manager.start(
        SessionAddress(project_id="acme", agent_id="coder", session_id="sess-uuid"),
        active_execute,
    )
    item = await manager.enqueue(
        SessionAddress(project_id="acme", agent_id="coder", session_id="sess-uuid"),
        queued_execute,
        display_content="queued",
    )

    # The session is busy, so the item is queued under the project scope.
    assert item.future.done() is False
    assert [
        queued.item_id for queued in manager.list_queued("coder", "sess-uuid", project_id="acme")
    ] == [item.item_id]
    assert manager.list_queued("coder", "sess-uuid", project_id=None) == []

    active_release.set()
    assert await active_run.wait() == "active"
    queued_run = await asyncio.wait_for(item.future, timeout=1)
    assert await queued_run.wait() == "queued"

    # The drained run carries the enqueue project anchor on the Run itself.
    assert queued_run.project_id == "acme"
    assert drained and drained[0].project_id == "acme"


async def test_cancel_by_session_is_scoped_to_the_project_anchor() -> None:
    """cancel_by_session needs the run's own project scope to find it."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    project_run = await manager.start(
        SessionAddress(project_id="acme", agent_id="coder", session_id="sess-uuid"),
        execute,
    )
    await asyncio.sleep(0)

    # The identity scope must not reach the project run.
    with pytest.raises(RunNotFoundError):
        manager.cancel_by_session("coder", "sess-uuid", project_id=None)

    cancelled = manager.cancel_by_session("coder", "sess-uuid", project_id="acme")
    assert cancelled is project_run
    assert project_run.cancel_requested is True

    release.set()
    with pytest.raises(RunCancelledError):
        await project_run.wait()


async def test_has_activity_for_agent_is_scoped_to_the_project_anchor() -> None:
    """Agent activity is checked per (project, agent) pair, not by bare id.

    An active run of project ``coder@acme`` must not read as activity of the
    same-named identity agent (or another project's ``coder``) — the false
    positive used to block deleting an unrelated identity agent / project.
    """
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await manager.start(
        SessionAddress(project_id="acme", agent_id="coder", session_id="sess-uuid"),
        execute,
    )

    assert manager.has_activity_for_agent("coder", project_id="acme") is True
    assert manager.has_activity_for_agent("coder", project_id=None) is False
    assert manager.has_activity_for_agent("coder", project_id="other") is False
    assert manager.has_activity_for_agent("writer", project_id="acme") is False

    release.set()
    await run.wait()
    assert manager.has_activity_for_agent("coder", project_id="acme") is False
