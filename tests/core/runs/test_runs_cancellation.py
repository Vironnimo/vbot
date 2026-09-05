"""Run cancellation and cancellation-callback tests."""

from __future__ import annotations

import subprocess
import sys
from textwrap import dedent

from core.sessions import SessionAddress

from .runs_test_support import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ChatRunManager,
    Run,
    RunCancelledError,
    RunNotFoundError,
    RunStatus,
    assert_timing_payload,
    asyncio,
    logging,
    pytest,
)

pytestmark = pytest.mark.asyncio
SUBPROCESS_TIMEOUT_SECONDS = 10


async def test_immediate_async_cleanup_settles_cancel_and_starts_queued_run() -> None:
    # A regression can starve asyncio itself, so only a separate interpreter's
    # timeout can safely bound this production-manager scenario.
    scenario = dedent(
        """
        import asyncio

        from core.runs import ChatRunManager, RunStatus
        from core.sessions import SessionAddress

        async def main():
            manager = ChatRunManager()
            started = asyncio.Event()
            completed = []

            async def cleanup():
                completed.append("cleanup")

            async def first_executor(run):
                run.add_cancel_callback(cleanup)
                started.set()
                await asyncio.Event().wait()

            async def second_executor(run):
                completed.append("second")
                return "second"

            address = SessionAddress(project_id=None, agent_id="coder", session_id="session")
            first = await manager.start(address, first_executor)
            await started.wait()
            queued = await manager.enqueue(address, second_executor)

            await manager.cancel(first.id, reason="user")
            second = await queued.future
            assert await second.wait() == "second"
            assert first.status == RunStatus.CANCELLED
            assert completed == ["cleanup", "second"]
            await manager.aclose()

        asyncio.run(main())
        """
    )

    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", scenario],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr


async def test_cancel_marks_run_cancelled_and_suppresses_late_output() -> None:
    manager = ChatRunManager()
    output_started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        run.emit("visible", {"step": "before"})
        output_started.set()
        await release.wait()
        run.emit("visible", {"step": "late"})
        return "ignored"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await output_started.wait()
    run.request_cancel()
    release.set()

    with pytest.raises(RunCancelledError):
        await run.wait()

    assert run.status == RunStatus.CANCELLED
    assert [event.payload for event in run.events if event.type == "visible"] == [
        {"step": "before"}
    ]
    assert run.events[-1].type == "run_cancelled"
    assert_timing_payload(run.events[-1].payload)


async def test_immediate_cancel_reaches_terminal_and_releases_session() -> None:
    manager = ChatRunManager()
    executor_started = False

    async def execute(_run: Run) -> str:
        nonlocal executor_started
        executor_started = True
        return "must not run"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    run.request_cancel()

    with pytest.raises(RunCancelledError):
        await asyncio.wait_for(run.wait(), timeout=1)
    await asyncio.sleep(0)

    assert executor_started is False
    assert run.status == RunStatus.CANCELLED
    assert run.events[-1].type == "run_cancelled"
    assert manager.active_run(agent_id="coder", session_id="session-one", project_id=None) is None

    replacement = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    assert await replacement.wait() == "must not run"


async def test_delta_events_obey_cancel_guard() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    first_event = run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "before"})
    run.request_cancel()
    late_event = run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "late"})

    assert first_event is not None
    assert late_event is None
    assert [event.payload for event in run.events] == [{"content_delta": "before"}]


async def test_cancel_invokes_registered_abort_callback() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()
    callbacks: list[str] = []

    async def execute(run: Run) -> str:
        run.add_cancel_callback(lambda: callbacks.append("aborted"))
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await asyncio.sleep(0)
    await manager.cancel(run.id)
    release.set()

    assert callbacks == ["aborted"]
    assert run.status == RunStatus.CANCELLED


