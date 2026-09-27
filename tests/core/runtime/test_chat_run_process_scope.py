"""A cancelled chat Run cancels its host process scope before releasing it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.runs import RunCancelledError
from tests.core.chat.chat_loop_support import StubAgent, StubRuntime, build_chat_loop


class _BlockingAdapter:
    def __init__(self) -> None:
        self.request_started = asyncio.Event()

    async def send(self, _messages: object, **_kwargs: object) -> dict[str, object]:
        self.request_started.set()
        await asyncio.Event().wait()
        return {"content": "unreachable", "tool_calls": None}

    def normalize_response(
        self, response: dict[str, object], *, model_id: str | None = None
    ) -> dict[str, object]:
        return response

    async def aclose(self) -> None:
        return None


class _RecordingProcessManager:
    def __init__(self) -> None:
        self.scope_events: list[tuple[str, str]] = []

    async def cancel_scope_async(self, scope_key: str) -> None:
        self.scope_events.append(("cancel", scope_key))

    def release_scope(self, scope_key: str) -> None:
        self.scope_events.append(("release", scope_key))


@pytest.mark.asyncio
async def test_chat_run_cancellation_cancels_then_releases_the_process_scope(
    tmp_path: Path,
) -> None:
    adapter = _BlockingAdapter()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="agent-one", model="provider/model"),
        adapter=adapter,  # type: ignore[arg-type]
    )
    process_manager = _RecordingProcessManager()
    runtime.process_manager = process_manager
    runtime.chat_sessions.create("agent-one", session_id="session-one")
    chat_loop = build_chat_loop(runtime)

    run = await chat_loop.start_run("agent-one", "hello", session_id="session-one")
    await adapter.request_started.wait()
    run.request_cancel()

    with pytest.raises(RunCancelledError):
        await run.wait()

    # The settled Run releases its closed scope only after cancellation cleanup.
    assert process_manager.scope_events == [("cancel", run.id), ("release", run.id)]
