"""Tests for chat run coordination primitives."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatLoop, ChatSessionManager
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    RUN_STARTED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    ActiveRunError,
    ChatRunManager,
    Run,
    RunCancelledError,
    RunNotFoundError,
    RunStatus,
)
from core.utils.errors import VBotError

pytestmark = pytest.mark.asyncio


def assert_timing_payload(payload: dict[str, Any]) -> None:
    timing = payload.get("timing")
    assert isinstance(timing, dict)
    assert isinstance(timing.get("started_at"), str)
    assert isinstance(timing.get("completed_at"), str)
    assert isinstance(timing.get("duration_ms"), int)
    assert timing["duration_ms"] >= 0


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
    assert_timing_payload(run.events[-1].payload)


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
    assert "Run cancel callback failed" in caplog.text


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

    assert "Run async cancel callback failed" in caplog.text


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


async def test_enqueue_when_session_is_idle_starts_run_immediately() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        started.set()
        await release.wait()
        return "done"

    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
        display_content="Queued hello",
    )
    run = await item.future

    assert run.status == RunStatus.RUNNING
    assert manager.active_run(agent_id="coder", session_id="session-one") is run
    assert manager.list_queued("coder", "session-one") == []
    assert item.to_dict()["content"] == "Queued hello"

    await started.wait()
    release.set()

    assert await run.wait() == "done"


async def test_enqueue_when_session_is_busy_queues_and_drains_after_completion() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_started = asyncio.Event()
    queued_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        queued_started.set()
        await queued_release.wait()
        return "queued"

    active_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_execute,
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="Queued next",
    )

    assert item.future.done() is False
    assert [queued_item.item_id for queued_item in manager.list_queued("coder", "session-one")] == [
        item.item_id
    ]

    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)

    assert queued_run.status == RunStatus.RUNNING
    assert manager.list_queued("coder", "session-one") == []

    await queued_started.wait()
    queued_release.set()

    assert await queued_run.wait() == "queued"


async def test_has_activity_for_agent_reports_active_and_queued_work() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_execute,
    )
    queued_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="Queued next",
    )

    assert manager.has_activity_for_agent("coder") is True
    assert manager.has_activity_for_agent("writer") is False

    active_release.set()
    assert await active_run.wait() == "active"
    queued_run = await queued_item.future
    assert manager.has_activity_for_agent("coder") is True
    assert await queued_run.wait() == "queued"

    assert manager.has_activity_for_agent("coder") is False


async def test_multiple_enqueued_items_drain_in_fifo_order() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    started: list[str] = []
    started_events = {
        "first": asyncio.Event(),
        "second": asyncio.Event(),
        "third": asyncio.Event(),
    }
    queued_releases = {
        "first": asyncio.Event(),
        "second": asyncio.Event(),
        "third": asyncio.Event(),
    }

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    def make_executor(label: str) -> Any:
        async def execute(_run: Run) -> str:
            started.append(label)
            started_events[label].set()
            await queued_releases[label].wait()
            return label

        return execute

    active_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_execute,
    )
    first_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=make_executor("first"),
        display_content="first",
    )
    second_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=make_executor("second"),
        display_content="second",
    )
    third_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=make_executor("third"),
        display_content="third",
    )

    assert [item.display_content for item in manager.list_queued("coder", "session-one")] == [
        "first",
        "second",
        "third",
    ]

    active_release.set()
    assert await active_run.wait() == "active"

    first_run = await asyncio.wait_for(first_item.future, timeout=1)
    await started_events["first"].wait()
    assert started == ["first"]
    queued_releases["first"].set()
    assert await first_run.wait() == "first"

    second_run = await asyncio.wait_for(second_item.future, timeout=1)
    await started_events["second"].wait()
    assert started == ["first", "second"]
    queued_releases["second"].set()
    assert await second_run.wait() == "second"

    third_run = await asyncio.wait_for(third_item.future, timeout=1)
    await started_events["third"].wait()
    assert started == ["first", "second", "third"]
    queued_releases["third"].set()
    assert await third_run.wait() == "third"


async def test_remove_queued_item_cancels_future_and_removes_from_queue() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_execute,
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="remove me",
    )

    assert manager.remove_queued("coder", "session-one", item.item_id) is True
    assert manager.list_queued("coder", "session-one") == []
    assert item.future.cancelled() is True
    assert manager.remove_queued("coder", "session-one", item.item_id) is False

    active_release.set()
    assert await active_run.wait() == "active"
    assert manager.active_run(agent_id="coder", session_id="session-one") is None


async def test_update_queued_item_replaces_executor_and_display_content() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    updated_started = asyncio.Event()
    queued_release = asyncio.Event()
    executed: list[str] = []

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def original_execute(_run: Run) -> str:
        executed.append("original")
        return "original"

    async def updated_execute(_run: Run) -> str:
        executed.append("updated")
        updated_started.set()
        await queued_release.wait()
        return "updated"

    active_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_execute,
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=original_execute,
        display_content="original",
    )

    assert (
        manager.update_queued(
            "coder",
            "session-one",
            item.item_id,
            updated_execute,
            "updated",
        )
        is True
    )
    assert manager.list_queued("coder", "session-one")[0].display_content == "updated"
    assert (
        manager.update_queued(
            "coder",
            "session-one",
            "missing",
            updated_execute,
            "updated",
        )
        is False
    )

    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)
    await updated_started.wait()
    assert executed == ["updated"]
    queued_release.set()
    assert await queued_run.wait() == "updated"


async def test_enqueue_race_condition_session_becomes_idle_between_error_and_enqueue() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        await queued_release.wait()
        return "queued"

    active_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_execute,
    )

    with pytest.raises(ActiveRunError, match="active run"):
        await manager.start(
            agent_id="coder",
            session_id="session-one",
            executor=queued_execute,
        )

    active_release.set()
    assert await active_run.wait() == "active"

    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="race",
    )
    queued_run = await item.future

    assert queued_run.status == RunStatus.RUNNING
    assert manager.list_queued("coder", "session-one") == []

    queued_release.set()
    assert await queued_run.wait() == "queued"


async def test_chat_loop_queue_run_uses_display_preview_for_busy_session(tmp_path: Path) -> None:
    session_id = "session-one"
    active_release = asyncio.Event()
    agents = SimpleNamespace(
        get=lambda agent_id: SimpleNamespace(id=agent_id, model="openai/gpt-5.2")
    )
    runtime = SimpleNamespace(
        agents=agents,
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda _project_id, agent_id: agents.get(agent_id)
        ),
        providers=SimpleNamespace(
            get=lambda provider_id: SimpleNamespace(connections=[SimpleNamespace(id="api-key")])
        ),
        provider_credentials=SimpleNamespace(
            has_credentials=lambda _provider_id, connection_id=None: (
                connection_id == "openai:api-key"
            )
        ),
        chat_sessions=ChatSessionManager(tmp_path),
        chat_runs=ChatRunManager(),
    )
    runtime.chat_run_manager = runtime.chat_runs
    runtime.chat_sessions.create("coder", session_id=session_id)

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    active_run = await runtime.chat_runs.start(
        agent_id="coder",
        session_id=session_id,
        executor=active_execute,
    )

    item = await ChatLoop(runtime).queue_run(
        "coder",
        "x" * 600,
        session_id=session_id,
    )

    assert item.display_content == "x" * 500
    assert runtime.chat_runs.list_queued("coder", session_id)[0] is item

    assert runtime.chat_runs.remove_queued("coder", session_id, item.item_id) is True
    active_release.set()
    assert await active_run.wait() == "active"


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


async def test_mark_failed_logs_unexpected_error_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-VBotError failure logs at ERROR with the exception attached."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    caplog.set_level(logging.ERROR, logger="vbot.runs")
    run.mark_failed(RuntimeError("kaboom"))

    error_records = [
        record
        for record in caplog.records
        if record.name == "vbot.runs" and record.levelno == logging.ERROR
    ]
    assert len(error_records) == 1
    record = error_records[0]
    assert "failed unexpectedly" in record.getMessage()
    assert run.id in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


async def test_mark_failed_logs_vbot_error_as_warning_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An expected VBotError failure logs at WARNING with no traceback."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    caplog.set_level(logging.WARNING, logger="vbot.runs")
    run.mark_failed(VBotError("expected boom"))

    warning_records = [
        record
        for record in caplog.records
        if record.name == "vbot.runs" and record.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    record = warning_records[0]
    assert "Run %s failed" not in record.getMessage()
    assert "expected boom" in record.getMessage()
    assert record.exc_info is None
    # No ERROR-level record for an expected failure.
    assert not [
        rec for rec in caplog.records if rec.name == "vbot.runs" and rec.levelno == logging.ERROR
    ]


async def test_failed_run_logs_once_through_manager_executor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An executor that raises reaches mark_failed exactly once and logs once."""
    manager = ChatRunManager()

    async def fail(_run: Run) -> Any:
        raise RuntimeError("executor boom")

    caplog.set_level(logging.ERROR, logger="vbot.runs")
    run = await manager.start(agent_id="coder", session_id="session-one", executor=fail)
    with pytest.raises(RuntimeError, match="executor boom"):
        await run.wait()

    error_records = [
        record
        for record in caplog.records
        if record.name == "vbot.runs"
        and record.levelno == logging.ERROR
        and "failed unexpectedly" in record.getMessage()
    ]
    assert len(error_records) == 1


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
    assert_timing_payload(completed_events[0].payload)
    assert completed_events[0].payload["status"] == "completed"
    assert completed_events[0].payload["usage"] == {
        "input_tokens": 200,
        "output_tokens": 30,
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
    assert_timing_payload(completed_events[0].payload)
    assert completed_events[0].payload["status"] == "completed"
    assert "usage" not in completed_events[0].payload


async def test_run_started_callbacks_are_notified_and_removable() -> None:
    manager = ChatRunManager()
    observed_runs: list[Run] = []

    async def execute(_run: Run) -> str:
        return "done"

    remove_callback = manager.add_run_started_callback(observed_runs.append)
    first_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
    )
    await first_run.wait()
    remove_callback()

    second_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
    )
    await second_run.wait()

    assert observed_runs == [first_run]


