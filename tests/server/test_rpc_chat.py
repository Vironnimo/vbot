"""Server RPC chat handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
    ReplySurface,
    ToolCall,
)
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.chat.errors import ChatError
from core.sessions import SessionAddress
from core.tools import FileReadState, register_read_tool
from server.rpc import (
    chat_methods,
)
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    JsonObject,
    RecordingCompactionService,
    StubAdapter,
    StubDelegateRun,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_chat_history_loads_current_session_and_strips_reasoning_meta(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="current-one")
    state.runtime.agents.update("coder", current_session_id="current-one")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            reasoning="visible",
            reasoning_meta={"secret": "opaque"},
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    assert response["result"]["session_id"] == "current-one"
    assert response["result"]["messages"][0]["reasoning"] == "visible"
    assert "reasoning_meta" not in response["result"]["messages"][0]


@pytest.mark.asyncio
async def test_chat_history_includes_whole_session_usage_totals(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="usage-session")
    state.runtime.agents.update("coder", current_session_id="usage-session")
    session.append(ChatMessage.user(content="hello"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="One",
            usage={
                "input_tokens": 1000,
                "output_tokens": 50,
                "cache_read_tokens": 800,
                "reasoning_tokens": 20,
            },
        )
    )
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Two",
            usage={
                "input_tokens": 2000,
                "output_tokens": 100,
                "cache_read_tokens": 1500,
                "cache_write_tokens": 300,
                "reasoning_tokens": 40,
            },
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder", "limit": 1}},
    )

    assert response["ok"] is True
    # The page is a slice; the totals still cover the whole transcript.
    assert len(response["result"]["messages"]) == 1
    assert response["result"]["session_usage"] == {
        "measured_turns": 2,
        "estimated_turns": 0,
        "cache_turns": 2,
        "input_tokens": 3000,
        "output_tokens": 150,
        "cache_read_tokens": 2300,
        "cache_write_tokens": 300,
        "reasoning_turns": 2,
        "reasoning_tokens": 60,
    }
    assert response["result"]["context_usage"] == {
        "tokens": 2100,
        "estimated": False,
        "provider_input_tokens": 2000,
        "provider_output_tokens": 100,
    }


@pytest.mark.asyncio
async def test_chat_history_includes_active_run_descriptor(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="active-session")
    state.runtime.agents.update("coder", current_session_id="active-session")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_executor(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    active_run = await state.chat_runs.start(
        agent_id="coder", session_id="active-session", executor=_blocking_executor, project_id=None
    )
    await started.wait()

    try:
        response = await dispatch_rpc(
            state,
            {"method": "chat.history", "params": {"agent_id": "coder"}},
        )
    finally:
        release.set()
        await active_run.wait()

    assert response["ok"] is True
    active_run_payload = response["result"]["active_run"]
    assert active_run_payload["run_id"] == active_run.id
    assert active_run_payload["agent_id"] == "coder"
    assert active_run_payload["session_id"] == "active-session"
    assert active_run_payload["status"] == "running"
    assert active_run_payload["sse_url"] == f"/api/runs/{active_run.id}/events"
    assert [event["type"] for event in active_run_payload["events"]] == ["run_started"]


@pytest.mark.asyncio
async def test_chat_history_filters_internal_notes(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="note-session")
    state.runtime.agents.update("coder", current_session_id="note-session")
    session.append(ChatMessage.user(content="Visible request"))
    session.append(ChatMessage.note(content="Internal reminder"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Visible response",
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "Internal reminder" not in str(messages)


@pytest.mark.asyncio
async def test_chat_history_includes_compaction_checkpoints(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="compaction-session")
    state.runtime.agents.update("coder", current_session_id="compaction-session")
    user_message = ChatMessage.user(content="Visible request")
    session.append(user_message)
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted context summary",
            projection=[user_message],
            compacted_token_count=321,
        )
    )
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Visible response",
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "compaction_checkpoint",
        "assistant",
    ]
    checkpoint = messages[1]
    assert checkpoint["content"] == "Compacted context summary"
    assert checkpoint["projection"][1]["id"] == user_message.id
    assert checkpoint["usage"] == {"compacted_token_count": 321}


@pytest.mark.asyncio
async def test_chat_history_includes_usage_on_assistant_messages(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="usage-session")
    state.runtime.agents.update("coder", current_session_id="usage-session")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            usage={
                "input_tokens": 150,
                "output_tokens": 42,
                "cache_write_tokens": 12,
                "reasoning_tokens": 30,
            },
        )
    )
    session.append(
        ChatMessage.user(content="Follow-up"),
    )
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="World",
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert len(messages) == 3

    # Assistant message with usage includes it in the response
    assert messages[0]["usage"] == {
        "input_tokens": 150,
        "output_tokens": 42,
        "cache_write_tokens": 12,
        "reasoning_tokens": 30,
    }
    assert messages[0]["content"] == "Hello"

    # User message does not carry usage
    assert "usage" not in messages[1]

    # Assistant message without usage has no usage key
    assert "usage" not in messages[2]


@pytest.mark.asyncio
async def test_chat_history_includes_tool_timing_and_run_summary(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="timing-session")
    state.runtime.agents.update("coder", current_session_id="timing-session")
    timing = {
        "started_at": "2026-05-03T14:30:01+00:00",
        "completed_at": "2026-05-03T14:30:02+00:00",
        "duration_ms": 1000,
    }
    session.append(ChatMessage.user(content="Run this"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content=None,
            tool_calls=[ToolCall(id="call-one", name="read", arguments={"path": "a.txt"})],
        )
    )
    session.append(
        ChatMessage.tool(
            tool_call_id="call-one",
            name="read",
            content='{"ok":true,"error":null,"data":{},"artifacts":[]}',
            timing=timing,
        )
    )
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Done"))
    session.append(
        ChatMessage.run_summary(
            run_id="run-one", status="completed", timing=timing, iteration_count=1
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "chat.history", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    messages = response["result"]["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert messages[2]["timing"] == timing
    assert messages[4]["run_id"] == "run-one"
    assert messages[4]["status"] == "completed"
    assert messages[4]["timing"] == timing


@pytest.mark.asyncio
async def test_chat_send_requires_existing_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {"agent_id": "coder", "session_id": "missing", "content": "Hi"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"


@pytest.mark.asyncio
async def test_chat_commands_returns_normalized_built_in_command_names(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.commands",
            "params": {},
        },
    )

    assert response["ok"] is True
    command_names = [
        item["name"] for item in response["result"]["items"] if item.get("type") == "command"
    ]
    assert command_names == [
        "agent",
        "compact",
        "handoff",
        "help",
        "learn",
        "model",
        "new",
        "reflect",
        "rename",
        "status",
        "stop",
    ]
    assert all(not name.startswith("/") for name in command_names)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_new_command_with_session_payload(
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
                "content": "/new",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["command_handled"] is True
    assert result["data"]["command"] == "new"
    new_session_id = result["data"]["session_id"]
    assert isinstance(new_session_id, str)
    assert new_session_id != "session-one"
    assert state.runtime.agents.get("coder").current_session_id == new_session_id
    assert (
        state.runtime.chat_sessions.get(
            SessionAddress(project_id=None, agent_id="coder", session_id=new_session_id)
        ).load()
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_reject_compact_command_while_session_run_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService()
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_run_executor(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    active_run = await state.chat_runs.start(
        agent_id="coder", session_id="session-one", executor=_blocking_run_executor, project_id=None
    )
    await started.wait()

    try:
        response = await dispatch_rpc(
            state,
            {
                "method": method,
                "params": {
                    "agent_id": "coder",
                    "session_id": "session-one",
                    "content": " /COMPACT ",
                },
            },
        )
    finally:
        release.set()
        await active_run.wait()

    assert response["ok"] is True
    assert response["result"]["command_handled"] is True
    assert response["result"]["output"] == "toast"
    assert response["result"]["reply"]
    assert compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_compact_command_when_service_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter()
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": " /COMPACT ",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["command_handled"] is True
    assert response["result"]["output"] == "toast"
    assert response["result"]["reply"]
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
async def test_chat_stream_exposes_compact_command_model_errors_on_the_run(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService()
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", model="")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": " /COMPACT ",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["status"] == "running"
    assert result["sse_url"] == f"/api/runs/{result['run_id']}/events"
    run = state.chat_runs.get(result["run_id"])
    with pytest.raises(ChatError):
        await run.wait()
    assert [event.type for event in run.events] == [
        "run_started",
        "compaction_started",
        "compaction_aborted",
        "run_failed",
    ]
    assert compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
async def test_chat_send_returns_compact_command_run_failure(tmp_path: Path) -> None:
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService()
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", model="")

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": " /COMPACT ",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
async def test_chat_stream_returns_manual_compaction_run_with_checkpoint_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService()
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    session = state.runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Keep this context"))

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/compact keep the API design",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert "command_handled" not in result
    assert result["sse_url"] == f"/api/runs/{result['run_id']}/events"
    run = state.chat_runs.get(result["run_id"])
    checkpoint = await run.wait()
    assert checkpoint.role == "compaction_checkpoint"
    assert [event.type for event in run.events] == [
        "run_started",
        "compaction_started",
        "compaction_completed",
        "run_completed",
    ]
    completed = run.events[-2]
    assert completed.payload["message"]["content"] == "Compacted context"
    assert checkpoint.usage is not None
    assert completed.payload["context_tokens_before"] == checkpoint.usage["context_tokens_before"]
    assert completed.payload["context_tokens_after"] == checkpoint.usage["context_tokens_after"]
    assert completed.payload["context_tokens_after"] > 0
    assert completed.payload["checkpoint_id"] == checkpoint.id


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
