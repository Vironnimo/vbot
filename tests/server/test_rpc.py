"""Tests for server RPC dispatcher and delegates."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatLoop, ChatRunManager, ChatSessionManager
from core.tools import ToolRegistry
from server.delegates import dispatch_rpc

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class StubAgent:
    id: str
    model: str = "openai/gpt-5.2"
    temperature: float = 0.1
    thinking_effort: str = ""
    allowed_tools: list[str] | None = None


class StubAgents:
    def __init__(self, agent: StubAgent) -> None:
        self._agent = agent

    def get(self, agent_id: str) -> StubAgent:
        if agent_id != self._agent.id:
            raise KeyError(agent_id)
        return self._agent


class StubProviders:
    def get(self, provider_id: str) -> object:
        if provider_id != "openai":
            raise KeyError(provider_id)
        return object()


class StubPrompts:
    def build_system_prompt(self, agent: StubAgent) -> str:
        return f"System for {agent.id}"

    def provider_tool_definitions(self, _agent: StubAgent) -> list[JsonObject]:
        return []


class StubAdapter:
    def __init__(self, responses: list[JsonObject] | None = None, *, block: bool = False) -> None:
        self._responses = responses or []
        self._block = block
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[JsonObject] = []

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.request_started.set()
        if self._block:
            await self.release.wait()
        if not self._responses:
            return {"content": "OK", "tool_calls": None}
        return self._responses.pop(0)

    def normalize_response(self, response: JsonObject) -> JsonObject:
        return response


class StubRuntime:
    def __init__(self, tmp_path: Path, adapter: StubAdapter) -> None:
        self.agents = StubAgents(StubAgent(id="coder", allowed_tools=["*"]))
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.system_prompts = StubPrompts()
        self.tools = ToolRegistry()
        self.providers = StubProviders()
        self.adapter = adapter
        self.chat_runs: ChatRunManager | None = None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def get_adapter(self, _provider_id: str) -> StubAdapter:
        return self.adapter


def make_state(tmp_path: Path, adapter: StubAdapter) -> SimpleNamespace:
    runtime = StubRuntime(tmp_path, adapter)
    chat_runs = ChatRunManager()
    runtime.chat_runs = chat_runs
    return SimpleNamespace(
        runtime=runtime,
        chat_runs=chat_runs,
        chat_loop=ChatLoop(runtime),
    )


@pytest.mark.asyncio
async def test_session_create_creates_explicit_session(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "session.create", "params": {"agent_id": "coder", "session_id": "session-one"}},
    )

    assert response == {
        "ok": True,
        "result": {"agent_id": "coder", "session_id": "session-one"},
    }
    assert state.runtime.chat_sessions.get("coder", "session-one").id == "session-one"


@pytest.mark.asyncio
async def test_chat_send_requires_existing_session(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {"agent_id": "coder", "session_id": "missing", "content": "Hi"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "session does not exist" in response["error"]["message"]


@pytest.mark.asyncio
async def test_chat_send_returns_collected_run_timeline_without_reasoning_meta(
    tmp_path: Path,
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
        "reasoning",
        "assistant_output",
        "run_completed",
    ]


@pytest.mark.asyncio
async def test_chat_stream_starts_run_and_returns_run_id_without_waiting(tmp_path: Path) -> None:
    adapter = StubAdapter(block=True)
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

    run_id = response["result"]["run_id"]
    adapter.release.set()
    await state.chat_runs.cancel(run_id)


@pytest.mark.asyncio
async def test_second_run_in_same_session_is_rejected_while_active(tmp_path: Path) -> None:
    adapter = StubAdapter(block=True)
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
    assert second_response["ok"] is False
    assert second_response["error"]["code"] == "active_run"

    adapter.release.set()
    await state.chat_runs.cancel(first_response["result"]["run_id"])


@pytest.mark.asyncio
async def test_chat_cancel_marks_running_run_cancelled(tmp_path: Path) -> None:
    adapter = StubAdapter(block=True)
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
async def test_dispatch_validates_unknown_method_and_required_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    unknown = await dispatch_rpc(state, {"method": "unknown", "params": {}})
    missing = await dispatch_rpc(state, {"method": "session.create", "params": {}})

    assert unknown["error"]["code"] == "method_not_found"
    assert missing["error"]["code"] == "invalid_request"
