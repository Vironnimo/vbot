"""Tests for chat run coordination primitives."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatLoop, ChatSessionManager
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
    ActiveRunError,
    ChatRunManager,
    Run,
    RunCancelledError,
    RunNotFoundError,
    RunStatus,
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
    runtime = SimpleNamespace(
        agents=SimpleNamespace(
            get=lambda agent_id: SimpleNamespace(id=agent_id, model="openai/gpt-5.2")
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
