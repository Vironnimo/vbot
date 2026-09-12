"""Tests for rpc chat commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat.errors import ChatError
from core.sessions import SessionAddress
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    RecordingCompactionService,
    StubAdapter,
    make_state,
)
from tests.server.rpc_test_support import _no_models_dev_fetch as _no_models_dev_fetch


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
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
        _blocking_run_executor,
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
