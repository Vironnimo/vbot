"""Server RPC end-to-end integration tests with a stub runtime."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.chat import ChatLoop, ChatSessionManager
from core.models import Capabilities, Model, ReasoningCapabilities
from core.models.query import ModelQuery
from core.runs import ChatRunManager
from core.tools import ToolContext, ToolRegistry, tool_success
from server.app import create_app
from server.delegates import dispatch_rpc

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class IntegrationAgent:
    id: str
    model: str = "openai/gpt-5.2"
    connection: str = "openai:api-key"
    temperature: float = 0.2
    thinking_effort: str = "medium"
    allowed_tools: list[str] | None = None


class IntegrationAgents:
    def __init__(self, agent: IntegrationAgent) -> None:
        self._agent = agent

    def get(self, agent_id: str) -> IntegrationAgent:
        if agent_id != self._agent.id:
            raise KeyError(agent_id)
        return self._agent


class IntegrationProviders:
    def __init__(self) -> None:
        self._providers = {
            "anthropic": IntegrationProvider(
                id="anthropic",
                name="Anthropic",
                base_url="https://api.anthropic.com/v1",
                connections=[
                    IntegrationConnection(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=IntegrationAuth(credential_key="ANTHROPIC_API_KEY"),
                    )
                ],
            ),
            "openai": IntegrationProvider(
                id="openai",
                name="OpenAI",
                base_url="https://api.openai.com/v1",
                connections=[
                    IntegrationConnection(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=IntegrationAuth(credential_key="OPENAI_API_KEY"),
                    ),
                    IntegrationConnection(
                        id="subscription",
                        type="oauth",
                        label="ChatGPT Plus/Pro",
                        auth=IntegrationAuth(credential_key="OPENAI_OAUTH_TOKEN"),
                    ),
                ],
            ),
        }

    def get(self, provider_id: str) -> object:
        if provider_id not in self._providers:
            raise KeyError(provider_id)
        return self._providers[provider_id]

    def list_ids(self) -> list[str]:
        return sorted(self._providers)


@dataclass(frozen=True)
class IntegrationAuth:
    credential_key: str


@dataclass(frozen=True)
class IntegrationConnection:
    id: str
    type: str
    label: str
    auth: IntegrationAuth


@dataclass(frozen=True)
class IntegrationProvider:
    id: str
    name: str
    base_url: str
    connections: list[IntegrationConnection]


class IntegrationModels:
    def __init__(self) -> None:
        self._models = {
            "anthropic": [
                Model(
                    model_id="claude-sonnet-4-20250219",
                    name="Claude Sonnet 4",
                    capabilities=Capabilities(
                        vision=True,
                        tools=True,
                        json_mode=False,
                        reasoning=ReasoningCapabilities(supported=True),
                    ),
                    context_window=200000,
                    max_output_tokens=64000,
                )
            ],
            "openai": [
                Model(
                    model_id="gpt-5.2",
                    name="GPT-5.2",
                    capabilities=Capabilities(
                        vision=True,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(supported=True),
                    ),
                    context_window=256000,
                    max_output_tokens=32000,
                    connections=("api-key",),
                )
            ],
        }

    def list_for_provider(self, provider_id: str) -> list[Model]:
        return list(self._models[provider_id])

    def query(self, model_query: ModelQuery) -> list[tuple[str, Model]]:
        results: list[tuple[str, Model]] = []
        for provider_id, models in self._models.items():
            if model_query.provider_id is not None and provider_id != model_query.provider_id:
                continue
            for model in models:
                if model_query.matches(model):
                    results.append((provider_id, model))
        results.sort(key=lambda item: (item[0], item[1].model_id))
        return results


class IntegrationStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def load_appearance_settings(self) -> JsonObject:
        return {"language": "en"}

    def supported_appearance_languages(self) -> list[str]:
        return ["en"]

    def load_subagent_settings(self) -> JsonObject:
        return {
            "max_subagent_depth": 4,
            "max_subagents_per_turn": 8,
            "subagent_timeout_minutes": 60,
        }

    def load_compaction_settings(self) -> JsonObject:
        return {
            "auto": True,
            "threshold": 0.8,
            "tail_tokens": 15000,
            "summary_model": None,
        }

    def load_recall_settings(self) -> JsonObject:
        return {"backend": "jsonl_scan"}

    def load_web_search_settings(self) -> JsonObject:
        return {
            "provider": "brave",
            "searxng": {"base_url": "http://localhost:8888"},
        }

    def load_defaults(self) -> JsonObject:
        return {}

    def load_debug_settings(self) -> JsonObject:
        return {"enabled": False, "trace_limit": 50}

    def load_model_task_settings(self) -> JsonObject:
        return {}


class IntegrationPrompts:
    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    def build_system_prompt(self, agent: IntegrationAgent) -> str:
        return f"System prompt for {agent.id}"

    def provider_tool_definitions(self, agent: IntegrationAgent) -> list[JsonObject]:
        return self._tools.provider_definitions(agent.allowed_tools)


class SequencedAdapter:
    def __init__(
        self,
        responses: list[JsonObject] | None = None,
        *,
        block: bool = False,
    ) -> None:
        self._responses = responses or []
        self._block = block
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.requests: list[JsonObject] = []
        self.stream_requests: list[JsonObject] = []

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

    async def stream(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.request_started.set()
        if self._block:
            await self.release.wait()
        if not self._responses:
            yield {"type": "content_delta", "text": "OK"}
            yield {"type": "finish", "reason": "stop"}
            return
        response = self._responses.pop(0)
        for delta in _response_to_stream_deltas(response):
            yield delta

    async def aclose(self) -> None:
        self.closed = True


class AdapterPool:
    def __init__(self, adapters: list[SequencedAdapter]) -> None:
        self._adapters = adapters
        self._index = 0

    def next(self) -> SequencedAdapter:
        if self._index >= len(self._adapters):
            raise AssertionError("unexpected adapter request")
        adapter = self._adapters[self._index]
        self._index += 1
        return adapter


class IntegrationRuntime:
    def __init__(
        self,
        tmp_path: Path,
        adapters: SequencedAdapter | list[SequencedAdapter],
        *,
        configured_provider_ids: set[str] | None = None,
    ) -> None:
        self.tools = ToolRegistry()
        self.agents = IntegrationAgents(
            IntegrationAgent(id="coder", allowed_tools=["lookup", "slow_tool"])
        )
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.storage = IntegrationStorage(tmp_path)
        self.system_prompts = IntegrationPrompts(self.tools)
        self.providers = IntegrationProviders()
        self.models = IntegrationModels()
        adapter_list = adapters if isinstance(adapters, list) else [adapters]
        self._adapter_pool = AdapterPool(adapter_list)
        self._configured_provider_ids = configured_provider_ids or {"openai"}
        self.chat_runs: ChatRunManager | None = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def get_adapter(self, _provider_id: str, _connection_id: str) -> SequencedAdapter:
        return self._adapter_pool.next()

    def has_provider_credentials(self, provider_id: str) -> bool:
        return provider_id in self._configured_provider_ids

    @property
    def provider_credentials(self) -> Any:
        runtime = self

        class CredentialResolver:
            def has_credentials(self, provider_id: str, _connection_id: str | None = None) -> bool:
                return runtime.has_provider_credentials(provider_id)

        return CredentialResolver()


def test_model_list_and_settings_get_follow_credential_contract(tmp_path: Path) -> None:
    runtime = IntegrationRuntime(tmp_path, SequencedAdapter(), configured_provider_ids={"openai"})
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        model_response = client.post("/api/rpc", json={"method": "model.list", "params": {}})
        settings_response = client.post("/api/rpc", json={"method": "settings.get", "params": {}})

    assert model_response.json() == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "openai/gpt-5.2",
                    "provider_id": "openai",
                    "model_id": "gpt-5.2",
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 256000,
                    "max_output_tokens": 32000,
                    "connections": ["api-key"],
                }
            ]
        },
    }
    assert settings_response.json() == {
        "ok": True,
        "result": {
            "general": {
                "server": {
                    "listen_host": "127.0.0.1",
                    "listen_port": 8420,
                    "port_source": "default",
                },
                "data_directory": str(tmp_path),
            },
            "providers": {
                "items": [
                    {
                        "id": "anthropic",
                        "name": "Anthropic",
                        "base_url": "https://api.anthropic.com/v1",
                        "models_endpoint": None,
                        "connections": [
                            {
                                "id": "anthropic:api-key",
                                "type": "api_key",
                                "label": "API Key",
                                "configured": False,
                            }
                        ],
                        "credentials_configured": False,
                        "status": "missing_credentials",
                        "model_count": 1,
                        "kind": "remote",
                        "editable": False,
                    },
                    {
                        "id": "openai",
                        "name": "OpenAI",
                        "base_url": "https://api.openai.com/v1",
                        "models_endpoint": None,
                        "connections": [
                            {
                                "id": "openai:api-key",
                                "type": "api_key",
                                "label": "API Key",
                                "configured": True,
                            },
                            {
                                "id": "openai:subscription",
                                "type": "oauth",
                                "label": "ChatGPT Plus/Pro",
                                "configured": True,
                                "connectable": False,
                            },
                        ],
                        "credentials_configured": True,
                        "status": "configured",
                        "model_count": 1,
                        "kind": "remote",
                        "editable": False,
                    },
                ],
                "custom_endpoints": {"supported": False, "items": []},
            },
            "appearance": {"language": "en", "available_languages": ["en"]},
            "subagents": {
                "max_subagent_depth": 4,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
            },
            "compaction": {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15000,
                "summary_model": None,
            },
            "recall": {
                "backend": "jsonl_scan",
                "available_backends": ["hybrid", "jsonl_scan", "sqlite_fts", "vector"],
            },
            "web_search": {
                "provider": "brave",
                "available_providers": ["brave", "searxng"],
                "searxng": {"base_url": "http://localhost:8888"},
            },
            "defaults": {},
            "debug": {
                "enabled": False,
                "trace_limit": 50,
                "trace_count": 0,
            },
            "model_tasks": {},
        },
    }
    assert "env_key" not in json.dumps(settings_response.json())
    assert "missing_api_key" not in json.dumps(settings_response.json())


def test_http_session_create_send_sse_and_jsonl_persistence(tmp_path: Path) -> None:
    adapter = SequencedAdapter(
        [
            {
                "content": None,
                "reasoning": "Need the lookup tool.",
                "reasoning_meta": {"encrypted_content": "opaque"},
                "tool_calls": [
                    {"id": "call_lookup", "name": "lookup", "arguments": {"query": "vBot"}}
                ],
            },
            {"content": "Lookup complete.", "tool_calls": None},
        ]
    )
    runtime = IntegrationRuntime(tmp_path, adapter)
    runtime.tools.register(
        "lookup",
        "Look up a value.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"result": f"found {arguments['query']}"}),
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
        send_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.send",
                "params": {"agent_id": "coder", "session_id": "session-one", "content": "Go"},
            },
        )
        send_result = send_response.json()["result"]
        sse_response = client.get(f"/api/runs/{send_result['run_id']}/events")

    assert create_response.json() == {
        "ok": True,
        "result": {"agent_id": "coder", "session_id": "session-one"},
    }
    assert send_response.json()["ok"] is True
    assert send_result["status"] == "completed"
    assert send_result["message"]["content"] == "Lookup complete."
    assert [event["type"] for event in send_result["events"]] == [
        "run_started",
        "user_message_persisted",
        "reasoning",
        "tool_call_started",
        "tool_call_result",
        "assistant_output",
        "run_completed",
    ]
    assert "reasoning_meta" not in json.dumps(send_result)
    assert "reasoning_meta" not in sse_response.text
    assert [event["event"] for event in _parse_sse(sse_response.text)] == [
        "run_started",
        "user_message_persisted",
        "reasoning",
        "tool_call_started",
        "tool_call_result",
        "assistant_output",
        "run_completed",
    ]

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert messages[-1].status == "completed"
    assert messages[-1].timing is not None
    assert messages[1].reasoning_meta == {"encrypted_content": "opaque"}
    tool_message_content = messages[2].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == {
        "ok": True,
        "error": None,
        "data": {"result": "found vBot"},
        "artifacts": [],
    }
    assert adapter.closed is True


def test_http_stream_sse_replays_visible_running_timeline(tmp_path: Path) -> None:
    adapter = SequencedAdapter(
        [
            {
                "content": "Streamed final.",
                "reasoning": "Readable thinking.",
                "reasoning_meta": {"secret": "hidden"},
                "tool_calls": None,
            }
        ]
    )
    runtime = IntegrationRuntime(tmp_path, adapter)
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
        stream_result = stream_response.json()["result"]
        sse_response = client.get(stream_result["sse_url"])

    assert stream_response.json()["ok"] is True
    assert stream_result["status"] == "running"
    events = _parse_sse(sse_response.text)
    assert [event["event"] for event in events] == [
        "run_started",
        "user_message_persisted",
        "reasoning_delta",
        "assistant_output_delta",
        "reasoning",
        "assistant_output",
        "run_completed",
    ]
    assert events[2]["data"]["payload"]["reasoning_delta"] == "Readable thinking."
    assert events[3]["data"]["payload"]["content_delta"] == "Streamed final."
    assert events[4]["data"]["payload"]["message"]["reasoning"] == "Readable thinking."
    assert events[5]["data"]["payload"]["message"]["content"] == "Streamed final."
    assert "reasoning_meta" not in sse_response.text


@pytest.mark.asyncio
async def test_cancel_suppresses_late_output_and_prevents_new_tool_steps(tmp_path: Path) -> None:
    adapter = SequencedAdapter(
        [
            {
                "content": None,
                "reasoning": "Need slow work.",
                "tool_calls": [
                    {"id": "call_slow", "name": "slow_tool", "arguments": {"value": "late"}}
                ],
            },
            {"content": "Should not be requested", "tool_calls": None},
        ]
    )
    runtime = IntegrationRuntime(tmp_path, adapter)
    state = _make_state(runtime)
    slow_tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    tool_results: list[JsonObject] = []

    async def slow_tool(context: ToolContext, arguments: JsonObject) -> JsonObject:
        slow_tool_started.set()
        while not context.is_cancelled():
            await asyncio.sleep(0)
        await release_tool.wait()
        result = tool_success({"value": arguments["value"]})
        tool_results.append(result)
        return result

    runtime.tools.register("slow_tool", "Slow tool.", {"type": "object"}, slow_tool)
    runtime.chat_sessions.create("coder", session_id="session-one")
    stream_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Start"},
        },
    )
    await slow_tool_started.wait()

    cancel_response = await dispatch_rpc(
        state,
        {"method": "chat.cancel", "params": {"run_id": stream_response["result"]["run_id"]}},
    )
    release_tool.set()
    await asyncio.sleep(0)

    run = state.chat_runs.get(stream_response["result"]["run_id"])
    messages = runtime.chat_sessions.get("coder", "session-one").load()

    assert cancel_response["ok"] is True
    assert cancel_response["result"]["status"] == "cancelled"
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        "reasoning_delta",
        "tool_call_delta",
        "tool_call_delta",
        "reasoning",
        "assistant_output",
        "tool_call_started",
        "run_cancelled",
    ]
    assert [message.role for message in messages] == ["user", "assistant", "run_summary"]
    assert messages[-1].status == "cancelled"
    assert messages[-1].timing is not None
    assert tool_results == []
    assert len(adapter.stream_requests) == 1


@pytest.mark.asyncio
async def test_same_session_queued_while_different_sessions_run_in_parallel(
    tmp_path: Path,
) -> None:
    first_adapter = SequencedAdapter(block=True)
    second_adapter = SequencedAdapter([{"content": "Second done", "tool_calls": None}])
    runtime = IntegrationRuntime(tmp_path, [first_adapter, second_adapter])
    state = _make_state(runtime)
    runtime.chat_sessions.create("coder", session_id="session-one")
    runtime.chat_sessions.create("coder", session_id="session-two")

    first_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "First"},
        },
    )
    await first_adapter.request_started.wait()
    same_session_response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Again"},
        },
    )
    parallel_response = await dispatch_rpc(
        state,
        {
            "method": "chat.send",
            "params": {"agent_id": "coder", "session_id": "session-two", "content": "Parallel"},
        },
    )

    assert first_response["ok"] is True
    assert same_session_response["ok"] is True
    assert same_session_response["result"]["queued"] is True
    queued_item = same_session_response["result"]["item"]
    assert queued_item["content"] == "Again"
    assert isinstance(queued_item["id"], str)
    assert queued_item["id"]
    assert parallel_response["ok"] is True
    assert parallel_response["result"]["message"]["content"] == "Second done"

    removed = state.chat_runs.remove_queued("coder", "session-one", queued_item["id"])
    assert removed is True

    run = state.chat_runs.get(first_response["result"]["run_id"])
    first_adapter.release.set()
    await run.wait()


def _make_state(runtime: IntegrationRuntime) -> Any:
    chat_runs = ChatRunManager()
    runtime.chat_runs = chat_runs
    return type(
        "IntegrationState",
        (),
        {
            "runtime": runtime,
            "chat_runs": chat_runs,
            "chat_loop": ChatLoop(runtime),
            "event_bus": None,
        },
    )()


def _parse_sse(body: str) -> list[JsonObject]:
    events: list[JsonObject] = []
    for block in body.strip().split("\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        fields = dict(line.split(": ", 1) for line in lines)
        event_name = fields["event"]
        data = json.loads(fields["data"])
        events.append({"event": event_name, "data": data})
    return events


def _response_to_stream_deltas(response: JsonObject) -> list[JsonObject]:
    deltas: list[JsonObject] = []
    reasoning = response.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        deltas.append({"type": "reasoning_delta", "text": reasoning})
    reasoning_meta = response.get("reasoning_meta")
    if isinstance(reasoning_meta, dict):
        deltas.append({"type": "reasoning_meta", "reasoning_meta": reasoning_meta})
    for tool_call in response.get("tool_calls") or []:
        deltas.extend(_tool_call_to_stream_deltas(tool_call))
    if response.get("tool_calls"):
        deltas.append({"type": "finish", "reason": "tool_calls"})
    content = response.get("content")
    if isinstance(content, str) and content:
        deltas.append({"type": "content_delta", "text": content})
    if not response.get("tool_calls"):
        deltas.append({"type": "finish", "reason": "stop"})
    return deltas


def _tool_call_to_stream_deltas(tool_call: JsonObject) -> list[JsonObject]:
    tool_call_id = cast(str, tool_call["id"])
    return [
        {"type": "tool_call_delta", "id": tool_call_id, "name_delta": tool_call["name"]},
        {
            "type": "tool_call_delta",
            "id": tool_call_id,
            "arguments_delta": json.dumps(
                tool_call["arguments"],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
