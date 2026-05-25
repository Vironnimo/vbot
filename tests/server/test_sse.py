"""Tests for server-sent run event streaming."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.tools import register_read_tool
from server.app import create_app
from tests.server.test_rpc import StubAdapter, StubRuntime

EXPECTED_SSE_EVENT_NAMES = [
    "run_started",
    "user_message_persisted",
    "reasoning_delta",
    "tool_call_delta",
    "tool_call_delta",
    "reasoning",
    "assistant_output",
    "tool_call_started",
    "tool_call_result",
    "assistant_output_delta",
    "assistant_output",
    "run_completed",
]


def test_chat_stream_returns_sse_url_and_endpoint_replays_visible_timeline(tmp_path: Path) -> None:
    adapter = StubAdapter(stream_deltas=_test_stream_turns())
    runtime = StubRuntime(tmp_path, adapter)
    register_read_tool(runtime.tools)
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
    assert [event["id"] for event in events] == [str(index) for index in range(1, 13)]
    assert [event["event"] for event in events] == EXPECTED_SSE_EVENT_NAMES
    reasoning_delta_data = cast(dict[str, Any], events[2]["data"])
    tool_delta_data = cast(dict[str, Any], events[3]["data"])
    reasoning_data = cast(dict[str, Any], events[5]["data"])
    tool_started_data = cast(dict[str, Any], events[7]["data"])
    tool_result_data = cast(dict[str, Any], events[8]["data"])
    assistant_delta_data = cast(dict[str, Any], events[9]["data"])
    assistant_data = cast(dict[str, Any], events[10]["data"])
    assert reasoning_delta_data["payload"]["reasoning_delta"] == "Thinking clearly"
    assert tool_delta_data["payload"]["name_delta"] == "read"
    assert reasoning_data["payload"]["message"]["reasoning"] == "Thinking clearly"
    assert tool_started_data["payload"] == {
        "tool_call": {
            "id": "call-one",
            "index": 0,
            "name": "read",
            "arguments": {"path": "note.txt"},
        },
        "display": {"summary": "note.txt", "hidden_argument_keys": []},
    }
    assert tool_result_data["payload"]["tool_call"] == {
        "id": "call-one",
        "index": 0,
        "name": "read",
    }
    assert tool_result_data["payload"]["result"]["ok"] is True
    assert tool_result_data["payload"]["result"]["error"] is None
    assert tool_result_data["payload"]["result"]["data"]["content"] == "SSE visible content"
    assert tool_result_data["payload"]["result"]["artifacts"] == []
    assert "tool_call_failed" not in [event["event"] for event in events]
    assert "batch" not in response.text
    assert assistant_delta_data["payload"]["content_delta"] == "Done"
    assert assistant_data["payload"]["message"]["content"] == "Done"
    assert "reasoning_meta" not in response.text


def test_sse_endpoint_returns_not_found_for_unknown_run(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        response = client.get("/api/runs/missing/events")

    assert response.status_code == 404


def test_sse_endpoint_replays_after_explicit_sequence(tmp_path: Path) -> None:
    response = _stream_test_run(tmp_path, sse_url_suffix="?after_sequence=3")

    assert _event_names(response.text) == [
        "tool_call_delta",
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "tool_call_started",
        "tool_call_result",
        "assistant_output_delta",
        "assistant_output",
        "run_completed",
    ]


def test_sse_endpoint_replays_after_last_event_id_header(tmp_path: Path) -> None:
    response = _stream_test_run(tmp_path, headers={"Last-Event-ID": "4"})

    assert _event_names(response.text) == [
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "tool_call_started",
        "tool_call_result",
        "assistant_output_delta",
        "assistant_output",
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
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "tool_call_started",
        "tool_call_result",
        "assistant_output_delta",
        "assistant_output",
        "run_completed",
    ]


def test_sse_endpoint_clamps_malformed_sequence_controls(tmp_path: Path) -> None:
    malformed_response = _stream_test_run(tmp_path, sse_url_suffix="?after_sequence=bad")
    negative_response = _stream_test_run(tmp_path, headers={"Last-Event-ID": "-8"})

    assert _event_names(malformed_response.text) == EXPECTED_SSE_EVENT_NAMES
    assert _event_names(negative_response.text) == EXPECTED_SSE_EVENT_NAMES


def _stream_test_run(
    tmp_path: Path,
    *,
    sse_url_suffix: str = "",
    headers: dict[str, str] | None = None,
) -> Any:
    adapter = StubAdapter(stream_deltas=_test_stream_turns())
    runtime = StubRuntime(tmp_path, adapter)
    register_read_tool(runtime.tools)
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
