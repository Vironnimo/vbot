"""Tests for chat run coordination primitives."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.chat import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunCancelledError,
    RunNotFoundError,
    RunStatus,
)
from core.chat.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
)

pytestmark = pytest.mark.asyncio


async def test_replays_events_to_late_subscriber() -> None:
    manager = ChatRunManager()

    async def execute(run: Run) -> str:
        run.emit("visible", {"content": "hello"})
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    assert await run.wait() == "done"

    events = [event async for event in run.subscribe()]

    assert [event.type for event in events] == ["run_started", "visible", "run_completed"]
    assert events[1].payload == {"content": "hello"}


async def test_rejects_second_active_run_for_same_session() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        return run.id

    first_run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)

    with pytest.raises(ActiveRunError, match="active run"):
        await manager.start(agent_id="coder", session_id="session-one", executor=execute)

    release.set()
    assert await first_run.wait() == first_run.id


async def test_allows_parallel_runs_for_different_sessions() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()
    started: list[str] = []

    async def execute(run: Run) -> str:
        started.append(run.session_id)
        await release.wait()
        return run.session_id

    first_run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    second_run = await manager.start(agent_id="coder", session_id="session-two", executor=execute)
    await asyncio.sleep(0)

    release.set()

    assert set(started) == {"session-one", "session-two"}
    assert await first_run.wait() == "session-one"
    assert await second_run.wait() == "session-two"


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

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
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

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await asyncio.sleep(0)
    await manager.cancel(run.id)
    release.set()

    assert callbacks == ["aborted"]
    assert run.status == RunStatus.CANCELLED


async def test_cancel_by_session_requests_cancel_and_returns_run() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await started.wait()

    cancelled_run = manager.cancel_by_session("coder", "session-one")

    assert cancelled_run is run
    assert run.cancel_requested is True

    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()


async def test_cancel_by_session_without_active_run_raises_not_found() -> None:
    manager = ChatRunManager()

    with pytest.raises(RunNotFoundError, match="no active run"):
        manager.cancel_by_session("coder", "session-one")


async def test_failed_run_releases_session_lock() -> None:
    manager = ChatRunManager()

    async def fail(_run: Run) -> Any:
        raise RuntimeError("boom")

    async def succeed(_run: Run) -> str:
        return "ok"

    failed_run = await manager.start(agent_id="coder", session_id="session-one", executor=fail)
    with pytest.raises(RuntimeError, match="boom"):
        await failed_run.wait()

    next_run = await manager.start(agent_id="coder", session_id="session-one", executor=succeed)

    assert await next_run.wait() == "ok"


async def test_mark_completed_includes_payload_extras_when_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_completed(
        "result",
        payload_extras={"usage": {"input_tokens": 150, "output_tokens": 12}},
    )

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {
        "status": "completed",
        "usage": {"input_tokens": 150, "output_tokens": 12},
    }


async def test_mark_completed_omits_payload_extras_when_not_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_completed("result")

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {"status": "completed"}


async def test_mark_completed_omits_payload_extras_when_empty() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_completed("result", payload_extras=None)

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {"status": "completed"}


async def test_mark_failed_includes_payload_extras_when_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_failed(RuntimeError("oops"), payload_extras={"detail": "extra"})

    failed_events = [event for event in run.events if event.type == "run_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].payload == {
        "status": "failed",
        "error": "oops",
        "detail": "extra",
    }


async def test_mark_failed_omits_payload_extras_when_not_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_failed(RuntimeError("oops"))

    failed_events = [event for event in run.events if event.type == "run_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].payload == {"status": "failed", "error": "oops"}


async def test_run_completed_includes_usage_from_result_object() -> None:
    """Usage attribute on executor result appears in run_completed payload."""

    class FakeResult:
        usage = {"input_tokens": 200, "output_tokens": 30}

    manager = ChatRunManager()

    async def execute(run: Run) -> FakeResult:
        return FakeResult()

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await run.wait()

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {
        "status": "completed",
        "usage": {"input_tokens": 200, "output_tokens": 30},
    }


async def test_run_completed_omits_usage_when_result_has_no_usage() -> None:
    """When the executor returns a plain string, no usage key appears in run_completed."""
    manager = ChatRunManager()

    async def execute(run: Run) -> str:
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await run.wait()

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {"status": "completed"}


async def test_run_completed_omits_usage_when_usage_is_none() -> None:
    """When the result has usage=None, no usage key appears in run_completed."""

    class ResultWithNoneUsage:
        usage = None

    manager = ChatRunManager()

    async def execute(run: Run) -> ResultWithNoneUsage:
        return ResultWithNoneUsage()

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await run.wait()

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {"status": "completed"}
