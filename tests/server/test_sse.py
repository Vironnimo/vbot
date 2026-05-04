"""Tests for server-sent run event streaming."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from server.app import create_app
from tests.server.test_rpc import StubAdapter, StubRuntime


def test_chat_stream_returns_sse_url_and_endpoint_replays_visible_timeline(tmp_path: Path) -> None:
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Thinking clearly",
                "reasoning_meta": {"secret": "opaque"},
                "tool_calls": [
                    {"id": "call-one", "name": "lookup", "arguments": {"query": "vBot"}}
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    runtime = StubRuntime(tmp_path, adapter)
    runtime.tools.register(
        "lookup", "Look up a value", {"type": "object"}, lambda _args: {"ok": True}
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
    assert [event["event"] for event in events] == [
        "run_started",
        "user_message_persisted",
        "reasoning",
        "tool_call_started",
        "tool_call_result",
        "assistant_output",
        "run_completed",
    ]
    reasoning_data = cast(dict[str, Any], events[2]["data"])
    tool_data = cast(dict[str, Any], events[3]["data"])
    assistant_data = cast(dict[str, Any], events[5]["data"])
    assert reasoning_data["payload"]["message"]["reasoning"] == "Thinking clearly"
    assert tool_data["payload"]["tool_call"]["name"] == "lookup"
    assert assistant_data["payload"]["message"]["content"] == "Done"
    assert "reasoning_meta" not in response.text


def test_sse_endpoint_returns_not_found_for_unknown_run(tmp_path: Path) -> None:
    app = create_app(runtime=cast(Any, StubRuntime(tmp_path, StubAdapter())))

    with TestClient(app) as client:
        response = client.get("/api/runs/missing/events")

    assert response.status_code == 404


def _parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append({"event": event_name, "data": data})
    return events