async def test_completed_run_lookup_retention_is_bounded() -> None:
    manager = ChatRunManager(completed_run_retention_limit=2)

    async def execute(run: Run) -> str:
        return run.id

    first_run = await manager.start(agent_id="coder", session_id="one", executor=execute)
    await first_run.wait()
    second_run = await manager.start(agent_id="coder", session_id="two", executor=execute)
    await second_run.wait()
    third_run = await manager.start(agent_id="coder", session_id="three", executor=execute)
    await third_run.wait()

    with pytest.raises(RunNotFoundError):
        manager.get(first_run.id)
    assert manager.get(second_run.id) is second_run
    assert manager.get(third_run.id) is third_run


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
    assert_timing_payload(completed_events[0].payload)
    assert completed_events[0].payload["status"] == "completed"
    assert "usage" not in completed_events[0].payload


async def test_request_cancel_stores_reason_and_surfaces_in_terminal_payload() -> None:
    """A cancel reason survives into the run_cancelled event payload."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
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

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
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


async def test_cancel_tool_call_fires_callback_and_flips_state_without_cancelling_run() -> None:
    """cancel_tool_call must fire the callback, mark cancelled, and leave the run alive."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    def abort() -> None:
        invocations.append("aborted")

    run.register_tool_cancel("tool-1", abort)

    cancelled = run.cancel_tool_call("tool-1")

    assert cancelled is True
    assert invocations == ["aborted"]
    assert run.tool_call_cancelled("tool-1") is True
    assert run.cancel_requested is False
    assert run.status == RunStatus.RUNNING


