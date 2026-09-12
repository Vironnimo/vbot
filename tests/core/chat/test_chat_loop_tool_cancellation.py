"""Tests for chat loop tool cancellation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.runs import (
    MODEL_STEP_USAGE_EVENT,
    TOOL_CALL_RESULT_EVENT,
    RunCancelledError,
    RunStatus,
)
from core.tools import (
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
    session_address,
)
from tests.core.chat.chat_loop_tools_test_support import (
    JsonObject,
)


@pytest.mark.asyncio
async def test_send_dispatches_tool_and_resends_context_until_final(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Need weather.",
                "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Sunny", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
        display=ToolDisplay(summary_fields=("city",)),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Weather?", session_id="session-one")

    persisted = [
        message.to_dict()
        for message in runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    ]
    assert assistant.content == "Sunny"
    assert [message["role"] for message in persisted] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert persisted[1]["reasoning_meta"] == {"encrypted_content": "opaque-current-turn"}
    assert persisted[2]["tool_call_id"] == "call_abc"
    assert persisted[2]["timing"]["duration_ms"] >= 0
    assert json.loads(persisted[2]["content"]) == tool_success({"temp": 22, "city": "Berlin"})
    assert persisted[4]["run_id"]
    assert persisted[4]["status"] == "completed"
    assert persisted[4]["timing"]["duration_ms"] >= 0
    assert [message["role"] for message in adapter.requests[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert adapter.requests[1]["messages"][2]["reasoning_meta"] == {
        "encrypted_content": "opaque-current-turn"
    }
    assert adapter.requests[1]["messages"][2]["reasoning"] == "Need weather."
    # usage is persisted on the assistant turn but never sent to the provider.
    assert persisted[1]["usage"]["input_tokens"] == 11
    assert persisted[1]["usage"]["output_tokens"] == 7
    assert "usage" not in adapter.requests[1]["messages"][2]
    assert "timing" not in adapter.requests[1]["messages"][3]
    tool_result_events = [
        event
        for event in runtime.chat_runs.get(persisted[4]["run_id"]).events
        if event.type == "tool_call_result"
    ]
    assert tool_result_events[0].payload["timing"]["duration_ms"] >= 0
    usage_events = [
        event
        for event in runtime.chat_runs.get(persisted[4]["run_id"]).events
        if event.type == MODEL_STEP_USAGE_EVENT
    ]
    assistant_turns = [message for message in persisted if message["role"] == "assistant"]
    assert [event.payload["usage"] for event in usage_events] == [
        message["usage"] for message in assistant_turns
    ]
    assert usage_events[0].payload["context_usage"] == {
        "tokens": 45,
        "estimated": True,
        "estimated_delta_tokens": 34,
        "provider_input_tokens": 11,
        "provider_output_tokens": 7,
    }
    assert usage_events[0].payload["session_usage"] == {
        "measured_turns": 1,
        "estimated_turns": 0,
        "cache_turns": 0,
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert usage_events[1].payload["usage"]["estimated"] is True
    assert usage_events[1].payload["context_usage"]["estimated"] is True
    assert usage_events[1].payload["context_usage"]["tokens"] > 18
    assert usage_events[1].payload["session_usage"] == {
        "measured_turns": 1,
        "estimated_turns": 1,
        "cache_turns": 0,
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


@pytest.mark.asyncio
async def test_real_run_cancel_during_parallel_tools_repairs_the_next_request(
    tmp_path: Path,
) -> None:
    slow_started = asyncio.Event()
    slow_release = asyncio.Event()
    cancel_callbacks: list[str] = []

    async def fast_probe(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        await slow_started.wait()
        return tool_success({"probe": "fast"})

    async def slow_probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        def cancel() -> None:
            cancel_callbacks.append(context.tool_call_id)
            slow_release.set()

        context.on_cancel(cancel)
        slow_started.set()
        await slow_release.wait()
        return tool_failure("cancelled_by_user", "Slow probe cancelled.")

    tools = ToolRegistry()
    tools.register(
        "fast_probe",
        "Complete while a sibling remains active.",
        {"type": "object"},
        fast_probe,
        parallel_safe=True,
    )
    tools.register(
        "slow_probe",
        "Remain active until cancelled.",
        {"type": "object"},
        slow_probe,
        parallel_safe=True,
    )
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["fast_probe", "slow_probe"],
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_fast", "name": "fast_probe", "arguments": {}},
                    {"id": "call_slow", "name": "slow_probe", "arguments": {}},
                ],
            },
            {"content": "Recovered on the next Run.", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    cancelled_run = await loop.start_run(
        "coder",
        "Run both probes.",
        session_id="session-one",
    )
    await _wait_for_tool_result_event(cancelled_run, "call_fast")

    cancelled_run.request_cancel(reason="user")

    with pytest.raises(RunCancelledError):
        await cancelled_run.wait()

    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    after_cancel = session.load()
    assert cancel_callbacks == ["call_slow"]
    assert runtime.process_manager.cancelled_scopes == [cancelled_run.id]
    assert persisted_roles(after_cancel) == ["user", "assistant"]
    assert [message.status for message in after_cancel if message.role == "run_summary"] == [
        "cancelled"
    ]
    assert not any(message.role == "tool" for message in after_cancel)
    assert any(
        event.type == TOOL_CALL_RESULT_EVENT and event.payload["tool_call"]["id"] == "call_fast"
        for event in cancelled_run.events
    )

    recovered = await loop.send(
        "coder",
        "Continue safely.",
        session_id="session-one",
    )

    assert recovered.content == "Recovered on the next Run."
    repaired_request = adapter.requests[1]["messages"]
    repaired_results = [
        message
        for message in repaired_request
        if message.get("role") == "tool"
        and message.get("tool_call_id") in {"call_fast", "call_slow"}
    ]
    assert [message["tool_call_id"] for message in repaired_results] == [
        "call_fast",
        "call_slow",
    ]
    for message in repaired_results:
        result = json.loads(message["content"])
        assert result["error"]["code"] == "result_unavailable"
    final_history = session.load()
    assert not any(message.role == "tool" for message in final_history)
    assert persisted_roles(final_history) == ["user", "assistant", "user", "assistant"]
    assert [message.status for message in final_history if message.role == "run_summary"] == [
        "cancelled",
        "completed",
    ]


@pytest.mark.asyncio
async def test_per_call_cancel_persists_cancelled_and_completed_siblings_in_order(
    tmp_path: Path,
) -> None:
    cancellable_started = asyncio.Event()
    cancel_fired = asyncio.Event()
    sibling_started = asyncio.Event()
    sibling_release = asyncio.Event()
    cancel_callbacks: list[str] = []

    async def cancellable(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        def cancel() -> None:
            cancel_callbacks.append(context.tool_call_id)
            cancel_fired.set()

        context.on_cancel(cancel)
        cancellable_started.set()
        await cancel_fired.wait()
        assert context.was_cancelled_by_user() is True
        return tool_failure("cancelled_by_user", "Cancelled by the user.")

    async def sibling(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        sibling_started.set()
        await sibling_release.wait()
        return tool_success({"sibling": "completed"})

    tools = ToolRegistry()
    tools.register(
        "cancellable",
        "Wait for per-call cancellation.",
        {"type": "object"},
        cancellable,
        parallel_safe=True,
    )
    tools.register(
        "sibling",
        "Complete beside a cancelled Tool.",
        {"type": "object"},
        sibling,
        parallel_safe=True,
    )
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["cancellable", "sibling"],
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_cancel", "name": "cancellable", "arguments": {}},
                    {"id": "call_sibling", "name": "sibling", "arguments": {}},
                ],
            },
            {"content": "Both Results observed.", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    loop = build_chat_loop(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await loop.start_run("coder", "Run siblings.", session_id="session-one")
    await asyncio.wait_for(
        asyncio.gather(cancellable_started.wait(), sibling_started.wait()),
        timeout=5,
    )

    assert run.cancel_tool_call("call_cancel") is True
    sibling_release.set()
    assistant = await run.wait()

    assert assistant.content == "Both Results observed."
    assert run.status is RunStatus.COMPLETED
    assert run.cancel_requested is False
    assert cancel_callbacks == ["call_cancel"]
    persisted_tools = [
        message
        for message in runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        if message.role == "tool"
    ]
    assert [message.tool_call_id for message in persisted_tools] == [
        "call_cancel",
        "call_sibling",
    ]
    assert json.loads(cast(str, persisted_tools[0].content))["error"]["code"] == (
        "cancelled_by_user"
    )
    assert json.loads(cast(str, persisted_tools[1].content)) == tool_success(
        {"sibling": "completed"}
    )


async def _wait_for_tool_result_event(run: Any, tool_call_id: str) -> None:
    async def result_was_emitted() -> None:
        while not any(
            event.type == TOOL_CALL_RESULT_EVENT
            and event.payload["tool_call"]["id"] == tool_call_id
            for event in run.events
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(result_was_emitted(), timeout=5)