async def test_cancel_keeps_session_owned_until_async_cleanup_finishes() -> None:
    manager = ChatRunManager()
    first_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    second_started = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    async def first_executor(run: Run) -> str:
        run.add_cancel_callback(cleanup)
        first_started.set()
        await asyncio.Event().wait()
        return "unreachable"

    async def second_executor(_run: Run) -> str:
        second_started.set()
        return "second"

    first = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        first_executor,
    )
    await first_started.wait()
    queued = await manager.enqueue(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        second_executor,
        display_content="second",
    )

    cancelling = asyncio.create_task(manager.cancel(first.id, reason="user"))
    await cleanup_started.wait()
    await asyncio.sleep(0)

    assert not second_started.is_set()
    assert not queued.future.done()

    cleanup_release.set()
    assert await cancelling is first
    second = await queued.future
    assert await second.wait() == "second"
    assert second_started.is_set()


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancel_drains_cleanup_registered_by_another_callback(
    cleanup_fails: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    nested_started = asyncio.Event()
    nested_release = asyncio.Event()
    completed: list[str] = []
    failure = RuntimeError("nested cleanup failure")
    caplog.set_level(logging.WARNING, logger="vbot.runs")

    async def nested_cleanup() -> None:
        nested_started.set()
        await nested_release.wait()
        completed.append("cleanup")
        if cleanup_fails:
            raise failure

    async def first_executor(run: Run) -> str:
        async def cleanup() -> None:
            cleanup_started.set()
            await cleanup_release.wait()
            run.add_cancel_callback(nested_cleanup)

        run.add_cancel_callback(cleanup)
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    async def second_executor(_run: Run) -> str:
        completed.append("second")
        return "second"

    address = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    first = await manager.start(address, first_executor)
    await started.wait()
    queued = await manager.enqueue(address, second_executor)

    cancelling = asyncio.create_task(manager.cancel(first.id, reason="user"))
    await cleanup_started.wait()
    cleanup_release.set()
    await nested_started.wait()

    assert not queued.future.done()
    assert first.status == RunStatus.RUNNING

    nested_release.set()
    assert await cancelling is first
    second = await queued.future
    assert await second.wait() == "second"
    assert first.status == RunStatus.CANCELLED
    assert completed == ["cleanup", "second"]
    logged_failure = any(
        record.exc_info and record.exc_info[1] is failure for record in caplog.records
    )
    assert logged_failure == cleanup_fails


async def test_cancel_callback_failure_does_not_skip_remaining_callbacks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    callbacks: list[str] = []

    def fail() -> None:
        callbacks.append("failed")
        raise RuntimeError("cancel callback boom")

    def succeed() -> None:
        callbacks.append("succeeded")

    run.add_cancel_callback(fail)
    run.add_cancel_callback(succeed)

    caplog.set_level(logging.WARNING, logger="vbot.runs")
    run.request_cancel()

    assert callbacks == ["failed", "succeeded"]
    assert run.cancel_requested is True
    assert caplog.records


async def test_async_cancel_callback_failure_is_observed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    async def fail() -> None:
        raise RuntimeError("async cancel callback boom")

    run.add_cancel_callback(fail)

    caplog.set_level(logging.WARNING, logger="vbot.runs")
    run.request_cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert caplog.records


async def test_cancel_by_session_requests_cancel_and_returns_run() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await started.wait()

    cancelled_run = manager.cancel_by_session(
        "coder",
        "session-one",
        project_id=None,
        reason="user",
    )

    assert cancelled_run is run
    assert run.cancel_requested is True
    assert run.cancel_reason == "user"

    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()


async def test_cancel_by_session_without_active_run_raises_not_found() -> None:
    manager = ChatRunManager()

    with pytest.raises(RunNotFoundError):
        manager.cancel_by_session("coder", "session-one", project_id=None)


async def test_request_cancel_stores_reason_and_surfaces_in_terminal_payload() -> None:
    """A cancel reason survives into the run_cancelled event payload."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await asyncio.sleep(0)
    run.request_cancel(reason="user")
    release.set()

    with pytest.raises(RunCancelledError):
        await run.wait()

    assert run.cancel_reason == "user"
    cancelled_events = [event for event in run.events if event.type == "run_cancelled"]
    assert len(cancelled_events) == 1
    assert cancelled_events[0].payload["reason"] == "user"
    assert_timing_payload(cancelled_events[0].payload)


async def test_request_cancel_omits_reason_from_payload_when_not_provided() -> None:
    """Default cancel (no reason) produces a run_cancelled payload without a 'reason' key."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        execute,
    )
    await asyncio.sleep(0)
    run.request_cancel()
    release.set()

    with pytest.raises(RunCancelledError):
        await run.wait()

    assert run.cancel_reason is None
    cancelled_events = [event for event in run.events if event.type == "run_cancelled"]
    assert len(cancelled_events) == 1
    assert "reason" not in cancelled_events[0].payload
    assert_timing_payload(cancelled_events[0].payload)
