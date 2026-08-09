"""Tests for server-sent run event streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.chat import ChatMessage
from core.chat.output_files import AssistantFileReference
from core.runs import ASSISTANT_OUTPUT_EVENT, Run
from core.tools import FileReadState, register_read_tool
from server.app import _sse_run_events, create_app
from server.file_delivery import FileDelivery
from tests.server.test_rpc import StubAdapter, StubRuntime

EXPECTED_SSE_EVENT_NAMES = [
    "run_started",
    "user_message_persisted",
    "reasoning_delta",
    "tool_call_delta",
    "reasoning",
    "assistant_output",
    "model_step_usage",
    "tool_call_started",
    "tool_call_result",
    "assistant_output_delta",
    "assistant_output",
    "model_step_usage",
    "run_completed",
]


def test_chat_stream_returns_sse_url_and_endpoint_replays_visible_timeline(tmp_path: Path) -> None:
    adapter = StubAdapter(stream_deltas=_test_stream_turns())
    runtime = StubRuntime(tmp_path, adapter)
    register_read_tool(
        runtime.tools,
        attachment_store=None,
        speech_service=None,
        file_state=FileReadState(),
        speech_max_size_bytes=20_971_520,
    )
    runtime.agents.update(
        "coder",
        model="openai/gpt-5.2::api-key",
        workspace=str(tmp_path / "workspace"),
    )
    workspace = Path(runtime.agents.get("coder").workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("note.txt").write_text("SSE visible content", encoding="utf-8")
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        create_response = client.post(
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

        assert create_response.json()["ok"] is True
        stream_result = stream_response.json()["result"]
        response = client.get(stream_result["sse_url"])

    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    assert [event["id"] for event in events] == [str(index) for index in range(1, 14)]
    assert [event["event"] for event in events] == EXPECTED_SSE_EVENT_NAMES
    reasoning_delta_data = cast(dict[str, Any], events[2]["data"])
    tool_delta_data = cast(dict[str, Any], events[3]["data"])
    reasoning_data = cast(dict[str, Any], events[4]["data"])
    tool_started_data = cast(dict[str, Any], events[7]["data"])
    tool_result_data = cast(dict[str, Any], events[8]["data"])
    assistant_delta_data = cast(dict[str, Any], events[9]["data"])
    assistant_data = cast(dict[str, Any], events[10]["data"])
    fingerprint = runtime.tools.schema_fingerprint("read")
    assert reasoning_delta_data["payload"]["reasoning_delta"] == "Thinking clearly"
    assert tool_delta_data["payload"]["name_delta"] == "read"
    assert tool_delta_data["payload"]["arguments_delta"] == '{"path":"note.txt"}'
    assert reasoning_data["payload"]["message"]["reasoning"] == "Thinking clearly"
    started_payload = dict(tool_started_data["payload"])
    display = started_payload.pop("display")
    assert started_payload == {
        "tool_call": {
            "id": "call-one",
            "index": 0,
            "name": "read",
            "arguments": {"path": "note.txt"},
        },
        "schema_fingerprint": fingerprint,
    }
    assert display["version"] == 1
    assert display["summary"] == "note.txt"
    assert display["hidden_argument_keys"] == []
    assert display["facts"] == []
    assert display["primary"] == [
        {
            "kind": "path",
            "value": "note.txt",
            "full_value": str(workspace.joinpath("note.txt")).replace("\\", "/"),
            "truncate": "start",
            "tooltip": "always",
            "max_characters": 64,
            "quote": False,
            "copyable": True,
        }
    ]
    assert tool_result_data["payload"]["tool_call"] == {
        "id": "call-one",
        "index": 0,
        "name": "read",
    }
    assert tool_result_data["payload"]["result"]["ok"] is True
    assert tool_result_data["payload"]["result"]["error"] is None
    assert tool_result_data["payload"]["result"]["data"]["content"] == "1| SSE visible content"
    assert tool_result_data["payload"]["result"]["artifacts"] == []
    assert tool_result_data["payload"]["schema_fingerprint"] == fingerprint
    assert tool_result_data["payload"]["error_code"] is None
    assert "tool_call_failed" not in [event["event"] for event in events]
    assert "batch" not in response.text
    assert assistant_delta_data["payload"]["content_delta"] == "Done"
    assert assistant_data["payload"]["message"]["content"] == "Done"
    assert "reasoning_meta" not in response.text
    assert "reasoning_scope" not in response.text


def test_sse_endpoint_returns_not_found_for_unknown_run(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        response = client.get("/api/runs/missing/events")

    assert response.status_code == 404


def test_streaming_chat_projects_completed_path_line_in_stable_event(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "streamed.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nstreamed")
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "content_delta", "text": f"Here: file:{image}"},
            {"type": "finish", "reason": "stop"},
        ]
    )
    runtime = StubRuntime(tmp_path, adapter)
    runtime.agents.update(
        "coder",
        model="openai/gpt-5.2::api-key",
        workspace=str(workspace),
    )
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {"agent_id": "coder", "session_id": "session-file"},
            },
        )
        stream_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.stream",
                "params": {
                    "agent_id": "coder",
                    "session_id": "session-file",
                    "content": "Show it",
                },
            },
        )
        response = client.get(stream_response.json()["result"]["sse_url"])

    assistant_event = next(
        event for event in _parse_sse(response.text) if event["event"] == ASSISTANT_OUTPUT_EVENT
    )
    assistant_content = assistant_event["data"]["payload"]["message"]["content"]
    assert assistant_content.startswith("Here: ![streamed.png](/api/files/")
    assert str(image) not in assistant_content
    canonical = runtime.chat_sessions.get("coder", "session-file").load()[-2]
    assert canonical.output_files == [
        AssistantFileReference(
            line_index=0,
            path=str(image.resolve()),
            start_index=6,
            end_index=11 + len(str(image)),
        )
    ]


def test_sse_endpoint_replays_after_explicit_sequence(tmp_path: Path) -> None:
    response = _stream_test_run(tmp_path, sse_url_suffix="?after_sequence=3")

    assert _event_names(response.text) == [
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "model_step_usage",
        "tool_call_started",
        "tool_call_result",
        "assistant_output_delta",
        "assistant_output",
        "model_step_usage",
        "run_completed",
    ]


def test_sse_endpoint_replays_after_last_event_id_header(tmp_path: Path) -> None:
    response = _stream_test_run(tmp_path, headers={"Last-Event-ID": "4"})

    assert _event_names(response.text) == [
        "reasoning",
        "assistant_output",
        "model_step_usage",
        "tool_call_started",
        "tool_call_result",
        "assistant_output_delta",
        "assistant_output",
        "model_step_usage",
        "run_completed",
    ]


def test_sse_endpoint_prefers_explicit_after_sequence_over_last_event_id(
    tmp_path: Path,
) -> None:
    response = _stream_test_run(
        tmp_path,
        sse_url_suffix="?after_sequence=2",
        headers={"Last-Event-ID": "5"},
    )

    assert _event_names(response.text) == [
        "reasoning_delta",
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "model_step_usage",
        "tool_call_started",
        "tool_call_result",
        "assistant_output_delta",
        "assistant_output",
        "model_step_usage",
        "run_completed",
    ]


def test_sse_endpoint_clamps_malformed_sequence_controls(tmp_path: Path) -> None:
    malformed_response = _stream_test_run(tmp_path, sse_url_suffix="?after_sequence=bad")
    negative_response = _stream_test_run(tmp_path, headers={"Last-Event-ID": "-8"})

    assert _event_names(malformed_response.text) == EXPECTED_SSE_EVENT_NAMES
    assert _event_names(negative_response.text) == EXPECTED_SSE_EVENT_NAMES


@pytest.mark.asyncio
async def test_sse_stream_close_removes_run_subscriber() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    stream = _sse_run_events(run)
    next_event = asyncio.create_task(_read_next_sse_event(stream))

    # The heartbeat-capable stream owns the blocking event read in a child
    # task, so allow the nested Run subscriber to enter before asserting its
    # lifecycle rather than depending on one exact scheduler turn.
    for _ in range(10):
        if run.subscriber_count == 1:
            break
        await asyncio.sleep(0)
    assert run.subscriber_count == 1

    run.emit("visible", {"content": "hello"})
    rendered_event = await next_event
    assert "event: visible" in rendered_event
    assert run.subscriber_count == 1

    await stream.aclose()

    assert run.subscriber_count == 0


@pytest.mark.asyncio
async def test_sse_stream_emits_heartbeat_while_run_is_quiet() -> None:
    run = Run(run_id="run-heartbeat", agent_id="coder", session_id="session-one")
    stream = _sse_run_events(run, heartbeat_interval_seconds=0.001)

    heartbeat = await asyncio.wait_for(anext(stream), timeout=1)

    assert heartbeat == "event: heartbeat\ndata: {}\n\n"
    assert run.subscriber_count == 1

    await stream.aclose()


@pytest.mark.asyncio
async def test_sse_projects_assistant_file_references_to_signed_urls(tmp_path: Path) -> None:
    image = tmp_path / "sse.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    message = ChatMessage.assistant(
        model="provider/model",
        content=str(image),
        output_files=[AssistantFileReference(line_index=0, path=str(image.resolve()))],
    )
    run = Run(run_id="run-file", agent_id="coder", session_id="session-one")
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": message.to_dict()})
    stream = _sse_run_events(run, file_delivery=FileDelivery(secret=b"sse-secret"))

    event = await anext(stream)
    await stream.aclose()

    assert "output_files" not in event
    assert str(image) not in event
    assert "![sse.png](/api/files/" in event

    assert run.subscriber_count == 0


async def _read_next_sse_event(stream: AsyncIterator[str]) -> str:
    return await anext(stream)


def _stream_test_run(
    tmp_path: Path,
    *,
    sse_url_suffix: str = "",
    headers: dict[str, str] | None = None,
) -> Any:
    adapter = StubAdapter(stream_deltas=_test_stream_turns())
    runtime = StubRuntime(tmp_path, adapter)
    register_read_tool(
        runtime.tools,
        attachment_store=None,
        speech_service=None,
        file_state=FileReadState(),
        speech_max_size_bytes=20_971_520,
    )
    runtime.agents.update(
        "coder",
        model="openai/gpt-5.2::api-key",
        workspace=str(tmp_path / "workspace"),
    )
    workspace = Path(runtime.agents.get("coder").workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("note.txt").write_text("SSE visible content", encoding="utf-8")
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
        sse_url = f"{stream_response.json()['result']['sse_url']}{sse_url_suffix}"
        return client.get(sse_url, headers=headers)


def _event_names(body: str) -> list[str]:
    events = _parse_sse(body)
    event_names = [event["event"] for event in events]
    assert [event["id"] for event in events] == [str(event["data"]["sequence"]) for event in events]
    assert event_names == [event["data"]["type"] for event in events]
    return event_names


def _parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        fields = dict(line.split(": ", 1) for line in lines)
        event_id = fields["id"]
        event_name = fields["event"]
        data = json.loads(fields["data"])
        events.append({"id": event_id, "event": event_name, "data": data})
    return events


def _test_stream_turns() -> list[Any]:
    return [
        [
            {"type": "reasoning_delta", "text": "Thinking clearly"},
            {"type": "reasoning_meta", "reasoning_meta": {"secret": "opaque"}},
            {"type": "tool_call_delta", "id": "call-one", "name_delta": "read"},
            {
                "type": "tool_call_delta",
                "id": "call-one",
                "arguments_delta": '{"path":"note.txt"}',
            },
            {"type": "finish", "reason": "tool_calls"},
        ],
        [
            {"type": "content_delta", "text": "Done"},
            {"type": "finish", "reason": "stop"},
        ],
    ]
