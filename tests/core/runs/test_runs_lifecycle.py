"""Run lifecycle, event metadata and replay, active-run lookup and admission waits."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Any

import pytest

from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    FINISHED_RUN_REPLAY_LIMIT,
    MODEL_STEP_USAGE_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT,
    REASONING_DELTA_EVENT,
    RUN_AGENT_ACTIVITY_FIELD,
    RUN_KIND_FIELD,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_OUTPUT_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    ChatRunManager,
    Run,
    RunAdmission,
    RunAdmissionBlockedError,
    RunCancelledError,
    RunKind,
    RunNotFoundError,
    RunStatus,
)
from core.sessions import SessionAddress
from tests.core.runs.runs_test_support import SESSION, RunTimelines, held

pytestmark = pytest.mark.asyncio


async def test_manager_aclose_cancels_active_and_queued_work_and_rejects_new_runs() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()

    async def blocking_executor(_run: Run) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    active = await manager.start(
        SESSION,
        blocking_executor,
    )
    await started.wait()
    queued = await manager.enqueue(
        SESSION,
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
    replay = asyncio.create_task(_collect(run.subscribe(after_sequence=1)))
    await asyncio.sleep(0)
    run.mark_completed("done")
    replayed_events = await replay

    assert [event.sequence for event in replayed_events] == [2, 3, 4]
    assert [event.type for event in replayed_events] == [
        REASONING_DELTA_EVENT,
        TOOL_CALL_DELTA_EVENT,
        "run_completed",
    ]
    assert replayed_events[1].payload == {
        "tool_call_id": "tool-one",
        "name_delta": "read",
    }


async def _collect(events: Any) -> list[Any]:
    return [event async for event in events]


@pytest.mark.parametrize(
    ("emitted", "expected_ending"),
    [
        (
            [
                TOOL_CALL_STARTED_EVENT,
                TOOL_CALL_OUTPUT_EVENT,
                TOOL_CALL_RESULT_EVENT,
                ASSISTANT_OUTPUT_DELTA_EVENT,
                ASSISTANT_OUTPUT_EVENT,
                PROVIDER_REQUEST_STATUS_EVENT,
                MODEL_STEP_USAGE_EVENT,
            ],
            [
                ASSISTANT_OUTPUT_EVENT,
                PROVIDER_REQUEST_STATUS_EVENT,
                MODEL_STEP_USAGE_EVENT,
                "run_completed",
            ],
        ),
        ([ASSISTANT_OUTPUT_EVENT, ASSISTANT_OUTPUT_DELTA_EVENT], ["run_completed"]),
        (
            ["visible"] * 40,
            ["visible"] * (FINISHED_RUN_REPLAY_LIMIT - 1) + ["run_completed"],
        ),
    ],
    ids=["settled-step", "transient-before-terminal", "capped"],
)
async def test_finished_run_replays_only_its_settled_ending(
    emitted: list[str], expected_ending: list[str]
) -> None:
    manager = ChatRunManager()
    timelines = RunTimelines(manager)

    async def execute(run: Run) -> str:
        await asyncio.sleep(0)  # The live follower subscribes before the Run ends.
        for event_type in emitted:
            run.emit(event_type)
        return "done"

    run = await manager.start(SESSION, execute)
    await run.wait()
    followed = await timelines.events(run)
    late = [event async for event in run.subscribe()]

    # A live subscriber saw every event; one arriving after the end gets the
    # terminal event plus the non-transient events directly before it.
    assert [event.sequence for event in followed] == list(range(1, len(emitted) + 3))
    assert [event.type for event in late] == expected_ending
    assert late == followed[-len(expected_ending) :] == run.events
    assert run.last_sequence == followed[-1].sequence
    assert [event async for event in run.subscribe(after_sequence=run.last_sequence)] == []


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
        SESSION,
        fail,
    )
    with pytest.raises(RuntimeError, match="boom"):
        await failed_run.wait()

    next_run = await manager.start(
        SESSION,
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
        SESSION,
        execute,
    )
    await first_run.wait()
    remove_callback()

    second_run = await manager.start(
        SESSION,
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


class _BlockingAdmission:
    """Session persistence whose admission waits for the test to release it."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.release = asyncio.Event()
        self.failure = failure
        self.admitted: list[str] = []
        self.finished: list[str] = []

    async def start_run(self, run: Run) -> None:
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        self.admitted.append(run.id)

    async def finish_run(
        self, run: Run, status: str, payload: dict[str, Any], *, completion_reason: str | None
    ) -> dict[str, Any]:
        del payload, completion_reason
        self.finished.append(status)
        return {}


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_wait_admitted_returns_only_after_the_admission_commits() -> None:
    persistence = _BlockingAdmission()
    manager = ChatRunManager(persistence=persistence)
    executed: list[str] = []

    async def execute(run: Run) -> str:
        executed.append(run.id)
        return "done"

    run = await manager.start(SESSION, execute)
    waiter = asyncio.create_task(run.wait_admitted())
    await _settle()
    assert not waiter.done()
    assert executed == []

    persistence.release.set()
    await waiter
    assert persistence.admitted == [run.id]
    assert await run.wait() == "done"
    assert executed == [run.id]
    assert persistence.finished == [RunStatus.COMPLETED.value]
    await asyncio.wait_for(run.wait_admitted(), timeout=1)
    await manager.aclose()


