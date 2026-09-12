"""Shared fixtures and fakes for process manager behavior tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from core.tools.process_manager import (
    ProcessManager,
)

PollResult = dict[str, object]

AGENT_A = "agent-a"

SCOPE_A = "run-a"


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[ProcessManager]:
    manager = ProcessManager(sweep_interval_seconds=3600)
    try:
        yield manager
    finally:
        await manager.aclose()


async def poll_until_terminal(
    manager: ProcessManager,
    process_id: str,
    *,
    agent_id: str = AGENT_A,
) -> PollResult:
    combined_result: PollResult = {}
    stdout = ""
    stderr = ""
    output = ""
    for _ in range(20):
        result = await manager.poll(process_id, agent_id, timeout_ms=500)
        stdout += as_text(result["stdout"])
        stderr += as_text(result["stderr"])
        output += as_text(result["output"])
        combined_result = dict(result)
        combined_result["stdout"] = stdout
        combined_result["stderr"] = stderr
        combined_result["output"] = output
        if result["status"] != "running":
            return combined_result

    return combined_result


def as_text(value: object) -> str:
    assert isinstance(value, str)
    return value
