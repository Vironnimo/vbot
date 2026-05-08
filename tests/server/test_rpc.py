"""Tests for server RPC dispatcher and delegates."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import ChatLoop, ChatMessage, ChatRunManager, ChatSessionManager
from core.models import Capabilities, Model, ReasoningCapabilities
from core.storage import StorageError
from core.tools import ToolRegistry, register_read_tool
from server.delegates import dispatch_rpc
from server.events import ServerEventBus

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class StubAgent:
    id: str
    name: str = "Coder Agent"
    model: str = "openai/gpt-5.2"
    fallback_model: str = ""
    connection: str = "openai:api-key"
    fallback_connection: str = ""
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
    def __init__(self) -> None:
        self._providers = {
            "anthropic": SimpleNamespace(
                id="anthropic",
                name="Anthropic",
                base_url="https://api.anthropic.com/v1",
                connections=[
                    SimpleNamespace(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=SimpleNamespace(credential_key="ANTHROPIC_API_KEY"),
                    )
                ],
            ),
            "openai": SimpleNamespace(
                id="openai",
                name="OpenAI",
                base_url="https://api.openai.com/v1",
                connections=[
                    SimpleNamespace(
                        id="oauth",
                        type="oauth",
                        label="OAuth",
                        auth=SimpleNamespace(credential_key="OPENAI_OAUTH_TOKEN"),
                    ),
                    SimpleNamespace(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=SimpleNamespace(credential_key="OPENAI_API_KEY"),
                    ),
                ],
            ),
            "ollama": SimpleNamespace(
                id="ollama",
                name="Ollama",
                base_url="",
                connections=[
                    SimpleNamespace(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=SimpleNamespace(credential_key="OLLAMA_API_KEY"),
                    )
                ],
            ),
        }

    def get(self, provider_id: str) -> object:
        if provider_id not in self._providers:
            raise KeyError(provider_id)
        return self._providers[provider_id]

    def list_ids(self) -> list[str]:
        return sorted(self._providers)


class StubModels:
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
                    model_id="gpt-4.1-mini",
                    name="GPT-4.1 mini",
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(supported=False),
                    ),
                    context_window=128000,
                    max_output_tokens=16000,
                ),
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
                ),
            ],
            "ollama": [
                Model(
                    model_id="llama3.2",
                    name="Llama 3.2",
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=False,
                        reasoning=ReasoningCapabilities(supported=False),
                    ),
                    context_window=128000,
                    max_output_tokens=8192,
                )
            ],
        }

    def list_for_provider(self, provider_id: str) -> list[object]:
        return list(self._models[provider_id])


class StubStorage:
    def __init__(self, tmp_path: Path) -> None:
        self.data_dir = tmp_path
        self._appearance = {"language": "en"}

    def load_appearance_settings(self) -> JsonObject:
        return dict(self._appearance)

    def supported_appearance_languages(self) -> list[str]:
        return ["en"]

    def update_appearance_settings(self, appearance: JsonObject) -> JsonObject:
        unsupported_fields = sorted(set(appearance) - {"language"})
        if unsupported_fields:
            raise StorageError(f"unsupported appearance settings: {', '.join(unsupported_fields)}")
        language = appearance.get("language")
        if not isinstance(language, str) or not language:
            raise StorageError("Appearance language must be a non-empty string")
        if language != "en":
            raise StorageError(f"Unsupported appearance language: {language}")
        self._appearance = {"language": language}
        return dict(self._appearance)


class StubPrompts:
    def build_system_prompt(self, agent: StubAgent) -> str:
        return f"System for {agent.id}"

    def provider_tool_definitions(self, _agent: StubAgent) -> list[JsonObject]:
        return []


class StubAdapter:
    def __init__(
        self,
        responses: list[JsonObject] | None = None,
        *,
        stream_deltas: list[JsonObject] | None = None,
        block: bool = False,
    ) -> None:
        self._responses = responses or []
        self._stream_deltas = stream_deltas or []
        self._block = block
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()
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
        deltas = self._next_stream_deltas()
        for delta in deltas:
            yield deepcopy(delta)

    def _next_stream_deltas(self) -> list[JsonObject]:
        if self._stream_deltas and isinstance(self._stream_deltas[0], list):
            return cast(list[JsonObject], self._stream_deltas.pop(0))
        return cast(list[JsonObject], self._stream_deltas)


class StubRuntime:
    def __init__(self, tmp_path: Path, adapter: StubAdapter) -> None:
        self.agents = StubAgents(StubAgent(id="coder", allowed_tools=["*"]))
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.system_prompts = StubPrompts()
        self.storage = StubStorage(tmp_path)
        self.tools = ToolRegistry()
        self.models = StubModels()
        self.providers = StubProviders()
        self.adapter = adapter
        self.chat_runs: ChatRunManager | None = None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def get_adapter(self, _provider_id: str, _connection_id: str) -> StubAdapter:
        return self.adapter

    def has_provider_credentials(self, provider_id: str) -> bool:
        provider = cast(Any, self.providers.get(provider_id))
        return any(
            bool(os.environ.get(connection.auth.credential_key))
            for connection in provider.connections
        )

    @property
    def provider_credentials(self) -> Any:
        runtime = self

        class CredentialResolver:
            def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
                provider = cast(Any, runtime.providers.get(provider_id))
                if connection_id is None:
                    return runtime.has_provider_credentials(provider_id)
                local_id = connection_id.removeprefix(f"{provider_id}:")
                connection = next(
                    connection for connection in provider.connections if connection.id == local_id
                )
                return bool(os.environ.get(connection.auth.credential_key))

        return CredentialResolver()


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
        server_bind={"listen_host": "127.0.0.1", "listen_port": 8420, "port_source": "default"},
    )


@pytest.mark.asyncio
async def test_settings_get_returns_normalized_settings_payload_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.server_bind = {
        "listen_host": "0.0.0.0",
        "listen_port": 9001,
        "port_source": "settings.server_port",
    }

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True
    assert response["result"] == {
        "general": {
            "server": {
                "listen_host": "0.0.0.0",
                "listen_port": 9001,
                "port_source": "settings.server_port",
            },
            "data_directory": str(tmp_path),
        },
        "providers": {
            "items": [
                {
                    "id": "anthropic",
                    "name": "Anthropic",
                    "base_url": "https://api.anthropic.com/v1",
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
                    "id": "ollama",
                    "name": "Ollama",
                    "base_url": "",
                    "connections": [
                        {
                            "id": "ollama:api-key",
                            "type": "api_key",
                            "label": "API Key",
                            "configured": False,
                        }
                    ],
                    "credentials_configured": False,
                    "status": "missing_credentials",
                    "model_count": 1,
                    "kind": "local",
                    "editable": False,
                },
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "connections": [
                        {
                            "id": "openai:oauth",
                            "type": "oauth",
                            "label": "OAuth",
                            "configured": False,
                        },
                        {
                            "id": "openai:api-key",
                            "type": "api_key",
                            "label": "API Key",
                            "configured": True,
                        },
                    ],
                    "credentials_configured": True,
                    "status": "configured",
                    "model_count": 2,
                    "kind": "remote",
                    "editable": False,
                },
            ],
            "custom_endpoints": {"supported": False, "items": []},
        },
        "appearance": {"language": "en", "available_languages": ["en"]},
    }
    assert "sk-live-secret" not in str(response)
    assert "show_token_counts" not in str(response)
    assert "origin" not in response["result"]["general"]["server"]


@pytest.mark.asyncio
async def test_settings_get_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {"extra": True}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_connection_list_returns_connections_with_usability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "connection.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "connections": [
                {
                    "id": "anthropic:api-key",
                    "provider_id": "anthropic",
                    "type": "api_key",
                    "label": "API Key",
                    "usable": False,
                },
                {
                    "id": "ollama:api-key",
                    "provider_id": "ollama",
                    "type": "api_key",
                    "label": "API Key",
                    "usable": False,
                },
                {
                    "id": "openai:oauth",
                    "provider_id": "openai",
                    "type": "oauth",
                    "label": "OAuth",
                    "usable": False,
                },
                {
                    "id": "openai:api-key",
                    "provider_id": "openai",
                    "type": "api_key",
                    "label": "API Key",
                    "usable": True,
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_connection_list_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "connection.list", "params": {"x": 1}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_model_list_returns_all_models_across_providers_with_full_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    state = make_state(tmp_path, StubAdapter())
    monkeypatch.setattr(
        state.runtime.providers,
        "list_ids",
        lambda: ["openai", "anthropic", "ollama"],
    )
    state.runtime.models._models["openai"] = [
        state.runtime.models._models["openai"][1],
        state.runtime.models._models["openai"][0],
    ]

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "anthropic/claude-sonnet-4-20250219",
                    "provider_id": "anthropic",
                    "model_id": "claude-sonnet-4-20250219",
                    "name": "Claude Sonnet 4",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": True},
                    },
                    "context_window": 200000,
                    "max_output_tokens": 64000,
                },
                {
                    "id": "ollama/llama3.2",
                    "provider_id": "ollama",
                    "model_id": "llama3.2",
                    "name": "Llama 3.2",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": False},
                    },
                    "context_window": 128000,
                    "max_output_tokens": 8192,
                },
                {
                    "id": "openai/gpt-4.1-mini",
                    "provider_id": "openai",
                    "model_id": "gpt-4.1-mini",
                    "name": "GPT-4.1 mini",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": False},
                    },
                    "context_window": 128000,
                    "max_output_tokens": 16000,
                },
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
                    },
                    "context_window": 256000,
                    "max_output_tokens": 32000,
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_model_list_filters_by_connection_usability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "openai/gpt-4.1-mini",
                    "provider_id": "openai",
                    "model_id": "gpt-4.1-mini",
                    "name": "GPT-4.1 mini",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": False},
                    },
                    "context_window": 128000,
                    "max_output_tokens": 16000,
                },
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
                    },
                    "context_window": 256000,
                    "max_output_tokens": 32000,
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_tool_list_returns_all_registered_tools_with_name_and_description(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.tools.register(
        "z_tool",
        "Last tool alphabetically",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _context, _arguments: {"ok": True, "error": None, "data": {}, "artifacts": []},
    )
    state.runtime.tools.register(
        "a_tool",
        "First tool alphabetically",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _context, _arguments: {"ok": True, "error": None, "data": {}, "artifacts": []},
    )

    response = await dispatch_rpc(state, {"method": "tool.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "tools": [
                {"name": "a_tool", "description": "First tool alphabetically"},
                {"name": "z_tool", "description": "Last tool alphabetically"},
            ]
        },
    }


@pytest.mark.asyncio
async def test_settings_update_persists_supported_language_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.server_bind = {
        "listen_host": "127.0.0.1",
        "listen_port": 8500,
        "port_source": "VBOT_SERVER_PORT",
    }

    response = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"appearance": {"language": "en"}}},
    )

    assert response["ok"] is True
    assert state.runtime.storage.load_appearance_settings() == {"language": "en"}
    assert response["result"]["appearance"] == {"language": "en", "available_languages": ["en"]}
    assert response["result"]["general"]["server"] == {
        "listen_host": "127.0.0.1",
        "listen_port": 8500,
        "port_source": "VBOT_SERVER_PORT",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"general": {}},
        {"appearance": []},
        {"appearance": {}},
        {"appearance": {"show_token_counts": False}},
        {"appearance": {"language": ""}},
    ],
)
async def test_settings_update_rejects_unsupported_sections_and_fields(
    tmp_path: Path,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_appearance = state.runtime.storage.load_appearance_settings()

    response = await dispatch_rpc(state, {"method": "settings.update", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert state.runtime.storage.load_appearance_settings() == original_appearance


@pytest.mark.asyncio
async def test_settings_update_maps_storage_validation_errors_to_domain_error(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"appearance": {"language": "fr"}}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "Unsupported appearance language" in response["error"]["message"]


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
async def test_agent_crud_accepts_and_returns_connection_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    create_response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {
                "id": "writer",
                "name": "Writer",
                "model": "openai/gpt-5.2",
                "connection": "openai:api-key",
                "fallback_model": "anthropic/claude-sonnet-4-20250219",
                "fallback_connection": "anthropic:api-key",
            },
        },
    )
    update_response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {
                "id": "writer",
                "connection": "openai:oauth",
                "fallback_connection": "",
            },
        },
    )

    assert create_response["ok"] is True
    assert create_response["result"]["connection"] == "openai:api-key"
    assert create_response["result"]["fallback_connection"] == "anthropic:api-key"
    assert update_response["ok"] is True
    assert update_response["result"]["connection"] == "openai:oauth"
    assert update_response["result"]["fallback_connection"] == ""


@pytest.mark.asyncio
async def test_agent_list_response_includes_connection_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["connection"] == "openai:api-key"
    assert agent["fallback_connection"] == ""


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
        ("agent.update", {"id": "coder", "connection": 5}),
        ("agent.update", {"id": "coder", "fallback_connection": 5}),
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
async def test_chat_send_collected_timeline_includes_read_tool_result_envelope(
    tmp_path: Path,
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
    state = make_state(tmp_path, adapter)
    register_read_tool(state.runtime.tools)
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
    assert tool_started["payload"] == {
        "tool_call": {
            "id": "call_read",
            "index": 0,
            "name": "read",
            "arguments": {"path": "note.txt"},
        }
    }
    assert tool_result["payload"]["tool_call"] == {
        "id": "call_read",
        "index": 0,
        "name": "read",
    }
    assert tool_result["payload"]["result"] == {
        "ok": True,
        "error": None,
        "data": {"content": "rpc content"},
        "artifacts": [],
    }
    assert "path" not in tool_result["payload"]["result"]["data"]
    assert "reasoning_meta" not in str(result["events"])
    assert "batch" not in str(result["events"])


@pytest.mark.asyncio
async def test_chat_stream_starts_run_and_returns_run_id_without_waiting(tmp_path: Path) -> None:
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "OK"}], block=True)
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
async def test_second_run_in_same_session_is_rejected_while_active(tmp_path: Path) -> None:
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "OK"}], block=True)
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
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "OK"}], block=True)
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
async def test_chat_send_uses_non_streaming_chat_loop(tmp_path: Path) -> None:
    adapter = StubAdapter([{"content": "Complete response", "tool_calls": None}])
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
async def test_chat_stream_uses_streaming_chat_loop(tmp_path: Path) -> None:
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "Streamed response"}])
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
