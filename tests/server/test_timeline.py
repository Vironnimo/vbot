"""Shared conversation transport: retention, patches, artifacts and cleanup."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import ClientDisconnect

from core.chat import ChatMessage
from core.chat.output_files import AssistantFileReference
from core.runs import Run
from server.file_delivery import FileDelivery
from server.timeline import TimelineStreamingResponse, sse_timeline, timeline_snapshot


def run() -> Run:
    return Run(
        run_id="run-one", agent_id="coder", session_id="session-one", event_retention_limit=4
    )


def data(frame: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: "))),
    )


@pytest.mark.asyncio
async def test_snapshot_survives_replay_eviction_and_patches_only_new_text() -> None:
    active = run()
    state = SimpleNamespace(file_delivery=FileDelivery())
    active.emit("run_started")
    for _ in range(20):
        active.emit("assistant_output_delta", {"content_delta": "x" * 1000})
    assert len(active.events) == 4
    projection = active.timeline
    async with aclosing(sse_timeline(state, active, batch_interval_seconds=0)) as stream:
        initial = data(await anext(stream))
        [row] = initial["items"]
        [child] = row["items"]
        assert child["content"] == "x" * 20_000
        assert child["events"] == []
        active.emit("assistant_output_delta", {"content_delta": "tail"})
        frame = await anext(stream)
        patch = data(frame)
        assert patch["base_sequence"] == initial["sequence"]
        assert patch["children"] == []
        assert patch["child_updates"] == [
            {
                "id": child["id"],
                "type": "assistant_output",
                "append": {"content": "tail"},
                "set": {},
            }
        ]
        assert len(frame) < 1800
        active.mark_completed(None)
        assert active.timeline is None
        terminal = data(await anext(stream))
        assert terminal["terminal"] is True
        assert terminal["run"]["status"] == "completed"
        assert projection is not None
        assert projection.snapshot()[0]["items"][0]["content"].endswith("tail")
        with pytest.raises(StopAsyncIteration):
            await anext(stream)


@pytest.mark.asyncio
async def test_late_user_placement_and_controls_have_explicit_patches() -> None:
    active = run()
    async with aclosing(
        sse_timeline(SimpleNamespace(file_delivery=None), active, batch_interval_seconds=0)
    ) as stream:
        initial = data(await anext(stream))
        user = ChatMessage.user("hello").to_dict()
        active.emit("user_message_persisted", {"message": user})
        patch = data(await anext(stream))
        assert patch["item_order"] == [f"message-{user['id']}", initial["items"][0]["id"]]
        assert patch["upsert"][0]["run_id"] == active.id
        active.emit(
            "model_step_usage", {"iteration_count": 1, "context_usage": {"input_tokens": 123}}
        )
        patch = data(await anext(stream))
        assert patch["state"]["context_usage"] == {"input_tokens": 123}
        assert patch["run"]["iterationCount"] == 1


@pytest.mark.asyncio
async def test_live_and_reconnected_artifacts_use_same_public_content(tmp_path: Path) -> None:
    artifact = tmp_path / "report.txt"
    artifact.write_text("report", encoding="utf-8")
    content = f"file:{artifact.as_posix()}"
    message = ChatMessage.assistant(
        model="test/model",
        content=content,
        output_files=[
            AssistantFileReference(
                path=str(artifact), line_index=0, start_index=0, end_index=len(content)
            )
        ],
    )
    active = run()
    state = SimpleNamespace(file_delivery=FileDelivery())
    async with aclosing(sse_timeline(state, active, batch_interval_seconds=0)) as stream:
        await anext(stream)
        active.emit("assistant_output_delta", {"content_delta": content})
        await anext(stream)
        active.emit("assistant_output", {"message": message.to_dict()})
        patch = data(await anext(stream))
        final = patch["child_updates"][0]
        assert "/api/files/" in final["set"]["content"]
        assert "content" not in final["append"]
        assert final["set"]["content"] == final["set"]["messages"][0]["content"]
    active.mark_completed(None)
    summary = ChatMessage.run_summary(
        run_id=active.id,
        status="completed",
        iteration_count=1,
        timing={
            "started_at": active.created_at,
            "completed_at": active.updated_at,
            "duration_ms": 1,
        },
    )

    class Session:
        async def load_run_messages_async(self, _run_id: str) -> list[ChatMessage]:
            return [message, summary]

    class Sessions:
        async def get_async(self, _address: Any) -> Session:
            return Session()

    state.runtime = SimpleNamespace(chat_sessions=Sessions())
    reconnected = await timeline_snapshot(state, active)
    [row] = reconnected["items"]
    assert "/api/files/" in row["items"][0]["content"]
    assert "output_files" not in json.dumps(reconnected)
    assert reconnected["terminal"] is True


@pytest.mark.asyncio
async def test_response_closes_pending_projection_wait_on_socket_failure() -> None:
    active = run()
    initial_tasks = asyncio.all_tasks()
    stream = sse_timeline(SimpleNamespace(file_delivery=None), active, heartbeat_interval_seconds=0)
    response = TimelineStreamingResponse(stream, media_type="text/event-stream")
    bodies = 0

    async def send(message: dict[str, Any]) -> None:
        nonlocal bodies
        if message["type"] == "http.response.body":
            bodies += 1
            if bodies == 2:
                raise OSError("socket closed")

    async def receive() -> dict[str, Any]:
        await asyncio.Future()
        raise AssertionError("unreachable")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert not [task for task in asyncio.all_tasks() - initial_tasks if not task.done()]
