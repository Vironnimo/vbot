"""Tool Calls start while the Model still streams, and a later stream break keeps them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, override

import pytest

from core.chat._step_outcomes import TOOL_CALLS_STREAM_RECOVERY_NOTE
from core.providers.errors import NetworkError
from core.runs import TOOL_CALL_STARTED_EVENT
from core.tools import ToolContext, ToolRegistry, tool_success
from tests.core.chat.chat_loop_streaming_test_support import SESSION_ID, answer
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    history,
    last_run,
    session_address,
)

JsonObject = dict[str, Any]
Script = Callable[[], AsyncIterator[JsonObject]]


class _ScriptedStreamAdapter(StubAdapter):
    """Streams one scripted async generator per request."""

    def __init__(self, *scripts: Script) -> None:
        super().__init__([], stream_responses=[])
        self._scripts = list(scripts)

    @override
    async def stream(
        self, messages: list[JsonObject], *, model_id: str, **kwargs: Any
    ) -> AsyncIterator[JsonObject]:
        self.stream_requests.append({"messages": deepcopy(messages), "model_id": model_id})
        async for delta in self._scripts.pop(0)():
            yield delta


def _call(call_id: str, city: str) -> JsonObject:
    return {
        "type": "tool_call_delta",
        "id": call_id,
        "name_delta": "get_weather",
        "arguments_delta": f'{{"city":"{city}"}}',
    }


def _runtime(tmp_path: Path, adapter: StubAdapter, started: list[str]) -> Any:
    tools = ToolRegistry()

    def get_weather(context: ToolContext, arguments: JsonObject) -> JsonObject:
        started.append(context.tool_call_id)
        return tool_success({"city": arguments["city"]})

    tools.register("get_weather", "Get weather.", {"type": "object"}, get_weather)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    return StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_completed_tool_call_runs_while_the_model_still_streams(tmp_path: Path) -> None:
    started: list[str] = []

    async def tool_turn() -> AsyncIterator[JsonObject]:
        yield _call("call_berlin", "Berlin")
        yield _call("call_paris", "Paris")
        # The stream moved past the first call: it runs before the turn ends.
        await _until(lambda: started == ["call_berlin"])
        yield {"type": "finish", "reason": "tool_calls"}

    async def final_turn() -> AsyncIterator[JsonObject]:
        for delta in answer("Sunny in both"):
            yield delta

    adapter = _ScriptedStreamAdapter(tool_turn, final_turn)
    runtime = _runtime(tmp_path, adapter, started)

    assistant = await build_chat_loop(runtime).send("coder", "Weather?", session_id=SESSION_ID)

    assert assistant.content == "Sunny in both"
    assert started == ["call_berlin", "call_paris"]
    tool_turn_message = history(runtime)[1]
    assert [call.id for call in tool_turn_message.tool_calls or []] == [
        "call_berlin",
        "call_paris",
    ]
    events = await runtime.timelines.events(last_run(runtime))
    started_events = [event for event in events if event.type == TOOL_CALL_STARTED_EVENT]
    # Started calls name the Assistant turn they belong to.
    assert {event.payload["assistant_message_id"] for event in started_events} == {
        tool_turn_message.id
    }


@pytest.mark.asyncio
async def test_a_stream_break_after_a_started_tool_call_keeps_it_and_continues(
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def broken_turn() -> AsyncIterator[JsonObject]:
        yield {"type": "content_delta", "text": "Checking."}
        yield _call("call_berlin", "Berlin")
        yield {"type": "tool_call_delta", "id": "call_paris", "name_delta": "get_weather"}
        await _until(lambda: started == ["call_berlin"])
        raise NetworkError("connection reset")

    async def continued_turn() -> AsyncIterator[JsonObject]:
        for delta in answer("Berlin is sunny"):
            yield delta

    adapter = _ScriptedStreamAdapter(broken_turn, continued_turn)
    runtime = _runtime(tmp_path, adapter, started)

    assistant = await build_chat_loop(runtime).send("coder", "Weather?", session_id=SESSION_ID)

    # The started call ran once and was not replayed; the unfinished one never ran.
    assert started == ["call_berlin"]
    assert assistant.content == "Berlin is sunny"
    user, tool_turn, tool_result, note, final = history(runtime)[:5]
    assert (tool_turn.content, tool_turn.interrupted) == ("Checking.", False)
    assert [call.id for call in tool_turn.tool_calls or []] == ["call_berlin"]
    assert tool_result.tool_call_id == "call_berlin"
    assert note.content == TOOL_CALLS_STREAM_RECOVERY_NOTE
    assert final is assistant or final.id == assistant.id
    # The continuation request carries the started call and its result.
    continuation = adapter.stream_requests[1]["messages"]
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call_berlin"
        for message in continuation
    )


@pytest.mark.asyncio
async def test_a_started_tool_call_writes_to_its_session_while_its_turn_is_persisted(
    tmp_path: Path,
) -> None:
    started: list[str] = []
    address = session_address("coder", SESSION_ID)

    async def write_note(context: ToolContext, arguments: JsonObject) -> JsonObject:
        # Wait until Chat holds the Session lock to persist this turn and waits
        # for this call, then write under the same lock (as channel_send does).
        await _until(lambda: any(message.tool_calls for message in history(runtime)))
        async with runtime.chat_sessions.write_lock(address):
            started.append(context.tool_call_id)
        return tool_success({})

    tools = ToolRegistry()
    tools.register("write_note", "Write a note.", {"type": "object"}, write_note)

    async def tool_turn() -> AsyncIterator[JsonObject]:
        yield {"type": "tool_call_delta", "id": "call_note", "name_delta": "write_note"}
        yield {"type": "tool_call_delta", "id": "call_note", "arguments_delta": "{}"}
        yield {"type": "content_delta", "text": "Noted."}
        yield {"type": "finish", "reason": "tool_calls"}

    async def final_turn() -> AsyncIterator[JsonObject]:
        for delta in answer("Done"):
            yield delta

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["write_note"])
    adapter = _ScriptedStreamAdapter(tool_turn, final_turn)
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    async with asyncio.timeout(5):
        assistant = await build_chat_loop(runtime).send("coder", "Note it", session_id=SESSION_ID)

    assert assistant.content == "Done"
    assert started == ["call_note"]
