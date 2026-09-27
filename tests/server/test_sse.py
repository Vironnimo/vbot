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
from core.sessions import SessionAddress
from core.tools import FileReadState, register_read_tool
from server._streams import _sse_run_events
from server.app import create_app
from server.file_delivery import FileDelivery
from tests.server.rpc_test_support import StubAdapter, StubRuntime

EXPECTED_SSE_EVENT_NAMES = [
    "run_started",
    "user_message_persisted",
    "provider_request_status",
    "reasoning_delta",
    "tool_call_delta",
    "reasoning",
    "assistant_output",
    "provider_request_status",
    "model_step_usage",
    "tool_call_started",
    "tool_call_result",
    "provider_request_status",
    "assistant_output_delta",
    "assistant_output",
    "provider_request_status",
    "model_step_usage",
    "run_completed",
]


def test_chat_stream_returns_sse_url_and_endpoint_replays_visible_timeline(tmp_path: Path) -> None:
    runtime, workspace = _read_tool_runtime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        response = client.get(_stream_chat(client)["sse_url"])

    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    assert [event["id"] for event in events] == [
        str(index) for index in range(1, len(EXPECTED_SSE_EVENT_NAMES) + 1)
    ]
    assert [event["event"] for event in events] == EXPECTED_SSE_EVENT_NAMES
    reasoning_delta_data = cast(
        dict[str, Any],
        next(event for event in events if event["event"] == "reasoning_delta")["data"],
    )
    tool_delta_data = cast(
        dict[str, Any],
        next(event for event in events if event["event"] == "tool_call_delta")["data"],
    )
    reasoning_data = cast(
        dict[str, Any], next(event for event in events if event["event"] == "reasoning")["data"]
    )
    tool_started_data = cast(
        dict[str, Any],
        next(event for event in events if event["event"] == "tool_call_started")["data"],
    )
    tool_result_data = cast(
        dict[str, Any],
        next(event for event in events if event["event"] == "tool_call_result")["data"],
    )
    assistant_delta_data = cast(
        dict[str, Any],
        next(event for event in events if event["event"] == "assistant_output_delta")["data"],
    )
    assistant_data = cast(
        dict[str, Any],
        next(event for event in reversed(events) if event["event"] == "assistant_output")["data"],
    )
    fingerprint = runtime.tools.schema_fingerprint("read")
    assert reasoning_delta_data["payload"]["reasoning_delta"] == "Thinking clearly"
    assert tool_delta_data["payload"]["name_delta"] == "read"
    assert tool_delta_data["payload"]["arguments_delta"] == '{"path":"note.txt"}'
    assert reasoning_data["payload"]["message"]["reasoning"] == "Thinking clearly"
    started_payload = dict(tool_started_data["payload"])
    display = started_payload.pop("display")
    assert (
        tool_result_data["payload"]["assistant_message_id"]
        == reasoning_data["payload"]["message"]["id"]
    )
    assert started_payload == {
        "assistant_message_id": reasoning_data["payload"]["message"]["id"],
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
        stream = _stream_chat(client, session_id="session-file", content="Show it")
        response = client.get(stream["sse_url"])

    assistant_event = next(
        event for event in _parse_sse(response.text) if event["event"] == ASSISTANT_OUTPUT_EVENT
    )
    assistant_content = assistant_event["data"]["payload"]["message"]["content"]
    assert assistant_content.startswith("Here: ![streamed.png](/api/files/")
    assert str(image) not in assistant_content
    canonical = runtime.chat_sessions.get(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-file")
    ).load()[-2]
    assert canonical.output_files == [
        AssistantFileReference(
            line_index=0,
            path=str(image.resolve()),
            start_index=6,
            end_index=11 + len(str(image)),
        )
    ]


def test_sse_endpoint_replays_after_the_requested_sequence(tmp_path: Path) -> None:
    runtime, _workspace = _read_tool_runtime(tmp_path)
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        sse_url = _stream_chat(client)["sse_url"]
        # (query, Last-Event-ID header) -> number of skipped timeline events.
        controls = [
            ("?after_sequence=3", None, 3),
            ("", "4", 4),
            # An explicit after_sequence wins over the reconnect header.
            ("?after_sequence=2", "5", 2),
            # Malformed or negative cursors clamp to a full replay.
            ("?after_sequence=bad", None, 0),
            ("", "-8", 0),
        ]
        replays = [
            _event_names(
                client.get(
                    f"{sse_url}{query}",
                    headers=None if last_event_id is None else {"Last-Event-ID": last_event_id},
                ).text
            )
            for query, last_event_id, _skipped in controls
        ]
        missing_run = client.get("/api/runs/missing/events")

    assert replays == [EXPECTED_SSE_EVENT_NAMES[skipped:] for _query, _header, skipped in controls]
    assert missing_run.status_code == 404


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
@pytest.mark.parametrize("include_file_urls", [False, True])
async def test_sse_projects_assistant_file_references_to_signed_urls(
    tmp_path: Path, include_file_urls: bool
) -> None:
    image = tmp_path / "sse.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    marker = f"file:{image}"
    message = ChatMessage.assistant(
        model="provider/model",
        content=f"{marker}\n[unverified](/api/files/forged.signature)",
        output_files=[
            AssistantFileReference(
                line_index=0, path=str(image.resolve()), start_index=0, end_index=len(marker)
            )
        ],
    )
    run = Run(run_id="run-file", agent_id="coder", session_id="session-one")
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": message.to_dict()})
    delivery = FileDelivery(secret=b"sse-secret")
    stream = _sse_run_events(run, file_delivery=delivery, include_file_urls=include_file_urls)

    event = await anext(stream)
    await stream.aclose()

    assert "output_files" not in event
    assert str(image) not in event
    assert "![sse.png](/api/files/" in event
    data = _parse_sse(event)[0]["data"]
    if include_file_urls:
        assert len(data["file_urls"]) == 1
        url = data["file_urls"][0]
        assert url in data["payload"]["message"]["content"]
        assert delivery.resolve_token(url.removeprefix("/api/files/")) is not None
    else:
        assert "file_urls" not in data

    assert run.subscriber_count == 0


async def _read_next_sse_event(stream: AsyncIterator[str]) -> str:
    return await anext(stream)


def _read_tool_runtime(tmp_path: Path) -> tuple[StubRuntime, Path]:
    """A runtime whose two-turn stream reads ``note.txt`` and then answers."""
    runtime = StubRuntime(tmp_path, StubAdapter(stream_deltas=_test_stream_turns()))
    register_read_tool(
        runtime.tools,
        attachment_store=None,
        speech_service=None,
        file_state=FileReadState(),
        speech_max_size_bytes=20_971_520,
    )
    workspace = tmp_path / "workspace"
    runtime.agents.update("coder", model="openai/gpt-5.2::api-key", workspace=str(workspace))
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("note.txt").write_text("SSE visible content", encoding="utf-8")
    return runtime, workspace


def _stream_chat(
    client: TestClient, *, session_id: str = "session-one", content: str = "Hi"
) -> dict[str, Any]:
    created = client.post(
        "/api/rpc",
        json={
            "method": "session.create",
            "params": {"agent_id": "coder", "session_id": session_id},
        },
    )
    assert created.json()["ok"] is True
    response = client.post(
        "/api/rpc",
        json={
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": session_id, "content": content},
        },
    )
    return cast(dict[str, Any], response.json()["result"])


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