async def test_cancel_tool_call_with_unknown_id_returns_false() -> None:
    """cancel_tool_call must be a no-op for an id that was never registered."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    def abort() -> None:
        invocations.append("aborted")

    run.register_tool_cancel("tool-1", abort)

    cancelled = run.cancel_tool_call("tool-missing")

    assert cancelled is False
    assert invocations == []
    assert run.tool_call_cancelled("tool-missing") is False
    assert run.tool_call_cancelled("tool-1") is False
    assert run.cancel_requested is False


async def test_cancel_tool_call_after_clear_returns_false() -> None:
    """clear_tool_cancel drops the entry; subsequent cancel_tool_call returns False."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    def abort() -> None:
        invocations.append("aborted")

    run.register_tool_cancel("tool-1", abort)
    run.clear_tool_cancel("tool-1")

    cancelled = run.cancel_tool_call("tool-1")

    assert cancelled is False
    assert invocations == []
    assert run.tool_call_cancelled("tool-1") is False
    assert run.cancel_requested is False


async def test_clear_tool_cancel_resets_cancelled_state() -> None:
    """clear_tool_cancel removes a cancelled entry so tool_call_cancelled returns False."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    def abort() -> None:
        return None

    run.register_tool_cancel("tool-1", abort)
    assert run.cancel_tool_call("tool-1") is True
    assert run.tool_call_cancelled("tool-1") is True

    run.clear_tool_cancel("tool-1")

    assert run.tool_call_cancelled("tool-1") is False
    assert run.cancel_tool_call("tool-1") is False


async def test_tool_call_cancel_does_not_invoke_run_cancel_callbacks_or_cancel_task() -> None:
    """Per-tool-call cancel must not touch run-level cancel callbacks or the executor task."""
    manager = ChatRunManager()
    release = asyncio.Event()
    run_callback_invocations: list[str] = []
    tool_invocations: list[str] = []

    async def execute(run: Run) -> str:
        run.add_cancel_callback(lambda: run_callback_invocations.append("run-cancel"))
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await asyncio.sleep(0)
    run.register_tool_cancel("tool-1", lambda: tool_invocations.append("tool-abort"))

    cancelled = run.cancel_tool_call("tool-1")
    assert cancelled is True
    assert tool_invocations == ["tool-abort"]
    assert run_callback_invocations == []
    assert run.cancel_requested is False
    assert run._task is not None and not run._task.done()  # noqa: SLF001 - task must stay alive.

    release.set()
    assert await run.wait() == "done"
    assert run.status == RunStatus.COMPLETED


async def test_tool_call_cancel_supports_async_callback() -> None:
    """Per-tool-call cancel can dispatch async callbacks via the existing scheduler."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    invocations: list[str] = []

    async def abort() -> None:
        invocations.append("async-abort")

    run.register_tool_cancel("tool-1", abort)

    assert run.cancel_tool_call("tool-1") is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert invocations == ["async-abort"]