async def test_wait_admitted_raises_the_error_of_a_failed_admission() -> None:
    persistence = _BlockingAdmission(failure=RuntimeError("admission rejected"))
    manager = ChatRunManager(persistence=persistence)
    executed: list[str] = []

    async def execute(run: Run) -> str:
        executed.append(run.id)
        return "done"

    run = await manager.start(SESSION, execute)
    waiter = asyncio.create_task(run.wait_admitted())
    await _settle()
    persistence.release.set()
    with pytest.raises(RuntimeError, match="admission rejected"):
        await waiter
    assert run.status == RunStatus.FAILED
    assert executed == []
    # An unadmitted Run has no durable row to finish.
    assert persistence.finished == []
    await manager.aclose()


async def test_wait_admitted_raises_for_a_run_that_ended_unadmitted() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    run.mark_cancelled()
    with pytest.raises(RunCancelledError, match="run ended before admission: run-one"):
        await run.wait_admitted()


async def test_wait_admitted_returns_at_once_without_session_persistence() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await manager.start(SESSION, execute)
    await asyncio.wait_for(run.wait_admitted(), timeout=1)
    assert run.status == RunStatus.RUNNING
    release.set()
    assert await run.wait() == "done"
    await manager.aclose()


@pytest.mark.parametrize(
    ("admission", "run_kind", "contributes"),
    [
        (RunAdmission(), RunKind.USER, True),
        (
            RunAdmission(run_kind=RunKind.CRON, contributes_to_agent_activity=False),
            RunKind.CRON,
            False,
        ),
    ],
    ids=["default", "cron-system-work"],
)
async def test_every_event_carries_run_metadata_and_replays_to_a_late_subscriber(
    admission: RunAdmission, run_kind: RunKind, contributes: bool
) -> None:
    async def execute(run: Run) -> str:
        run.emit("visible", {"content": "hello"})
        return "done"

    run = await ChatRunManager().start(SESSION, execute, admission=admission)
    assert await run.wait() == "done"

    events = [event async for event in run.subscribe()]
    assert [event.type for event in events] == ["run_started", "visible", "run_completed"]
    assert events[0].payload == {"status": RunStatus.RUNNING.value}
    assert events[1].payload == {"content": "hello"}
    assert (run.run_kind, run.contributes_to_agent_activity) == (run_kind, contributes)
    assert all(event.run_kind is run_kind for event in events)
    assert all(event.to_dict()[RUN_KIND_FIELD] == run_kind.value for event in events)
    # The wire field appears only for Runs that do not count as Agent activity.
    assert all(event.contributes_to_agent_activity is contributes for event in events)
    assert all(
        event.to_dict().get(RUN_AGENT_ACTIVITY_FIELD, True) is contributes for event in events
    )


async def test_active_runs_lists_running_runs_of_every_session_only() -> None:
    manager = ChatRunManager()
    held_runs = {}
    for session_id in ("session-one", "session-two"):
        execute, release = held(session_id)
        address = SessionAddress(project_id=None, agent_id="coder", session_id=session_id)
        held_runs[session_id] = (await manager.start(address, execute), release)
    finished = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-three"),
        lambda _run: asyncio.sleep(0, result="done"),
    )
    assert await finished.wait() == "done"
    await asyncio.sleep(0)

    assert set(manager.active_runs()) == {run for run, _release in held_runs.values()}
    assert all(run.status == RunStatus.RUNNING for run in manager.active_runs())
    assert manager.active_run(agent_id="coder", session_id="session-three", project_id=None) is None
    assert all(manager.is_running(run.id) for run, _release in held_runs.values())
    assert not manager.is_running(finished.id)
    assert not manager.is_running("run-unknown")

    for session_id, (run, release) in held_runs.items():
        release.set()
        assert await run.wait() == session_id
        assert not manager.is_running(run.id)
    assert manager.active_runs() == []
