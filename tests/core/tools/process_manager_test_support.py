"""Shared fixtures and helpers for process Tool and ProcessManager tests."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio

from core.tools.process_manager import ProcessManager

PollResult = dict[str, object]

AGENT_A = "agent-a"
AGENT_B = "agent-b"
SCOPE_A = "run-a"
SLEEP = "import time; time.sleep(30)"


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[ProcessManager]:
    manager = ProcessManager(sweep_interval_seconds=3600)
    try:
        yield manager
    finally:
        await manager.aclose()


async def spawn(
    manager: ProcessManager,
    script: str = SLEEP,
    *,
    agent_id: str = AGENT_A,
    scope_key: str = SCOPE_A,
    **options: Any,
) -> str:
    """Start ``python -c script`` the way the shell Tool starts a command."""
    options.setdefault("env", None)
    options.setdefault("cwd", None)
    return await manager.spawn(scope_key, agent_id, [sys.executable, "-c", script], **options)


async def finish(
    manager: ProcessManager, process_id: str, *, agent_id: str = AGENT_A
) -> PollResult:
    """Wait until the process has settled, then collect its unread output as Bash polls it."""
    outcome, _line = await manager.wait(process_id, agent_id, timeout_seconds=10)
    assert outcome == "exited"
    return await manager.poll(process_id, agent_id)


def stream_text(poll_result: PollResult, stream: str) -> str:
    """Join one pipe's text from a poll result's stream-tagged chunks."""
    chunks = poll_result["chunks"]
    assert isinstance(chunks, list)
    return "".join(chunk["data"] for chunk in chunks if chunk["stream"] == stream)