async def test_tool_call_cancel_isolates_state_between_tool_call_ids() -> None:
    """Cancelling one tool call must not flip tool_call_cancelled for a different id."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    def abort() -> None:
        return None

    run.register_tool_cancel("tool-1", abort)
    run.register_tool_cancel("tool-2", abort)

    assert run.cancel_tool_call("tool-1") is True

    assert run.tool_call_cancelled("tool-1") is True
    assert run.tool_call_cancelled("tool-2") is False
    assert run.cancel_tool_call("tool-2") is True
    assert run.tool_call_cancelled("tool-2") is True


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
        agent_id="coder",
        session_id="session-one",
        executor=running_execute,
    )
    await started.wait()

    finishing_run = await manager.start(
        agent_id="coder",
        session_id="session-two",
        executor=finishing_execute,
    )
    assert await finishing_run.wait() == "done"
    assert finishing_run.status == RunStatus.COMPLETED

    active = manager.active_runs()

    assert active == [running_run]
    assert all(run.status == RunStatus.RUNNING for run in active)
    assert manager.active_run(agent_id="coder", session_id="session-two") is None

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
            agent_id="coder",
            session_id=session_id,
            executor=execute,
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


async def test_drained_queued_run_started_payload_contains_queue_item_id() -> None:
    """A queued item that gets drained carries its item id on the run_started payload."""
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        await queued_release.wait()
        return "queued"

    active_run = await manager.start(
        agent_id="coder",
        session_id="session-one",
        executor=active_execute,
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="Queued next",
    )

    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)
    await asyncio.sleep(0)

    started_events = [event for event in queued_run.events if event.type == RUN_STARTED_EVENT]
    assert len(started_events) == 1
    assert started_events[0].payload == {
        "status": RunStatus.RUNNING.value,
        "queue_item_id": item.item_id,
    }

    queued_release.set()
    assert await queued_run.wait() == "queued"


async def test_enqueue_idle_session_start_immediately_carries_queue_item_id() -> None:
    """enqueue on an idle session still tags run_started with the queued item id."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
        display_content="Hello",
    )
    run = await item.future
    await asyncio.sleep(0)

    started_events = [event for event in run.events if event.type == RUN_STARTED_EVENT]
    assert len(started_events) == 1
    assert started_events[0].payload == {
        "status": RunStatus.RUNNING.value,
        "queue_item_id": item.item_id,
    }

    release.set()
    assert await run.wait() == "done"


