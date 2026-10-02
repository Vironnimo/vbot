"""Steering preserves Run identity and ordered Model/Tool boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, override

import pytest

from core.providers.errors import NetworkError
from core.runs import PROVIDER_REQUEST_STATUS_EVENT, RunKind, RunStatus
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    PolicyStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)


class SteeringAdapter(StubAdapter):
    """Select one new steering input while each chosen request is in flight."""

    def __init__(self, responses: list[Any], *, steer_requests: range) -> None:
        super().__init__(responses)
        self.steer_requests = steer_requests
        self.loop: Any = None
        self.runtime: Any = None

    @override
    async def send(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        index = len(self.requests)
        if index in self.steer_requests:
            item = await self.loop.queue_run("coder", f"Steer {index}", session_id="one")
            self.runtime.chat_run_manager.steer_queued(
                "coder", "one", item.item_id, project_id=None
            )
        return await super().send(messages, model_id=model_id, **kwargs)


class PausedAdapter(PolicyStubAdapter):
    """Hold the first request until released; reasoning replays only within the Run."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses, policy="current_run")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @override
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
        runtime.chat_run_manager.steer_queued("coder", "one", item.item_id, project_id=None)
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
        e.payload.get("queue_item_id")
        for e in await runtime.timelines.events(run)
        if e.type == "user_message_persisted"
    ] == [None, items[0].item_id, items[1].item_id]


@pytest.mark.asyncio
async def test_steering_reaches_an_internal_system_run(tmp_path: Path) -> None:
    adapter = PausedAdapter(
        [{"content": "Reviewed", "tool_calls": None}, {"content": "Adjusted", "tool_calls": None}]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    # Shaped like the automatic delivery of a finished Sub-Agent's result.
    run = await loop.start_run(
        "coder",
        "Sub-Agent finished",
        session_id="one",
        internal=True,
        input_persisted_hook=lambda: None,
        run_kind=RunKind.SYSTEM,
    )
    await asyncio.wait_for(adapter.entered.wait(), 5)
    item = await loop.queue_run("coder", "Also check the tests", session_id="one")
    runtime.chat_run_manager.steer_queued("coder", "one", item.item_id, project_id=None)
    adapter.release.set()
    result = await asyncio.wait_for(run.wait(), 10)
    assert result.content == "Adjusted"
    assert item.future.result() is run
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    assert [m.content for m in history if m.role == "user"] == ["Also check the tests"]
    sent = adapter.requests[1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"][-1] == "Also check the tests"


@pytest.mark.asyncio
async def test_input_selected_for_a_cancelled_run_starts_next(tmp_path: Path) -> None:
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
    manager.steer_queued("coder", "one", item.item_id, project_id=None)
    await manager.cancel(run.id)
    adapter.release.set()
    successor = await asyncio.wait_for(item.future, 5)
    assert successor.id != run.id
    await asyncio.wait_for(successor.wait(), 10)
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    assert [m.content for m in history if m.role == "user"] == ["Original", "Retained"]


@pytest.mark.asyncio
async def test_each_steered_step_gets_a_fresh_recovery_budget(
    tmp_path: Path, recovery_waits: list[float]
) -> None:
    failures: list[Any] = [NetworkError("temporarily unavailable") for _ in range(8)]
    answers = [{"content": f"Answer {index}", "tool_calls": None} for index in range(10)]
    # The first answer needs the ninth attempt; nine steered answers follow it.
    adapter = SteeringAdapter([*failures, *answers], steer_requests=range(8, 17))
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    loop = build_chat_loop(runtime)
    adapter.loop, adapter.runtime = loop, runtime
    run = await loop.start_run("coder", "Original", session_id="one")
    result = await asyncio.wait_for(run.wait(), 20)
    assert run.status == RunStatus.COMPLETED
    assert result.content == "Answer 9"
    assert len(adapter.requests) == 18
    retries = [
        event.payload["attempt"]
        for event in await runtime.timelines.events(run)
        if event.type == PROVIDER_REQUEST_STATUS_EVENT
        and event.payload.get("state") == "retrying"
        and "attempt" in event.payload
    ]
    # Only the initial step failed; no steered request waits for a backoff.
    assert retries == list(range(2, 10))
    assert len(recovery_waits) == 8
    history = runtime.chat_sessions.get(session_address("coder", "one")).load()
    assert len([m for m in history if m.role == "user"]) == 10


@pytest.mark.asyncio
async def test_withdrawn_steering_input_keeps_the_final_answer(tmp_path: Path) -> None:
    address = session_address("coder", "one")

    class WithdrawingAdapter(StubAdapter):
        @override
        async def send(
            self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
        ) -> dict[str, Any]:
            if not self.requests:
                item = await loop.queue_run("coder", "Withdrawn", session_id="one")
                manager.steer_queued("coder", "one", item.item_id, project_id=None)
                withdrawals.append(asyncio.create_task(withdraw(item.item_id)))
            return await super().send(messages, model_id=model_id, **kwargs)

    async def withdraw(item_id: str) -> None:
        # Wait on the Session lock while the final answer is persisted, so the
        # removal lands between the pending-steering check and its delivery.
        lock = runtime.chat_sessions.write_lock(address)
        while not lock._lock.locked():
            await asyncio.sleep(0)
        async with lock:
            assert manager.remove_queued("coder", "one", item_id, project_id=None)

    withdrawals: list[asyncio.Task[None]] = []
    adapter = WithdrawingAdapter([{"content": "Final", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/gpt-5.2"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="one")
    manager = runtime.chat_run_manager
    loop = build_chat_loop(runtime)
    run = await loop.start_run("coder", "Original", session_id="one")
    result = await asyncio.wait_for(run.wait(), 10)
    await asyncio.gather(*withdrawals)
    assert run.status == RunStatus.COMPLETED
    assert result.content == "Final"
    assert len(adapter.requests) == 1
    history = runtime.chat_sessions.get(address).load()
    assert [m.content for m in history if m.role == "user"] == ["Original"]


@pytest.mark.asyncio
@pytest.mark.parametrize("steer", [False, True])
async def test_steering_keeps_current_run_reasoning(tmp_path: Path, steer: bool) -> None:
    first = {
        "content": "Before",
        "reasoning": "Plan the probe",
        "reasoning_meta": {"signature": "sig-1"},
        "tool_calls": [{"id": "a", "name": "probe", "arguments": {}}],
        "terminal_outcome": "tool_calls",
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
    if steer:
        item = await loop.queue_run("coder", "Steer", session_id="one")
        runtime.chat_run_manager.steer_queued("coder", "one", item.item_id, project_id=None)
    adapter.release.set()
    await asyncio.wait_for(run.wait(), 10)
    sent = adapter.requests[1]["messages"]
    tool_turn = next(m for m in sent if m["role"] == "assistant" and m.get("tool_calls"))
    assert tool_turn["reasoning"] == "Plan the probe"
    assert tool_turn["reasoning_meta"] == {"signature": "sig-1"}
    assert [m["content"] for m in sent if m["role"] == "user"][-1] == (
        "Steer" if steer else "Original"
    )
