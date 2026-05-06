"""Tests for server RPC dispatcher and delegates."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatLoop, ChatMessage, ChatRunManager, ChatSessionManager
from core.tools import ToolRegistry
from server.delegates import dispatch_rpc
from server.events import ServerEventBus

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class StubAgent:
    id: str
    name: str = "Coder Agent"
    model: str = "openai/gpt-5.2"
    fallback_model: str = ""
    workspace: str = "C:/workspace"
    temperature: float = 0.1
    thinking_effort: str = ""
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    current_session_id: str = ""
    created_at: str = "2026-05-04T00:00:00Z"
    updated_at: str = "2026-05-04T00:00:00Z"

    def __post_init__(self) -> None:
        if self.allowed_tools is None:
            object.__setattr__(self, "allowed_tools", ["*"])
        if self.allowed_skills is None:
            object.__setattr__(self, "allowed_skills", ["*"])


class StubAgents:
    def __init__(self, agent: StubAgent) -> None:
        self._agents: dict[str, StubAgent] = {agent.id: agent}

    def get(self, agent_id: str) -> StubAgent:
        if agent_id not in self._agents:
            raise KeyError(agent_id)
        return self._agents[agent_id]

    def list(self) -> list[StubAgent]:
        return [self._agents[agent_id] for agent_id in sorted(self._agents)]

    def create(self, agent_id: str, name: str, **changes: Any) -> StubAgent:
        agent = StubAgent(id=agent_id, name=name, **changes)
        self._agents[agent_id] = agent
        return agent

    def update(self, agent_id: str, **changes: Any) -> StubAgent:
        agent = self.get(agent_id)
        updated = StubAgent(**{**agent.__dict__, **changes})
        self._agents[agent_id] = updated
        return updated

    def delete(self, agent_id: str) -> Path:
        self.get(agent_id)
        del self._agents[agent_id]
        return Path("archive") / agent_id


class InstrumentedAgentDeleteLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.attempts = 0
        self.active = 0
        self.max_active = 0
        self._second_attempt = asyncio.Event()

    async def __aenter__(self) -> None:
        self.attempts += 1
        if self.attempts == 2:
            self._second_attempt.set()
        if self.attempts == 1:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._second_attempt.wait(), timeout=1)
        await self._lock.acquire()
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    async def __aexit__(self, *_exc_info: object) -> None:
        self.active -= 1
        self._lock.release()


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
        event_bus=ServerEventBus(),
        agent_delete_lock=asyncio.Lock(),
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
async def test_agent_crud_delegates_expose_current_session_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="current-one")
    state.runtime.agents.update("coder", current_session_id="current-one")

    list_response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})
    create_response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {"id": "writer", "name": "Writer", "model": "openai/gpt-5.2"},
        },
    )
    update_response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "writer", "name": "Updated Writer"}},
    )
    delete_response = await dispatch_rpc(
        state, {"method": "agent.delete", "params": {"id": "writer"}}
    )

    assert list_response["result"]["agents"][0]["current_session_id"] == "current-one"
    assert create_response["result"]["id"] == "writer"
    assert update_response["result"]["name"] == "Updated Writer"
    assert delete_response["result"]["agent_id"] == "writer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("agent.create", {"id": "writer", "name": "Writer", "allowed_tools": "read_file"}),
        (
            "agent.create",
            {"id": "writer", "name": "Writer", "allowed_tools": ["read_file", 1]},
        ),
        ("agent.create", {"id": "writer", "name": "Writer", "allowed_skills": "debugging"}),
        (
            "agent.create",
            {"id": "writer", "name": "Writer", "allowed_skills": ["debugging", None]},
        ),
        ("agent.create", {"id": "writer", "name": "Writer", "temperature": "0.7"}),
        ("agent.create", {"id": "writer", "name": "Writer", "temperature": -0.1}),
        ("agent.create", {"id": "writer", "name": "Writer", "temperature": 2.1}),
        ("agent.create", {"id": "writer", "name": "Writer", "thinking_effort": "extreme"}),
        ("agent.update", {"id": "coder", "allowed_tools": "read_file"}),
        ("agent.update", {"id": "coder", "allowed_tools": ["read_file", 1]}),
        ("agent.update", {"id": "coder", "allowed_skills": "debugging"}),
        ("agent.update", {"id": "coder", "allowed_skills": ["debugging", None]}),
        ("agent.update", {"id": "coder", "temperature": "0.7"}),
        ("agent.update", {"id": "coder", "temperature": -0.1}),
        ("agent.update", {"id": "coder", "temperature": 2.1}),
        ("agent.update", {"id": "coder", "thinking_effort": "extreme"}),
        ("agent.update", {"id": "coder", "name": ""}),
        ("agent.update", {"id": "coder", "model": 5}),
    ],
)
async def test_agent_rpc_rejects_malformed_mutable_payloads(
    tmp_path: Path,
    method: str,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("agent.create", {"id": "writer", "name": "Writer", "workspace": "C:/escape"}),
        ("agent.update", {"id": "coder", "workspace": "C:/escape"}),
    ],
)
async def test_agent_rpc_rejects_workspace_mutation(
    tmp_path: Path,
    method: str,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_workspace = state.runtime.agents.get("coder").workspace

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert state.runtime.agents.get("coder").workspace == original_workspace


@pytest.mark.asyncio
async def test_agent_delete_rejects_last_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "last_agent"


@pytest.mark.asyncio
async def test_agent_delete_serializes_minimum_one_check_and_delete(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    agent_delete_lock = InstrumentedAgentDeleteLock()
    state.agent_delete_lock = agent_delete_lock

    coder_delete, writer_delete = await asyncio.gather(
        dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}}),
        dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "writer"}}),
    )

    responses = [coder_delete, writer_delete]
    successes = [response for response in responses if response["ok"]]
    failures = [response for response in responses if not response["ok"]]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error"]["code"] == "last_agent"
    assert len(state.runtime.agents.list()) == 1
    assert len(successes[0]["result"]["remaining_agents"]) == 1
    assert agent_delete_lock.max_active == 1


@pytest.mark.asyncio
async def test_session_create_make_current_updates_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "session.create",
            "params": {"agent_id": "coder", "session_id": "current-two", "make_current": True},
        },
    )

    assert response["ok"] is True
    assert state.runtime.agents.get("coder").current_session_id == "current-two"


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
    assert "reasoning_meta" not in str(result["events"])


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


@pytest.mark.asyncio
async def test_agent_create_publishes_agent_created_event(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "agent.create", "params": {"id": "writer", "name": "Writer"}},
    )

    assert response["ok"] is True
    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "agent.created"
    assert event["payload"]["id"] == "writer"
    assert event["payload"]["name"] == "Writer"
    assert event["sequence"] == 1


@pytest.mark.asyncio
async def test_agent_update_publishes_agent_updated_event(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "name": "Updated Coder"}},
    )

    assert response["ok"] is True
    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "agent.updated"
    assert event["payload"]["id"] == "coder"
    assert event["payload"]["name"] == "Updated Coder"


@pytest.mark.asyncio
async def test_agent_delete_publishes_agent_deleted_event(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")

    response = await dispatch_rpc(
        state,
        {"method": "agent.delete", "params": {"id": "writer"}},
    )

    assert response["ok"] is True
    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "agent.deleted"
    assert event["payload"]["agent_id"] == "writer"
    assert len(event["payload"]["remaining_agents"]) == 1
    assert event["payload"]["remaining_agents"][0]["id"] == "coder"


@pytest.mark.asyncio
async def test_agent_crud_events_not_published_without_event_bus(tmp_path: Path) -> None:
    runtime = StubRuntime(tmp_path, StubAdapter())
    chat_runs = ChatRunManager()
    runtime.chat_runs = chat_runs
    state = SimpleNamespace(
        runtime=runtime,
        chat_runs=chat_runs,
        chat_loop=ChatLoop(runtime),
        agent_delete_lock=asyncio.Lock(),
    )
    # No event_bus attribute — should not crash

    response = await dispatch_rpc(
        state,
        {"method": "agent.create", "params": {"id": "writer", "name": "Writer"}},
    )

    assert response["ok"] is True