async def test_start_run_payload_omits_queue_item_id() -> None:
    """A plain start() call produces a run_started payload without queue_item_id."""
    manager = ChatRunManager()

    async def execute(_run: Run) -> str:
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    assert await run.wait() == "done"

    started_events = [event for event in run.events if event.type == RUN_STARTED_EVENT]
    assert len(started_events) == 1
    assert started_events[0].payload == {"status": RunStatus.RUNNING.value}
    assert "queue_item_id" not in started_events[0].payload


async def test_project_id_rides_run_not_key() -> None:
    """project_id is carried on the Run for session I/O, never in the dedup key.

    A run started with a project_id is found by the project-agnostic lookups
    (active_run without project_id), and a second start on the same session is
    rejected as already active regardless of whether the caller knows the
    project. This is the exact contract the server RPCs / commands rely on.
    """
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        return run.id

    project_run = await manager.start(
        agent_id="coder",
        session_id="sess-uuid",
        executor=execute,
        project_id="acme",
    )
    await asyncio.sleep(0)

    # The project anchor rides the Run, available to its session I/O.
    assert project_run.project_id == "acme"

    # The project-scoped run is found by the project-agnostic lookup the server
    # RPCs and slash commands use (no project_id passed).
    assert manager.active_run(agent_id="coder", session_id="sess-uuid") is project_run

    # A second start on the same (agent, session) is rejected as active even
    # though no project_id is supplied — the key is project-agnostic.
    with pytest.raises(ActiveRunError, match="active run"):
        await manager.start(agent_id="coder", session_id="sess-uuid", executor=execute)

    release.set()
    assert await project_run.wait() == project_run.id


async def test_identity_run_leaves_project_id_none() -> None:
    """A run started without a project_id keeps run.project_id None (identity path)."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        return run.id

    run = await manager.start(agent_id="coder", session_id="sess-uuid", executor=execute)
    await asyncio.sleep(0)

    assert run.project_id is None
    assert manager.active_run(agent_id="coder", session_id="sess-uuid") is run

    release.set()
    await run.wait()


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
        agent_id="coder",
        session_id="sess-uuid",
        executor=active_execute,
        project_id="acme",
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="sess-uuid",
        executor=queued_execute,
        display_content="queued",
        project_id="acme",
    )

    # The session is busy, so the item is queued (found by the 2-arg lookup).
    assert item.future.done() is False
    assert [queued.item_id for queued in manager.list_queued("coder", "sess-uuid")] == [
        item.item_id
    ]

    active_release.set()
    assert await active_run.wait() == "active"
    queued_run = await asyncio.wait_for(item.future, timeout=1)
    assert await queued_run.wait() == "queued"

    # The drained run carries the enqueue project anchor on the Run itself.
    assert queued_run.project_id == "acme"
    assert drained and drained[0].project_id == "acme"


async def test_cancel_by_session_finds_a_project_scoped_run() -> None:
    """cancel_by_session (no project_id) cancels a project-scoped active run."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    project_run = await manager.start(
        agent_id="coder", session_id="sess-uuid", executor=execute, project_id="acme"
    )
    await asyncio.sleep(0)

    cancelled = manager.cancel_by_session("coder", "sess-uuid")
    assert cancelled is project_run
    assert project_run.cancel_requested is True

    release.set()
    with pytest.raises(RunCancelledError):
        await project_run.wait()


async def test_has_activity_for_agent_matches_a_project_scoped_run() -> None:
    """An agent busy only in a project-scoped run still reports activity."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await manager.start(
        agent_id="coder", session_id="sess-uuid", executor=execute, project_id="acme"
    )

    assert manager.has_activity_for_agent("coder") is True
    assert manager.has_activity_for_agent("writer") is False

    release.set()
    await run.wait()
    assert manager.has_activity_for_agent("coder") is False
