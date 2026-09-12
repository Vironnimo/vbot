"""Tests for rpc chat."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
    ReplySurface,
)
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.tools import FileReadState, register_read_tool
from server.rpc import (
    chat_methods,
)
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    StubDelegateRun,
    make_state,
)
from tests.server.rpc_test_support import _no_models_dev_fetch as _no_models_dev_fetch


@pytest.mark.asyncio
async def test_chat_send_accepts_content_block_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    captured: JsonObject = {}
    run = StubDelegateRun(
        run_id="run-list-send",
        agent_id="coder",
        session_id="session-one",
        status="completed",
        final_message=ChatMessage.assistant(model="openai/gpt-5.2", content="Done"),
    )

    async def fake_start_run(
        agent_id: str,
        content: str | list[Any],
        *,
        session_id: str,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
    ) -> StubDelegateRun:
        captured["agent_id"] = agent_id
        captured["content"] = content
        captured["session_id"] = session_id
        captured["reply_surface"] = reply_surface
        return run

    monkeypatch.setattr(state.chat_loop, "start_run", fake_start_run)
    monkeypatch.setattr(chat_methods, "_bridge_run_to_event_bus", lambda _state, _run: None)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": [
                    {"type": "text", "text": "Please inspect this image."},
                    {
                        "type": "media",
                        "attachment_id": "att-123",
                        "filename": "screen.png",
                        "media_type": "image/png",
                    },
                ],
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["status"] == "completed"
    assert captured == {
        "agent_id": "coder",
        "session_id": "session-one",
        "reply_surface": ReplySurface.webui(),
        "content": [
            TextBlock(type="text", text="Please inspect this image."),
            MediaBlock(
                type="media",
                attachment_id="att-123",
                filename="screen.png",
                media_type="image/png",
            ),
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_forward_speech_transcription_input_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    captured: JsonObject = {}
    run = StubDelegateRun(
        run_id="run-speech-origin",
        agent_id="coder",
        session_id="session-one",
        status="running" if method == "chat.stream" else "completed",
        final_message=ChatMessage.assistant(model="openai/gpt-5.2", content="Done"),
    )

    async def fake_start_run(
        agent_id: str,
        content: str | list[Any],
        *,
        session_id: str,
        input_origin: str | None = None,
        reply_surface: ReplySurface | None = None,
        project_id: str | None = None,
    ) -> StubDelegateRun:
        captured["agent_id"] = agent_id
        captured["content"] = content
        captured["session_id"] = session_id
        captured["input_origin"] = input_origin
        captured["reply_surface"] = reply_surface
        return run

    class StubStreamingLoop:
        async def start_run(
            self,
            agent_id: str,
            content: str | list[Any],
            *,
            session_id: str,
            input_origin: str | None = None,
            reply_surface: ReplySurface | None = None,
            project_id: str | None = None,
        ) -> StubDelegateRun:
            return await fake_start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
                reply_surface=reply_surface,
            )

    monkeypatch.setattr(state.chat_loop, "start_run", fake_start_run)
    monkeypatch.setattr(chat_methods, "_streaming_chat_loop", lambda _state: StubStreamingLoop())
    monkeypatch.setattr(chat_methods, "_bridge_run_to_event_bus", lambda _state, _run: None)

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "helo wrld",
                "input_origin": "speech_transcription",
            },
        },
    )

    assert response["ok"] is True
    assert captured == {
        "agent_id": "coder",
        "session_id": "session-one",
        "content": "helo wrld",
        "input_origin": "speech_transcription",
        "reply_surface": ReplySurface.webui(),
    }


@pytest.mark.asyncio
async def test_chat_stream_accepts_content_block_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    captured: JsonObject = {}
    run = StubDelegateRun(
        run_id="run-list-stream",
        agent_id="coder",
        session_id="session-one",
        status="running",
    )

    class StubStreamingLoop:
        async def start_run(
            self,
            agent_id: str,
            content: str | list[Any],
            *,
            session_id: str,
            reply_surface: ReplySurface | None = None,
            project_id: str | None = None,
        ) -> StubDelegateRun:
            captured["agent_id"] = agent_id
            captured["content"] = content
            captured["session_id"] = session_id
            captured["reply_surface"] = reply_surface
            return run

    monkeypatch.setattr(chat_methods, "_streaming_chat_loop", lambda _state: StubStreamingLoop())
    monkeypatch.setattr(chat_methods, "_bridge_run_to_event_bus", lambda _state, _run: None)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": [
                    {"type": "text", "text": "Review this document."},
                    {
                        "type": "file",
                        "attachment_id": "att-456",
                        "filename": "report.pdf",
                        "media_type": "application/pdf",
                    },
                ],
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["status"] == "running"
    assert response["result"]["sse_url"] == "/api/runs/run-list-stream/events"
    assert captured == {
        "agent_id": "coder",
        "session_id": "session-one",
        "reply_surface": ReplySurface.webui(),
        "content": [
            TextBlock(type="text", text="Review this document."),
            FileBlock(
                type="file",
                attachment_id="att-456",
                filename="report.pdf",
                media_type="application/pdf",
            ),
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_reject_invalid_content_type(
    tmp_path: Path,
    method: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": 123,
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_reject_invalid_input_origin(
    tmp_path: Path,
    method: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "Hi",
                "input_origin": "paste",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "input_origin" in response["error"]["message"]


@pytest.mark.asyncio
async def test_chat_send_returns_collected_run_timeline_without_reasoning_meta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": "Readable thinking",
                "reasoning_meta": {"secret": "opaque"},
                "tool_calls": None,
            }
        ]
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["status"] == "completed"
    assert result["message"]["content"] == "Hello"
    assert "reasoning_meta" not in result["message"]
    assert [event["type"] for event in result["events"]] == [
        "run_started",
        "user_message_persisted",
        "model_step_usage",
        "reasoning",
        "assistant_output",
        "run_completed",
    ]
    assert "reasoning_meta" not in str(result["events"])


@pytest.mark.asyncio
async def test_chat_send_collected_timeline_includes_read_tool_result_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning_meta": {"secret": "opaque"},
                "tool_calls": [
                    {"id": "call_read", "name": "read", "arguments": {"path": "note.txt"}}
                ],
            },
            {"content": "Read the file", "tool_calls": None},
        ]
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    register_read_tool(
        state.runtime.tools,
        attachment_store=None,
        speech_service=None,
        file_state=FileReadState(),
        speech_max_size_bytes=20_971_520,
    )
    state.runtime.agents.update("coder", workspace=str(tmp_path / "workspace"))
    workspace = Path(state.runtime.agents.get("coder").workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("note.txt").write_text("rpc content", encoding="utf-8")
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Read note"},
        },
    )

    assert response["ok"] is True
    result = response["result"]
    tool_started = next(event for event in result["events"] if event["type"] == "tool_call_started")
    tool_result = next(event for event in result["events"] if event["type"] == "tool_call_result")
    fingerprint = state.runtime.tools.schema_fingerprint("read")
    started_payload = dict(tool_started["payload"])
    display = started_payload.pop("display")
    assert started_payload == {
        "tool_call": {
            "id": "call_read",
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
    assert tool_result["payload"]["tool_call"] == {
        "id": "call_read",
        "index": 0,
        "name": "read",
    }
    assert tool_result["payload"]["result"] == {
        "ok": True,
        "error": None,
        "data": {"content": "1| rpc content"},
        "artifacts": [],
    }
    assert tool_result["payload"]["schema_fingerprint"] == fingerprint
    assert tool_result["payload"]["error_code"] is None
    assert "path" not in tool_result["payload"]["result"]["data"]
    assert "reasoning_meta" not in str(result["events"])
    assert "batch" not in str(result["events"])


@pytest.mark.asyncio
async def test_chat_stream_starts_run_and_returns_run_id_without_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "content_delta", "text": "OK"},
            {"type": "finish", "reason": "stop"},
        ],
        block=True,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
        },
    )
    await adapter.request_started.wait()

    assert response["ok"] is True
    assert response["result"]["status"] == "running"
    assert response["result"]["sse_url"].startswith("/api/runs/")
    assert len(adapter.requests) == 0
    assert len(adapter.stream_requests) == 1

    run_id = response["result"]["run_id"]
    adapter.release.set()
    await state.chat_runs.cancel(run_id)


@pytest.mark.asyncio
async def test_second_run_in_same_session_is_queued_while_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "content_delta", "text": "OK"},
            {"type": "finish", "reason": "stop"},
        ],
        block=True,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    first_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "First"},
        },
    )
    await adapter.request_started.wait()

    second_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Second"},
        },
    )

    assert first_response["ok"] is True
    assert second_response["ok"] is True
    assert second_response["result"]["queued"] is True
    queued_item = second_response["result"]["item"]
    assert queued_item["content"] == "Second"
    assert isinstance(queued_item["id"], str)
    assert queued_item["id"]
    assert len(adapter.stream_requests) == 1

    removed = state.chat_runs.remove_queued(
        "coder", "session-one", queued_item["id"], project_id=None
    )
    assert removed is True

    run = state.chat_runs.get(first_response["result"]["run_id"])
    adapter.release.set()
    await run.wait()


@pytest.mark.asyncio
async def test_chat_cancel_marks_running_run_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "content_delta", "text": "OK"},
            {"type": "finish", "reason": "stop"},
        ],
        block=True,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    stream_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
        },
    )
    await adapter.request_started.wait()

    cancel_response = await dispatch_rpc(
        state,
        {"method": "chat.cancel", "params": {"run_id": stream_response["result"]["run_id"]}},
    )
    adapter.release.set()

    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_chat_send_uses_non_streaming_chat_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter([{"content": "Complete response", "tool_calls": None}])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["message"]["content"] == "Complete response"
    assert len(adapter.requests) == 1
    assert len(adapter.stream_requests) == 0


@pytest.mark.asyncio
async def test_chat_stream_uses_streaming_chat_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "content_delta", "text": "Streamed response"},
            {"type": "finish", "reason": "stop"},
        ]
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
        },
    )
    run = state.chat_runs.get(response["result"]["run_id"])
    final_message = await run.wait()

    assert response["ok"] is True
    assert final_message.content == "Streamed response"
    assert len(adapter.requests) == 0
    assert len(adapter.stream_requests) == 1
    assert response["result"]["sse_url"] == f"/api/runs/{run.id}/events"


@pytest.mark.asyncio
async def test_chat_stream_uses_state_streaming_chat_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    captured: JsonObject = {}
    run = StubDelegateRun(
        run_id="runtime-stream-loop",
        agent_id="coder",
        session_id="session-one",
        status="running",
    )

    class RuntimeStreamingLoop:
        async def start_run(
            self,
            agent_id: str,
            content: str | list[Any],
            *,
            session_id: str,
            reply_surface: ReplySurface | None = None,
            project_id: str | None = None,
        ) -> StubDelegateRun:
            captured["agent_id"] = agent_id
            captured["content"] = content
            captured["session_id"] = session_id
            captured["reply_surface"] = reply_surface
            return run

    runtime_streaming_loop = RuntimeStreamingLoop()
    state.streaming_chat_loop = runtime_streaming_loop
    monkeypatch.setattr(chat_methods, "_bridge_run_to_event_bus", lambda _state, _run: None)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["run_id"] == "runtime-stream-loop"
    assert captured == {
        "agent_id": "coder",
        "content": "Hi",
        "session_id": "session-one",
        "reply_surface": ReplySurface.webui(),
    }
    assert state.streaming_chat_loop is runtime_streaming_loop
