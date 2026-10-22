"""Shared conversation transport: retention, patches, artifacts and cleanup."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import ClientDisconnect

from core.chat import ChatMessage
from core.chat.output_files import AssistantFileReference
from core.runs import Run
from core.sessions import SessionAddress
from server.file_delivery import FileDelivery
from server.timeline import (
    TimelineStreamingResponse,
    project_timeline_history,
    sse_timeline,
    timeline_children,
    timeline_snapshot,
    window_timeline_update,
)


@pytest.mark.parametrize("child_limit", [0, 1])
def test_history_usage_survives_windowing_and_absent_incremental_assistants(
    child_limit: int,
) -> None:
    usage = {"input_tokens": 120, "output_tokens": 15}
    response = project_timeline_history(
        {
            "messages": [
                {"id": "a1", "role": "assistant", "content": "Answer", "usage": usage},
                {"id": "a2", "role": "assistant", "content": "Later text without usage"},
            ],
            "incremental": False,
        },
        child_limit=child_limit,
    )
    assert response["last_usage"] == usage
    if child_limit:
        assert response["messages"] == []
    for incremental in (False, True):
        empty = project_timeline_history(
            {"messages": [], "incremental": incremental}, child_limit=child_limit
        )
        if incremental:
            assert "last_usage" not in empty
        else:
            assert empty["last_usage"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_windowed_run_stream_and_child_pages_preserve_positions_and_generation(
    live: bool,
) -> None:
    active = run()
    messages = [ChatMessage.assistant(model="test/model", content=f"step {i}") for i in range(30)]
    speed = {"output_tokens_per_second": 50, "first_token_ms": 900, "basis": "output"}
    latency = {"first_token_ms": 20, "duration_ms": 25}
    messages[0] = replace(messages[0], usage={"speed": speed})
    messages[-1] = replace(messages[-1], usage={"speed": latency})
    for message in messages:
        active.emit("assistant_output", {"message": message.to_dict()})

    class Session:
        generation_id = "generation"

        async def load_run_messages_async(self, _run_id: str) -> list[ChatMessage]:
            return messages

    class Sessions:
        async def get_async(self, address: SessionAddress) -> Session:
            assert address == SessionAddress(None, "coder", "session-one")
            return Session()

    state = SimpleNamespace(
        file_delivery=None,
        runtime=SimpleNamespace(chat_sessions=Sessions()),
        chat_runs=SimpleNamespace(active_run=lambda **_kwargs: active if live else None),
    )
    if not live:
        active.mark_completed(None)
    async with aclosing(
        sse_timeline(state, active, child_limit=3, batch_interval_seconds=0)
    ) as stream:
        initial = data(await anext(stream))
        assert initial["state"]["output_speed"] == {
            **speed,
            "model": "test/model",
            "message_id": messages[0].id,
        }
        assert initial["state"]["output_latency"] == {
            **latency,
            "model": "test/model",
            "message_id": messages[-1].id,
        }
        children = initial["items"][0]["items"]
        gap = children[0]
        assert gap["type"] == "history_gap"
        assert [child["position"] for child in children[1:]] == [27, 28, 29]
        page = await timeline_children(
            state,
            SessionAddress(None, "coder", "session-one"),
            active.id,
            gap["before"],
            4,
            "generation",
        )
        assert [child["position"] for child in page["items"][1:]] == [23, 24, 25, 26]
        assert (
            len(
                {
                    child["id"]
                    for child in children + page["items"]
                    if child["type"] != "history_gap"
                }
            )
            == 7
        )
        with pytest.raises(ValueError):
            await timeline_children(
                state,
                SessionAddress(None, "coder", "session-one"),
                active.id,
                gap["before"],
                4,
                "other-generation",
            )
        if live:
            active.emit(
                "assistant_output",
                {"message": ChatMessage.assistant(model="test/model", content="latest").to_dict()},
            )
            patch = data(await anext(stream))
            assert patch["windowed"] is True
            assert "order" not in patch
            assert len(patch["children"]) == 1
            assert patch["children"][0]["position"] == 30


def run() -> Run:
    return Run(
        run_id="run-one", agent_id="coder", session_id="session-one", event_retention_limit=4
    )


def data(frame: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: "))),
    )


def test_windowed_patch_updates_unloaded_children_and_moves_delivered_text() -> None:
    text = {"id": "text", "type": "assistant_output", "content": "Hello"}
    tool = {"id": "tool", "type": "tool_call", "status": "running"}

    def frame(sequence: int, children: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "run_id": "one",
            "sequence": sequence,
            "state": {},
            "terminal": False,
            "items": [{"id": "one", "type": "assistant_run", "items": children}],
        }

    known = {"text"}
    patch = window_timeline_update(
        frame(1, [text, tool]),
        frame(2, [{**tool, "status": "success"}, {**text, "content": "Hello world"}]),
        known,
    )
    assert patch["children"] == [{**tool, "status": "success", "position": 0}]
    assert patch["child_updates"] == [
        {"id": "text", "type": "assistant_output", "append": {"content": " world"}, "set": {}}
    ]
    assert patch["positions"] == {"text": 1}
    assert known == {"text", "tool"}


@pytest.mark.asyncio
@pytest.mark.parametrize("child_limit", [0, 3])
async def test_snapshot_survives_replay_eviction_and_patches_only_new_text(
    child_limit: int,
) -> None:
    active = run()
    state = SimpleNamespace(file_delivery=FileDelivery())
    active.emit("run_started")
    for _ in range(20):
        active.emit("assistant_output_delta", {"content_delta": "x" * 1000})
    assert len(active.events) == 4
    projection = active.timeline
    async with aclosing(
        sse_timeline(state, active, batch_interval_seconds=0, child_limit=child_limit)
    ) as stream:
        initial = data(await anext(stream))
        [row] = initial["items"]
        [child] = row["items"]
        assert child["content"] == "x" * 20_000
        assert "events" not in child
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
