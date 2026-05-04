"""Tests for chat run coordination primitives."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.chat import ActiveRunError, ChatRunManager, Run, RunCancelledError, RunStatus

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
