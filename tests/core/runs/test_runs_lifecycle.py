"""Run lifecycle, event replay, and active-run lookup tests."""

from __future__ import annotations

from core.sessions import SessionAddress

from .runs_test_support import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    RUN_AGENT_ACTIVITY_FIELD,
    RUN_KIND_FIELD,
    RUN_STARTED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    Any,
    ChatRunManager,
    Run,
    RunAdmission,
    RunAdmissionBlockedError,
    RunKind,
    RunNotFoundError,
    RunStatus,
    aclosing,
    asyncio,
    pytest,
)

pytestmark = pytest.mark.asyncio


async def test_replays_events_to_late_subscriber() -> None:
    manager = ChatRunManager()

    async def execute(run: Run) -> str:
        run.emit("visible", {"content": "hello"})
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    assert await run.wait() == "done"

    events = [event async for event in run.subscribe()]

    assert [event.type for event in events] == ["run_started", "visible", "run_completed"]
    assert events[1].payload == {"content": "hello"}


async def test_run_activity_projection_policy_is_carried_by_every_event() -> None:
    manager = ChatRunManager()

    async def execute(run: Run) -> str:
        run.emit("visible", {"content": "system work"})
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
        admission=RunAdmission(contributes_to_agent_activity=False),
    )
    assert await run.wait() == "done"

    assert run.contributes_to_agent_activity is False
    assert [event.type for event in run.events] == [
        "run_started",
        "visible",
        "run_completed",
    ]
    assert all(event.contributes_to_agent_activity is False for event in run.events)
    assert all(event.to_dict()[RUN_AGENT_ACTIVITY_FIELD] is False for event in run.events)


async def test_run_kind_is_carried_by_run_and_every_event() -> None:
    manager = ChatRunManager()

    async def execute(_run: Run) -> str:
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
        admission=RunAdmission(run_kind=RunKind.CRON),
    )
    assert await run.wait() == "done"

    assert run.run_kind is RunKind.CRON
    assert all(event.run_kind is RunKind.CRON for event in run.events)
    assert all(event.to_dict()[RUN_KIND_FIELD] == "cron" for event in run.events)


async def test_allows_parallel_runs_for_different_sessions() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()
    started: list[str] = []

    async def execute(run: Run) -> str:
        started.append(run.session_id)
        await release.wait()
        return run.session_id

    first_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    second_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-two"),
        execute,
    )
    await asyncio.sleep(0)

    release.set()

    assert set(started) == {"session-one", "session-two"}
    assert await first_run.wait() == "session-one"
    assert await second_run.wait() == "session-two"


async def test_manager_aclose_cancels_active_and_queued_work_and_rejects_new_runs() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()

    async def blocking_executor(_run: Run) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    active = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        blocking_executor,
    )
    await started.wait()
    queued = await manager.enqueue(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        blocking_executor,
        display_content="queued",
    )

    await manager.aclose()

    assert active.status == RunStatus.CANCELLED
    assert manager.active_runs() == []
    with pytest.raises(asyncio.CancelledError):
        await queued.future
    with pytest.raises(RunAdmissionBlockedError, match="shutting down"):
        await manager.start(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-two"),
            blocking_executor,
        )


async def test_delta_events_use_normal_sequences_and_replay_filtering() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "Hel"})
    run.emit(REASONING_DELTA_EVENT, {"reasoning_delta": "Thinking"})
    run.emit(TOOL_CALL_DELTA_EVENT, {"tool_call_id": "tool-one", "name_delta": "read"})
    run.mark_completed("done")

    replayed_events = [event async for event in run.subscribe(after_sequence=1)]

    assert [event.sequence for event in run.events] == [1, 2, 3, 4]
    assert [event.type for event in replayed_events] == [
        REASONING_DELTA_EVENT,
        TOOL_CALL_DELTA_EVENT,
        "run_completed",
    ]
    assert replayed_events[1].payload == {
        "tool_call_id": "tool-one",
        "name_delta": "read",
    }


async def test_run_event_replay_window_is_bounded_without_reusing_sequences() -> None:
    run = Run(
        run_id="run-one",
        agent_id="coder",
        session_id="session-one",
        event_retention_limit=3,
    )

    for index in range(5):
        run.emit("visible", {"index": index})
    run.mark_completed("done")

    retained_events = run.events
    replayed_events = [event async for event in run.subscribe()]

    assert [event.sequence for event in retained_events] == [4, 5, 6]
    assert [event.sequence for event in replayed_events] == [4, 5, 6]
    assert [event.payload for event in replayed_events[:2]] == [{"index": 3}, {"index": 4}]
    assert replayed_events[-1].type == "run_completed"


