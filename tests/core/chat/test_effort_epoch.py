"""A thinking effort change keeps the System Prompt and reaches the next Run.

The System Prompt shows the effort its prompt epoch pinned. A change between
Runs applies from the next Run's first request; an active Run keeps its effort.
A route that switches the effort mid-conversation gets the change at its place
in the history and keeps the epoch's effort as the request effort.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, override

import pytest

from core.providers.effort_updates import EFFORT_BASE_KWARG, effort_update_message
from core.sessions import is_effort_change_note
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_adapter_support import StubAdapter
from tests.core.chat.chat_loop_support import (
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)

JsonObject = dict[str, Any]
_SESSION = session_address("coder", "session-one")


class _UpdatingAdapter(StubAdapter):
    """Answers each request; can switch the effort mid-conversation."""

    def __init__(self, responses: list[Any], *, effort_updates: bool = False) -> None:
        super().__init__(responses)
        self._effort_updates = effort_updates
        self.before_request: Any = None

    @override
    def effort_updates(self, model_id: str) -> bool:
        del model_id
        return self._effort_updates

    @override
    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        response = await super().send(messages, model_id=model_id, **kwargs)
        if self.before_request is not None:
            self.before_request(len(self.requests))
        return response


def _runtime(tmp_path: Path, adapter: StubAdapter, *, effort: str = "low") -> Any:
    tools = ToolRegistry()

    async def work(_context: Any, _arguments: JsonObject) -> JsonObject:
        return tool_success({"done": True})

    tools.register("work", "Work stub.", {"type": "object"}, work)
    agent = StubAgent(
        id="coder", model="openai/gpt-5.2", thinking_effort=effort, allowed_tools=["*"]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="session-one")
    return runtime


def _set_effort(runtime: Any, effort: str) -> None:
    runtime.agent_resolver.update_session_overrides(_SESSION, {"thinking_effort": effort})


def _wire(messages: list[JsonObject]) -> list[JsonObject]:
    """The request messages as a Provider renders them (without local ids and times)."""

    return [
        {key: value for key, value in message.items() if key not in ("id", "timestamp")}
        for message in messages
    ]


def _answer(text: str) -> JsonObject:
    return {"content": text, "tool_calls": None}


_WORK = {"content": None, "tool_calls": [{"id": "call_1", "name": "work", "arguments": {}}]}


@pytest.mark.asyncio
async def test_a_change_between_runs_reaches_the_next_run_and_keeps_the_system_prompt(
    tmp_path: Path,
) -> None:
    adapter = _UpdatingAdapter([_answer("one"), _answer("two")])
    runtime = _runtime(tmp_path, adapter)
    loop = build_chat_loop(runtime)

    await loop.send("coder", "First", session_id="session-one")
    _set_effort(runtime, "high")
    await loop.send("coder", "Second", session_id="session-one")

    assert [request["kwargs"]["thinking_effort"] for request in adapter.requests] == [
        "low",
        "high",
    ]
    assert {build["thinking_effort"] for build in runtime.system_prompts.build_pin_calls} == {"low"}
    first, second = (_wire(request["messages"]) for request in adapter.requests)
    assert second[: len(first)] == first
    assert all(
        message.get("role") != "effort_update"
        for request in adapter.requests
        for message in request["messages"]
    )


@pytest.mark.parametrize(
    ("changed", "in_run", "next_run"),
    [
        pytest.param("high", "low", "high", id="level-waits-for-the-next-run"),
        pytest.param("none", "low", "none", id="reasoning-off-waits-for-the-next-run"),
    ],
)
@pytest.mark.asyncio
async def test_a_change_during_a_run_waits_for_the_next_run(
    tmp_path: Path, changed: str, in_run: str, next_run: str
) -> None:
    adapter = _UpdatingAdapter([_WORK, _answer("done"), _answer("next")])
    runtime = _runtime(tmp_path, adapter)
    adapter.before_request = lambda count: _set_effort(runtime, changed) if count == 1 else None
    loop = build_chat_loop(runtime)

    await loop.send("coder", "Work", session_id="session-one")
    await loop.send("coder", "Again", session_id="session-one")

    assert [request["kwargs"]["thinking_effort"] for request in adapter.requests] == [
        "low",
        in_run,
        next_run,
    ]
    history = runtime.chat_sessions.get(_SESSION).load()
    assert sum(is_effort_change_note(message) for message in history) == 1


@pytest.mark.asyncio
async def test_a_route_with_updates_gets_the_change_in_place_and_keeps_the_prefix(
    tmp_path: Path,
) -> None:
    adapter = _UpdatingAdapter(
        [_answer("one"), _answer("two"), _answer("three")], effort_updates=True
    )
    runtime = _runtime(tmp_path, adapter)
    loop = build_chat_loop(runtime)

    await loop.send("coder", "First", session_id="session-one")
    _set_effort(runtime, "high")
    await loop.send("coder", "Second", session_id="session-one")
    await loop.send("coder", "Third", session_id="session-one")

    kwargs = [request["kwargs"] for request in adapter.requests]
    assert [(item["thinking_effort"], item.get(EFFORT_BASE_KWARG)) for item in kwargs] == [
        ("low", None),
        ("high", "low"),
        ("high", "low"),
    ]
    first, second, third = (_wire(request["messages"]) for request in adapter.requests)
    assert second[: len(first)] == first
    assert second[-1] == effort_update_message("high")
    assert third[: len(second)] == second
