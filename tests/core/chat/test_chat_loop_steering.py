"""Steering preserves Run identity and ordered Model/Tool boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.runs import ActiveRunError
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)


class PausedAdapter(StubAdapter):
    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        if not self.requests:
            self.entered.set()
            await self.release.wait()
        return await super().send(messages, model_id=model_id, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_step", [False, True])
async def test_steer_keeps_same_run_and_follows_complete_tool_batch(
    tmp_path: Path, tool_step: bool
) -> None:
    first = {
        "content": "Before",
        "tool_calls": [
            {"id": "a", "name": "probe", "arguments": {}},
            {"id": "b", "name": "probe", "arguments": {}},
        ]
        if tool_step
        else None,
    }
    adapter = PausedAdapter([first, {"content": "After", "tool_calls": None}])
    tools = ToolRegistry()
    tools.register(
        "probe", "Probe", {"type": "object"}, lambda _ctx, _args: tool_success({"done": True})
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Original", session_id="one")
    await asyncio.wait_for(adapter.entered.wait(), 5)
    ordinary = await loop.queue_run("coder", "Later", session_id="one")
    items = [
        await loop.queue_run("coder", content, session_id="one")
        for content in ["Steer one", "Steer two"]
    ]
    for item in reversed(items):
        runtime.chat_run_manager.steer_queued(
            "coder", "one", item.item_id, project_id=None, run_id=run.id
        )
    assert all(not item.future.done() for item in items)
    assert runtime.chat_run_manager.remove_queued("coder", "one", ordinary.item_id, project_id=None)
    adapter.release.set()
    result = await asyncio.wait_for(run.wait(), 10)
    assert result.content == "After"
    assert run.iteration_count == 2
    assert all(item.future.result() is run for item in items)
    assert runtime.chat_run_manager.list_queued("coder", "one", project_id=None) == []
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    visible = [m for m in history if m.role in {"user", "assistant", "tool"}]
    assert [m.role for m in visible] == (
        ["user", "assistant", "tool", "tool", "user", "user", "assistant"]
        if tool_step
        else ["user", "assistant", "user", "user", "assistant"]
    )
    assert [m.content for m in visible if m.role == "user"] == [
        "Original",
        "Steer one",
        "Steer two",
    ]
    sent = adapter.requests[1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"][-2:] == ["Steer one", "Steer two"]
    assert len([m for m in history if m.role == "run_summary"]) == 1
    assert [
        e.payload.get("queue_item_id") for e in run.events if e.type == "user_message_persisted"
    ] == [None, items[0].item_id, items[1].item_id]


@pytest.mark.asyncio
async def test_rejects_stale_run_and_keeps_input_on_cancel(tmp_path: Path) -> None:
    adapter = PausedAdapter([{"content": "Queued answer", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Original", session_id="one")
    await asyncio.wait_for(adapter.entered.wait(), 5)
    item = await loop.queue_run("coder", "Retained", session_id="one")
    manager = runtime.chat_run_manager
    with pytest.raises(ActiveRunError):
        manager.steer_queued("coder", "one", item.item_id, project_id=None, run_id="stale")
    assert item.steering_run_id is None
    manager.steer_queued("coder", "one", item.item_id, project_id=None, run_id=run.id)
    await manager.cancel(run.id)
    adapter.release.set()
    successor = await asyncio.wait_for(item.future, 5)
    assert successor.id != run.id
    await asyncio.wait_for(successor.wait(), 10)
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    assert [m.content for m in history if m.role == "user"] == ["Original", "Retained"]