async def test_run_subscribe_evicts_lagging_live_subscriber() -> None:
    run = Run(
        run_id="run-one",
        agent_id="coder",
        session_id="session-one",
        subscriber_queue_limit=2,
    )

    async with aclosing(run.subscribe()) as stream:
        first_event_task = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)

        first_event = run.emit("run_started")
        streamed_event = await first_event_task

        run.emit("visible", {"index": 1})
        run.emit("visible", {"index": 2})
        run.emit("visible", {"index": 3})

        assert first_event is not None
        assert streamed_event.sequence == first_event.sequence
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()


async def test_failed_run_releases_session_lock() -> None:
    manager = ChatRunManager()

    async def fail(_run: Run) -> Any:
        raise RuntimeError("boom")

    async def succeed(_run: Run) -> str:
        return "ok"

    failed_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        fail,
    )
    with pytest.raises(RuntimeError, match="boom"):
        await failed_run.wait()

    next_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        succeed,
    )

    assert await next_run.wait() == "ok"


async def test_run_started_callbacks_are_notified_and_removable() -> None:
    manager = ChatRunManager()
    observed_runs: list[Run] = []

    async def execute(_run: Run) -> str:
        return "done"

    remove_callback = manager.add_run_started_callback(observed_runs.append)
    first_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await first_run.wait()
    remove_callback()

    second_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await second_run.wait()

    assert observed_runs == [first_run]


async def test_completed_run_lookup_retention_is_bounded() -> None:
    manager = ChatRunManager(completed_run_retention_limit=2)

    async def execute(run: Run) -> str:
        return run.id

    first_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="one"),
        execute,
    )
    await first_run.wait()
    second_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="two"),
        execute,
    )
    await second_run.wait()
    third_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="three"),
        execute,
    )
    await third_run.wait()

    with pytest.raises(RunNotFoundError):
        manager.get(first_run.id)
    assert manager.get(second_run.id) is second_run
    assert manager.get(third_run.id) is third_run


async def test_active_runs_returns_running_runs_and_omits_terminal_runs() -> None:
    """active_runs() exposes only RUNNING entries; terminal ones are filtered out."""
    manager = ChatRunManager()
    running_release = asyncio.Event()
    started = asyncio.Event()

    async def running_execute(_run: Run) -> str:
        started.set()
        await running_release.wait()
        return "active"

    async def finishing_execute(_run: Run) -> str:
        return "done"

    running_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        running_execute,
    )
    await started.wait()

    finishing_run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-two"),
        finishing_execute,
    )
    assert await finishing_run.wait() == "done"
    assert finishing_run.status == RunStatus.COMPLETED

    active = manager.active_runs()

    assert active == [running_run]
    assert all(run.status == RunStatus.RUNNING for run in active)
    assert manager.active_run(agent_id="coder", session_id="session-two", project_id=None) is None

    running_release.set()
    assert await running_run.wait() == "active"

    assert manager.active_runs() == []


async def test_active_runs_returns_runs_across_multiple_sessions() -> None:
    """active_runs() returns the running run from every session that has one."""
    manager = ChatRunManager()
    started_events = {
        "session-one": asyncio.Event(),
        "session-two": asyncio.Event(),
        "session-three": asyncio.Event(),
    }
    releases = {session: asyncio.Event() for session in started_events}

    async def execute(_run: Run) -> str:
        started_events[_run.session_id].set()
        await releases[_run.session_id].wait()
        return _run.session_id

    runs_by_session: dict[str, Run] = {}
    for session_id in started_events:
        runs_by_session[session_id] = await manager.start(
            SessionAddress(project_id=None, agent_id="coder", session_id=session_id),
            execute,
        )

    for _session_id, event in started_events.items():
        await event.wait()

    active = manager.active_runs()

    assert set(active) == set(runs_by_session.values())
    assert {run.session_id for run in active} == set(runs_by_session)
    assert all(run.status == RunStatus.RUNNING for run in active)
    assert len(active) == len(runs_by_session)

    for session_id, release in releases.items():
        release.set()
        assert await runs_by_session[session_id].wait() == session_id

    assert manager.active_runs() == []


async def test_start_run_payload_omits_queue_item_id() -> None:
    """A plain start() call produces a run_started payload without queue_item_id."""
    manager = ChatRunManager()

    async def execute(_run: Run) -> str:
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    assert await run.wait() == "done"

    started_events = [event for event in run.events if event.type == RUN_STARTED_EVENT]
    assert len(started_events) == 1
    assert started_events[0].payload == {"status": RunStatus.RUNNING.value}
    assert "queue_item_id" not in started_events[0].payload
