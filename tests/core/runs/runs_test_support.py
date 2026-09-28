"""Shared addresses, executors and assertions for Run tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from core.runs import ChatRunManager, Run, RunEvent
from core.sessions import SessionAddress

SESSION = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")


class RunTimelines:
    """Follow every Run a manager starts from its first event, like a live SSE client.

    A finished Run replays only its settled ending, so a test that asserts the
    complete emitted timeline reads it here instead of from ``Run.events``. The
    follower subscribes on its first event-loop step, so the Run must not end
    within its own first step; Session persistence admission always yields.
    """

    def __init__(self, manager: ChatRunManager) -> None:
        self._followers: dict[str, asyncio.Task[list[RunEvent]]] = {}
        manager.add_run_started_callback(self._follow)

    def _follow(self, run: Run) -> None:
        self._followers[run.id] = asyncio.create_task(_collect(run), name=f"timeline:{run.id}")

    async def events(self, run: Run) -> list[RunEvent]:
        """Return every event *run* emitted, once its terminal event arrived."""
        return await self._followers[run.id]

    async def types(self, run: Run) -> list[str]:
        """Return the emitted event types of *run* in order."""
        return [event.type for event in await self.events(run)]


async def _collect(run: Run) -> list[RunEvent]:
    return [event async for event in run.subscribe()]


def held(result: Any = "done") -> tuple[Callable[[Run], Awaitable[Any]], asyncio.Event]:
    """An executor that waits for the returned release event, then returns *result*."""
    release = asyncio.Event()

    async def execute(_run: Run) -> Any:
        await release.wait()
        return result

    return execute, release


async def finish_immediately(_run: Run) -> str:
    return "done"


def assert_timing_payload(payload: dict[str, Any]) -> None:
    timing = payload.get("timing")
    assert isinstance(timing, dict)
    assert isinstance(timing.get("started_at"), str)
    assert isinstance(timing.get("completed_at"), str)
    assert isinstance(timing.get("duration_ms"), int)
    assert timing["duration_ms"] >= 0
