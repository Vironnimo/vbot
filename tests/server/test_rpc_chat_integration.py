"""Tests for rpc chat integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.chat import CommandDispatcher
from core.runs import ChatRunManager
from core.sessions import SessionAddress
from core.tools import ToolContext, tool_success
from server.app import create_app
from server.rpc.methods import dispatch_rpc
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.server.rpc_integration_test_support import (
    IntegrationRuntime,
    JsonObject,
    SequencedAdapter,
)


def test_http_session_create_send_sse_and_sqlite_persistence(tmp_path: Path) -> None:
    adapter = SequencedAdapter(
        [
            {
                "content": None,
                "reasoning": "Need the lookup tool.",
                "reasoning_meta": {"encrypted_content": "opaque"},
                "tool_calls": [
                    {"id": "call_lookup", "name": "lookup", "arguments": {"query": "vBot"}}
                ],
            },
            {"content": "Lookup complete.", "tool_calls": None},
        ]
    )
    runtime = IntegrationRuntime(tmp_path, adapter)
    runtime.tools.register(
        "lookup",
        "Look up a value.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"result": f"found {arguments['query']}"}),
    )
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        create_response = client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        )
        send_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.send",
                "params": {"agent_id": "coder", "session_id": "session-one", "content": "Go"},
            },
        )
        send_result = send_response.json()["result"]
        sse_response = client.get(f"/api/runs/{send_result['run_id']}/events")
        history_result = client.post(
            "/api/rpc",
            json={
                "method": "chat.history",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        ).json()["result"]

    assert create_response.json() == {
        "ok": True,
        "result": {"agent_id": "coder", "session_id": "session-one"},
    }
    assert send_response.json()["ok"] is True
    assert send_result["status"] == "completed"
    assert send_result["message"]["content"] == "Lookup complete."
    assert [event["type"] for event in send_result["events"]] == [
        "run_started",
        "user_message_persisted",
        "model_step_usage",
        "reasoning",
        "tool_call_started",
        "tool_call_result",
        "model_step_usage",
        "assistant_output",
        "run_completed",
    ]
    assert "reasoning_meta" not in json.dumps(send_result)
    assert "reasoning_meta" not in sse_response.text
    assert [event["event"] for event in _parse_sse(sse_response.text)] == [
        "run_started",
        "user_message_persisted",
        "model_step_usage",
        "reasoning",
        "tool_call_started",
        "tool_call_result",
        "model_step_usage",
        "assistant_output",
        "run_completed",
    ]

    messages = runtime.chat_sessions.get(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    ).load()
    assert [message.role for message in messages] == [
        "note",
        "user",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert str(messages[0].content).startswith('[reply-surface] {"kind":"webui"}')
    assert messages[-1].status == "completed"
    assert messages[-1].timing is not None
    assert messages[2].reasoning_meta == {"encrypted_content": "opaque"}
    assert messages[4].usage is not None
    assert history_result["context_usage"] == messages[4].usage["context_usage"]
    assert history_result["context_usage"]["estimated"] is True
    assert history_result["context_usage"]["tokens"] > 0
    tool_message_content = messages[3].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == {
        "ok": True,
        "error": None,
        "data": {"result": "found vBot"},
        "artifacts": [],
    }
    assert adapter.closed is True


def test_http_stream_sse_replays_visible_running_timeline(tmp_path: Path) -> None:
    adapter = SequencedAdapter(
        [
            {
                "content": "Streamed final.",
                "reasoning": "Readable thinking.",
                "reasoning_meta": {"secret": "hidden"},
                "tool_calls": None,
            }
        ]
    )
    runtime = IntegrationRuntime(tmp_path, adapter)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        )
        stream_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.stream",
                "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
            },
        )
        stream_result = stream_response.json()["result"]
        sse_response = client.get(stream_result["sse_url"])

    assert stream_response.json()["ok"] is True
    assert stream_result["status"] == "running"
    events = _parse_sse(sse_response.text)
    assert [event["event"] for event in events] == [
        "run_started",
        "user_message_persisted",
        "reasoning_delta",
        "assistant_output_delta",
        "reasoning",
        "assistant_output",
        "model_step_usage",
        "run_completed",
    ]
    assert events[2]["data"]["payload"]["reasoning_delta"] == "Readable thinking."
    assert events[3]["data"]["payload"]["content_delta"] == "Streamed final."
    assert events[4]["data"]["payload"]["message"]["reasoning"] == "Readable thinking."
    assert events[5]["data"]["payload"]["message"]["content"] == "Streamed final."
    assert "reasoning_meta" not in sse_response.text


@pytest.mark.asyncio
async def test_cancel_suppresses_late_output_and_prevents_new_tool_steps(tmp_path: Path) -> None:
    adapter = SequencedAdapter(
        [
            {
                "content": None,
                "reasoning": "Need slow work.",
                "tool_calls": [
                    {"id": "call_slow", "name": "slow_tool", "arguments": {"value": "late"}}
                ],
            },
            {"content": "Should not be requested", "tool_calls": None},
        ]
    )
    runtime = IntegrationRuntime(tmp_path, adapter)
    state = _make_state(runtime)
    slow_tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    tool_results: list[JsonObject] = []

    async def slow_tool(context: ToolContext, arguments: JsonObject) -> JsonObject:
        slow_tool_started.set()
        while not context.is_cancelled():
            await asyncio.sleep(0)
        await release_tool.wait()
        result = tool_success({"value": arguments["value"]})
        tool_results.append(result)
        return result

    runtime.tools.register("slow_tool", "Slow tool.", {"type": "object"}, slow_tool)
    runtime.chat_sessions.create("coder", session_id="session-one")
    stream_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Start"},
        },
    )
    await slow_tool_started.wait()

    cancel_response = await dispatch_rpc(
        state,
        {"method": "chat.cancel", "params": {"run_id": stream_response["result"]["run_id"]}},
    )
    release_tool.set()
    await asyncio.sleep(0)

    run = state.chat_runs.get(stream_response["result"]["run_id"])
    messages = runtime.chat_sessions.get(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    ).load()

    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "cancelled"
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        "reasoning_delta",
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "model_step_usage",
        "tool_call_started",
        "run_cancelled",
    ]
    tool_delta = next(event for event in run.events if event.type == "tool_call_delta")
    assert tool_delta.payload == {
        "tool_call_id": "call_slow",
        "name_delta": "slow_tool",
        "arguments_delta": '{"value":"late"}',
    }
    assert [message.role for message in messages] == [
        "note",
        "user",
        "assistant",
        "run_summary",
    ]
    assert str(messages[0].content).startswith('[reply-surface] {"kind":"webui"}')
    assert messages[-1].status == "cancelled"
    assert messages[-1].timing is not None
    assert tool_results == []
    assert len(adapter.stream_requests) == 1


@pytest.mark.asyncio
async def test_same_session_queued_while_different_sessions_run_in_parallel(
    tmp_path: Path,
) -> None:
    first_adapter = SequencedAdapter(block=True)
    second_adapter = SequencedAdapter([{"content": "Second done", "tool_calls": None}])
    runtime = IntegrationRuntime(tmp_path, [first_adapter, second_adapter])
    state = _make_state(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")
    runtime.chat_sessions.create("coder", session_id="session-two")

    first_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "First"},
        },
    )
    await first_adapter.request_started.wait()
    same_session_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Again"},
        },
    )
    parallel_response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {"agent_id": "coder", "session_id": "session-two", "content": "Parallel"},
        },
    )

    assert first_response["ok"] is True
    assert same_session_response["ok"] is True
    assert same_session_response["result"]["queued"] is True
    queued_item = same_session_response["result"]["item"]
    assert queued_item["content"] == "Again"
    assert isinstance(queued_item["id"], str)
    assert queued_item["id"]
    assert parallel_response["ok"] is True
    assert parallel_response["result"]["message"]["content"] == "Second done"

    removed = state.chat_runs.remove_queued(
        "coder", "session-one", queued_item["id"], project_id=None
    )
    assert removed is True

    run = state.chat_runs.get(first_response["result"]["run_id"])
    first_adapter.release.set()
    await run.wait()


def _make_state(runtime: Any) -> Any:
    chat_runs = ChatRunManager()
    runtime.chat_runs = chat_runs
    return type(
        "IntegrationState",
        (),
        {
            "runtime": runtime,
            "chat_runs": chat_runs,
            "chat_loop": build_chat_loop(runtime),
            "streaming_chat_loop": build_chat_loop(runtime, streaming=True),
            "command_dispatcher": CommandDispatcher(chat_runs),
            "event_bus": None,
        },
    )()


def _parse_sse(body: str) -> list[JsonObject]:
    events: list[JsonObject] = []
    for block in body.strip().split("\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        fields = dict(line.split(": ", 1) for line in lines)
        event_name = fields["event"]
        data = json.loads(fields["data"])
        events.append({"event": event_name, "data": data})
    return events
