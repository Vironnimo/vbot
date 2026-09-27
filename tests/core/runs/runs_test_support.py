"""Shared addresses, executors and assertions for Run tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from core.runs import Run
from core.sessions import SessionAddress

SESSION = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")


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
