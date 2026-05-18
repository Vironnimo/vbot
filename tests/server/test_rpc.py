"""Tests for server RPC dispatcher and delegates."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import server.delegates as delegates
from core.chat import ChatLoop, ChatMessage, ChatRunManager, ChatSessionManager
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.models import Capabilities, Model, ReasoningCapabilities
from core.models.discovery import ModelDiscoveryError
from core.models.models import ModelRegistry
from core.storage import StorageError
from core.tools import ToolRegistry, register_read_tool
from core.utils.errors import ConfigError
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
    def __init__(self) -> None:
        self._providers: dict[str, Any] = {
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
                adapter="openai_compatible",
                base_url="https://api.openai.com/v1",
                defaults={"max_tokens": 4096},
                extra_headers={},
                models_endpoint=None,
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
                adapter="openai_compatible",
                base_url="",
                defaults={"max_tokens": 4096},
                extra_headers={},
                models_endpoint=None,
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

    def add(self, provider: object) -> None:
        self._providers[cast(Any, provider).id] = provider

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

    def get(self, provider_id: str, model_id: str) -> Model:
        for model in self._models.get(provider_id, []):
            if model.model_id == model_id:
                return model
        raise KeyError(f"Model not found: {provider_id}/{model_id}")

    def list_for_provider(self, provider_id: str) -> list[object]:
        return list(self._models[provider_id])


class EmptyStubModels(StubModels):
    def list_for_provider(self, provider_id: str) -> list[object]:
        if provider_id not in self._models:
            return []
        return super().list_for_provider(provider_id)


def openrouter_provider() -> SimpleNamespace:
    return SimpleNamespace(
        id="openrouter",
        name="OpenRouter",
        adapter="openai_compatible",
        base_url="https://openrouter.ai/api/v1",
        defaults={"max_tokens": 8192},
        extra_headers={"X-Title": "vBot"},
        models_endpoint="/models",
        connections=[
            SimpleNamespace(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=SimpleNamespace(credential_key="OPENROUTER_API_KEY"),
            )
        ],
    )


def openrouter_provider_with_secondary_connection() -> SimpleNamespace:
    provider = openrouter_provider()
    provider.connections = [
        SimpleNamespace(
            id="oauth",
            type="oauth",
            label="OAuth",
            auth=SimpleNamespace(credential_key="OPENROUTER_OAUTH_TOKEN"),
        ),
        SimpleNamespace(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=SimpleNamespace(credential_key="OPENROUTER_API_KEY"),
        ),
    ]
    return provider


async def fake_refresh_models(
    provider_config: Any,
    credential_value: str,
    resources_dir: Path,
    **kwargs: Any,
) -> JsonObject:
    FAKE_REFRESH_MODEL_PROVIDER_IDS.append(provider_config.id)
    FAKE_REFRESH_MODEL_CALLS.append(credential_value)
    FAKE_REFRESH_MODEL_KWARGS.append(kwargs)
    models_dir = resources_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    models_dir.joinpath(f"{provider_config.id}.json").write_text(
        json.dumps(
            {
                "provider_id": provider_config.id,
                "source": "discovery",
                "fetched_at": "2026-05-08T19:08:00+00:00",
                "models": {
                    "fresh-model": {
                        "name": "Fresh Model",
                        "capabilities": {
                            "vision": True,
                            "tools": True,
                            "json_mode": True,
                            "reasoning": {"supported": True},
                        },
                        "context_window": 128000,
                        "max_output_tokens": 8192,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ModelRegistry.invalidate(resources_dir)
    return {
        "provider_id": provider_config.id,
        "model_count": 1,
        "fetched_at": "2026-05-08T19:08:00+00:00",
    }


FAKE_REFRESH_MODEL_CALLS: list[str] = []
FAKE_REFRESH_MODEL_KWARGS: list[JsonObject] = []
FAKE_REFRESH_MODEL_PROVIDER_IDS: list[str] = []


class StubStorage:
    def __init__(self, tmp_path: Path) -> None:
        self.data_dir = tmp_path
        self.prompts_dir = tmp_path / "prompts"
        self._appearance = {"language": "en"}
        self._skill_directories: list[str] = []
        self._settings: JsonObject = {}
        self._prompt_fragments: dict[str, str] = {
            "system.md": "# System\nDefault system prompt.",
            "runtime.md": "# Runtime\nDefault runtime info.",
            "tools.md": "# Tools\nDefault tools list.",
            "channels.md": "# Channels\nDefault channels list.",
            "skills.md": "# Skills\nDefault skills list.",
        }

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

    def load_skill_directory_settings(self) -> list[str]:
        return list(self._skill_directories)

    def update_skill_directory_settings(self, directories: object) -> list[str]:
        if not isinstance(directories, list) or not all(
            isinstance(directory, str) for directory in directories
        ):
            raise StorageError("settings.skill_directories must be a list")
        self._skill_directories = list(directories)
        return list(self._skill_directories)

    def load_subagent_settings(self) -> JsonObject:
        return {
            "max_subagent_depth": int(self._settings.get("max_subagent_depth", 4)),
            "max_subagents_per_turn": int(self._settings.get("max_subagents_per_turn", 8)),
            "subagent_timeout_minutes": int(self._settings.get("subagent_timeout_minutes", 60)),
        }

    def load_settings(self) -> JsonObject:
        return dict(self._settings)

    def save_settings(self, settings: JsonObject) -> None:
        self._settings = dict(settings)

    def read_prompt_fragment(self, name: str) -> str:
        if name not in self._prompt_fragments:
            raise StorageError(f"Unknown prompt fragment: {name}")
        return self._prompt_fragments[name]

    def write_prompt_fragment(self, name: str, content: str) -> None:
        if name not in self._prompt_fragments:
            raise StorageError(f"Unknown prompt fragment: {name}")
        self._prompt_fragments[name] = content
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        (self.prompts_dir / name).write_text(content, encoding="utf-8")

    def reset_prompt_fragment(self, name: str) -> None:
        if name not in self._prompt_fragments:
            raise StorageError(f"Unknown prompt fragment: {name}")
        default = f"# {name}\nDefault {name} content."
        self._prompt_fragments[name] = default
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        (self.prompts_dir / name).write_text(default, encoding="utf-8")


class StubPrompts:
    def build_system_prompt(self, agent: StubAgent) -> str:
        return f"System for {agent.id}"

    def provider_tool_definitions(self, _agent: StubAgent) -> list[JsonObject]:
        return []


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str


class StubSkills:
    def __init__(self) -> None:
        self._skills = [
            StubSkill("debugging", "Debug failures."),
            StubSkill("warned", "Loads with warnings."),
        ]
        self._warnings = {"debugging": [], "warned": ["Name does not match directory."]}
        self._invalid = [
            SimpleNamespace(
                name="broken",
                path=Path("/skills/broken/SKILL.md"),
                valid=False,
                warnings=["missing description"],
                loadable=False,
            )
        ]

    def list_all(self) -> list[StubSkill]:
        return list(self._skills)

    def warnings_for(self, name: str) -> list[str]:
        return list(self._warnings[name])

    def invalid_diagnostics(self) -> list[Any]:
        return list(self._invalid)


class ReloadableStubRuntimeSkills:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def list_all(self) -> list[StubSkill]:
        return [
            StubSkill(name, f"{name} skill.") for name in self._runtime.storage._skill_directories
        ]

    def warnings_for(self, _name: str) -> list[str]:
        return []

    def invalid_diagnostics(self) -> list[Any]:
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
        self.resources_dir = tmp_path / "resources"
        self.agents = StubAgents(StubAgent(id="coder", allowed_tools=["*"]))
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.system_prompts = StubPrompts()
        self.storage = StubStorage(tmp_path)
        self.tools = ToolRegistry()
        self.skills: Any = StubSkills()
        self._models = StubModels()
        self.providers = StubProviders()
        self.adapter = adapter
        self.chat_runs: ChatRunManager | None = None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def get_adapter(self, _provider_id: str, _connection_id: str) -> StubAdapter:
        return self.adapter

    @property
    def models(self) -> Any:
        return self._models

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

            def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str:
                provider = cast(Any, runtime.providers.get(provider_id))
                if connection_id is None:
                    for connection in provider.connections:
                        credential = os.environ.get(connection.auth.credential_key, "")
                        if credential:
                            return credential
                    raise ConfigError(
                        f"Provider credentials not found for provider '{provider_id}'"
                    )
                local_id = connection_id.removeprefix(f"{provider_id}:")
                connection = next(
                    connection for connection in provider.connections if connection.id == local_id
                )
                credential = os.environ.get(connection.auth.credential_key, "")
                if credential:
                    return credential
                raise ConfigError(f"Provider credentials not found for provider '{provider_id}'")

        return CredentialResolver()

    def _resolve_resources_path(self) -> Path:
        return self.resources_dir

    def reload_skills(self) -> None:
        self.skills = ReloadableStubRuntimeSkills(self)


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


class StubDelegateRun:
    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        session_id: str,
        status: str,
        final_message: ChatMessage | None = None,
    ) -> None:
        self.id = run_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.status = SimpleNamespace(value=status)
        self.events: list[Any] = []
        self._final_message = final_message or ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="OK",
        )

    async def wait(self) -> ChatMessage:
        return self._final_message


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

    assert response["ok"] is True, response
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
                    "id": "ollama",
                    "name": "Ollama",
                    "base_url": "",
                    "models_endpoint": None,
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
                    "models_endpoint": None,
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
        "skills": {
            "default_directory": str(tmp_path / "skills"),
            "directories": [],
        },
        "appearance": {"language": "en", "available_languages": ["en"]},
        "subagents": {
            "max_subagent_depth": 4,
            "max_subagents_per_turn": 8,
            "subagent_timeout_minutes": 60,
        },
    }
    assert "sk-live-secret" not in str(response)
    assert "show_token_counts" not in str(response)
    assert "origin" not in response["result"]["general"]["server"]


@pytest.mark.asyncio
async def test_settings_get_exposes_provider_models_endpoint_for_refresh_button(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime._models = EmptyStubModels()
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True
    providers = response["result"]["providers"]["items"]
    openrouter = next(provider for provider in providers if provider["id"] == "openrouter")
    openai = next(provider for provider in providers if provider["id"] == "openai")
    assert openrouter["models_endpoint"] == "/models"
    assert openai["models_endpoint"] is None


@pytest.mark.asyncio
async def test_settings_get_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {"extra": True}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_log_list_returns_sorted_files_with_default_selection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    (logs_dir / "2026-05-09").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-11").write_text("", encoding="utf-8")
    (logs_dir / "2026-05-10").write_text("", encoding="utf-8")

    response = await dispatch_rpc(state, {"method": "log.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "files": ["2026-05-11", "2026-05-10", "2026-05-09"],
            "default_file": "2026-05-11",
        },
    }


@pytest.mark.asyncio
async def test_log_list_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "log.list", "params": {"extra": True}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_log_read_returns_structured_entries(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    (logs_dir / "2026-05-11").write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.app - Ready",
                "trace line",
                "2026-05-11 09:00:01 [ERROR] vbot.server.app - Failed",
            ]
        ),
        encoding="utf-8",
    )

    response = await dispatch_rpc(
        state,
        {"method": "log.read", "params": {"file": "2026-05-11"}},
    )

    assert response == {
        "ok": True,
        "result": {
            "file": "2026-05-11",
            "entries": [
                {
                    "timestamp": "2026-05-11 09:00:00",
                    "level": "info",
                    "logger_name": "vbot.server.app",
                    "message": "Ready",
                    "continuation": "trace line",
                },
                {
                    "timestamp": "2026-05-11 09:00:01",
                    "level": "error",
                    "logger_name": "vbot.server.app",
                    "message": "Failed",
                    "continuation": "",
                },
            ],
            "cursor": response["result"]["cursor"],
        },
    }
    assert isinstance(response["result"]["cursor"], str)
    assert response["result"]["cursor"]


@pytest.mark.asyncio
async def test_log_read_filters_persisted_routine_websocket_noise(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    (logs_dir / "2026-05-11").write_text(
        "\n".join(
            [
                "2026-05-11 09:00:00 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:55090 - "WebSocket /ws" [accepted]',
                "2026-05-11 09:00:01 [INFO] vbot.server.uvicorn - connection open",
                "2026-05-11 09:00:02 [INFO] vbot.server.uvicorn - "
                '127.0.0.1:60756 - "WebSocket /ws/logs?cursor=abc" [accepted]',
                "2026-05-11 09:00:03 [INFO] vbot.server.uvicorn - connection closed",
                "2026-05-11 09:00:04 [WARN] vbot.server.uvicorn - keepalive ping timeout",
                "2026-05-11 09:00:05 [ERROR] vbot.server.uvicorn - opening handshake failed",
                "2026-05-11 09:00:06 [INFO] vbot.server.app - Ready",
            ]
        ),
        encoding="utf-8",
    )

    response = await dispatch_rpc(
        state,
        {"method": "log.read", "params": {"file": "2026-05-11"}},
    )

    assert response["ok"] is True
    assert response["result"]["entries"] == [
        {
            "timestamp": "2026-05-11 09:00:04",
            "level": "warn",
            "logger_name": "vbot.server.uvicorn",
            "message": "keepalive ping timeout",
            "continuation": "",
        },
        {
            "timestamp": "2026-05-11 09:00:05",
            "level": "error",
            "logger_name": "vbot.server.uvicorn",
            "message": "opening handshake failed",
            "continuation": "",
        },
        {
            "timestamp": "2026-05-11 09:00:06",
            "level": "info",
            "logger_name": "vbot.server.app",
            "message": "Ready",
            "continuation": "",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"file": ""},
        {"file": "../2026-05-11"},
        {"file": "2026-05-11", "extra": True},
    ],
)
async def test_log_read_rejects_invalid_requests(tmp_path: Path, params: JsonObject) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "log.read", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_log_read_rejects_missing_file_with_domain_error(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "log.read", "params": {"file": "2026-05-11"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert response["error"]["message"] == "log file not found: 2026-05-11"


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
async def test_model_refresh_db_refreshes_provider_models_and_runtime_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(delegates, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "model_count": 1,
            "fetched_at": "2026-05-08T19:08:00+00:00",
        },
    }
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openrouter"]
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key"]
    refreshed_model = state.runtime.models.get("openrouter", "fresh-model")
    assert refreshed_model.name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_without_params_refreshes_only_eligible_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("OPENROUTER_SECONDARY_API_KEY", "secondary-key")
    monkeypatch.setattr(delegates, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(
        SimpleNamespace(
            id="refreshable-missing-credentials",
            name="Refreshable Missing Credentials",
            adapter="openai_compatible",
            base_url="https://missing.example/v1",
            defaults={},
            extra_headers={},
            models_endpoint="/models",
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=SimpleNamespace(credential_key="MISSING_REFRESH_API_KEY"),
                )
            ],
        )
    )
    state.runtime.providers.add(
        SimpleNamespace(
            id="refreshable-secondary",
            name="Refreshable Secondary",
            adapter="openai_compatible",
            base_url="https://secondary.example/v1",
            defaults={},
            extra_headers={},
            models_endpoint="/models",
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=SimpleNamespace(credential_key="OPENROUTER_SECONDARY_API_KEY"),
                )
            ],
        )
    )

    response = await dispatch_rpc(state, {"method": "model.refresh_db"})

    assert response == {
        "ok": True,
        "result": {
            "providers": [
                {
                    "provider_id": "openrouter",
                    "model_count": 1,
                    "fetched_at": "2026-05-08T19:08:00+00:00",
                },
                {
                    "provider_id": "refreshable-secondary",
                    "model_count": 1,
                    "fetched_at": "2026-05-08T19:08:00+00:00",
                },
            ],
            "refreshed_count": 2,
            "model_count": 2,
        },
    }
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openrouter", "refreshable-secondary"]
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key", "secondary-key"]
    assert state.runtime.models.get("openrouter", "fresh-model").name == "Fresh Model"
    assert state.runtime.models.get("refreshable-secondary", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_empty_params_reloads_runtime_registry_after_global_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(delegates, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    previous_models = state.runtime.models

    response = await dispatch_rpc(state, {"method": "model.refresh_db", "params": {}})

    assert response["ok"] is True
    assert state.runtime.models is not previous_models
    assert state.runtime.models.get("openrouter", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_passes_first_usable_connection_to_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(delegates, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider_with_secondary_connection())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is True
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key"]
    assert FAKE_REFRESH_MODEL_KWARGS[0]["credential_connection"].id == "api-key"


@pytest.mark.asyncio
async def test_model_refresh_db_maps_discovery_failures_to_rpc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_refresh_models(*_args: Any, **_kwargs: Any) -> JsonObject:
        raise ModelDiscoveryError("Model discovery failed for provider 'openrouter': bad JSON")

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(delegates, "refresh_models", failing_refresh_models)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "bad JSON" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_provider_without_models_endpoint(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "provider 'openai' does not support model refresh" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_missing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert (
        "Provider credentials not found for provider 'openrouter'" in response["error"]["message"]
    )


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_unknown_provider(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "missing"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "missing" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_unsupported_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter", "extra": True}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert response["error"]["message"] == "unsupported model refresh fields: extra"


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
async def test_tool_list_omits_internal_skill_tool(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.tools.register(
        "skill",
        "Load skills",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _context, _arguments: {"ok": True, "error": None, "data": {}, "artifacts": []},
        internal=True,
    )

    response = await dispatch_rpc(state, {"method": "tool.list", "params": {}})

    assert response == {"ok": True, "result": {"tools": []}}


@pytest.mark.asyncio
async def test_skill_list_returns_loadable_and_invalid_diagnostics(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "skill.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "skills": [
                {
                    "name": "debugging",
                    "description": "Debug failures.",
                    "valid": True,
                    "warnings": [],
                },
                {
                    "name": "warned",
                    "description": "Loads with warnings.",
                    "valid": False,
                    "warnings": ["Name does not match directory."],
                },
            ],
            "invalid_skills": [
                {
                    "name": "broken",
                    "path": str(Path("/skills/broken/SKILL.md")),
                    "valid": False,
                    "warnings": ["missing description"],
                }
            ],
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
async def test_settings_update_persists_skill_directories_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"skills": {"directories": ["~/skills", " C:/skills/team "]}},
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_skill_directory_settings() == [
        "~/skills",
        " C:/skills/team ",
    ]
    assert response["result"]["skills"] == {
        "default_directory": str(tmp_path / "skills"),
        "directories": ["~/skills", " C:/skills/team "],
    }


@pytest.mark.asyncio
async def test_settings_update_persists_subagent_settings_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "subagents": {
                    "max_subagent_depth": 6,
                    "max_subagents_per_turn": 12,
                    "subagent_timeout_minutes": 90,
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_settings() == {
        "max_subagent_depth": 6,
        "max_subagents_per_turn": 12,
        "subagent_timeout_minutes": 90,
    }
    assert response["result"]["subagents"] == {
        "max_subagent_depth": 6,
        "max_subagents_per_turn": 12,
        "subagent_timeout_minutes": 90,
    }


@pytest.mark.asyncio
async def test_settings_update_reloads_runtime_skills_for_immediate_skill_list(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    update_response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"skills": {"directories": ["debugging"]}},
        },
    )
    list_response = await dispatch_rpc(state, {"method": "skill.list", "params": {}})

    assert update_response["ok"] is True, update_response
    assert list_response == {
        "ok": True,
        "result": {
            "skills": [
                {
                    "name": "debugging",
                    "description": "debugging skill.",
                    "valid": True,
                    "warnings": [],
                }
            ],
            "invalid_skills": [],
        },
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
        {"skills": []},
        {"skills": {}},
        {"skills": {"extra": []}},
        {"skills": {"directories": "~/skills"}},
        {"skills": {"directories": [1]}},
        {"subagents": []},
        {"subagents": {}},
        {"subagents": {"extra": 1}},
        {
            "subagents": {
                "max_subagent_depth": 0,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
            }
        },
        {
            "subagents": {
                "max_subagent_depth": True,
                "max_subagents_per_turn": 8,
                "subagent_timeout_minutes": 60,
            }
        },
        {
            "subagents": {
                "max_subagent_depth": 4,
                "max_subagents_per_turn": "8",
                "subagent_timeout_minutes": 60,
            }
        },
        {
            "subagents": {
                "max_subagent_depth": 4,
                "max_subagents_per_turn": 8,
            }
        },
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
async def test_agent_crud_rejects_connection_fields(tmp_path: Path) -> None:
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

    assert create_response["ok"] is False
    assert create_response["error"]["code"] == "invalid_request"
    assert "unsupported agent fields" in create_response["error"]["message"]
    assert update_response["ok"] is False
    assert update_response["error"]["code"] == "invalid_request"
    assert "unsupported agent fields" in update_response["error"]["message"]


@pytest.mark.asyncio
async def test_agent_list_response_omits_connection_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert "connection" not in agent
    assert "fallback_connection" not in agent


@pytest.mark.asyncio
async def test_agent_list_includes_context_window_for_known_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "openai/gpt-5.2"
    assert agent["context_window"] == 256000


@pytest.mark.asyncio
async def test_agent_list_includes_context_window_for_suffixed_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", model="openai/gpt-5.2::api-key")

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "openai/gpt-5.2::api-key"
    assert agent["context_window"] == 256000


@pytest.mark.asyncio
async def test_agent_list_includes_null_context_window_for_unknown_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", model="unknown/missing-model")

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "unknown/missing-model"
    assert agent["context_window"] is None


@pytest.mark.asyncio
async def test_agent_list_includes_null_context_window_for_model_without_provider_prefix(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", model="bare-model-id")

    response = await dispatch_rpc(state, {"method": "agent.list", "params": {}})

    assert response["ok"] is True
    agent = response["result"]["agents"][0]
    assert agent["model"] == "bare-model-id"
    assert agent["context_window"] is None


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
async def test_chat_history_includes_usage_on_assistant_messages(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("coder", session_id="usage-session")
    state.runtime.agents.update("coder", current_session_id="usage-session")
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            usage={"input_tokens": 150, "output_tokens": 42},
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
    assert messages[0]["usage"] == {"input_tokens": 150, "output_tokens": 42}
    assert messages[0]["content"] == "Hello"

    # User message does not carry usage
    assert "usage" not in messages[1]

    # Assistant message without usage has no usage key
    assert "usage" not in messages[2]


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
    assert "session does not exist" in response["error"]["message"]


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
    ) -> StubDelegateRun:
        captured["agent_id"] = agent_id
        captured["content"] = content
        captured["session_id"] = session_id
        return run

    monkeypatch.setattr(state.chat_loop, "start_run", fake_start_run)
    monkeypatch.setattr(delegates, "_bridge_run_to_event_bus", lambda _state, _run: None)

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
        ) -> StubDelegateRun:
            captured["agent_id"] = agent_id
            captured["content"] = content
            captured["session_id"] = session_id
            return run

    monkeypatch.setattr(delegates, "_streaming_chat_loop", lambda _state: StubStreamingLoop())
    monkeypatch.setattr(delegates, "_bridge_run_to_event_bus", lambda _state, _run: None)

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
    assert response["error"]["message"] == (
        "params.content must be a non-empty string or a list of content blocks"
    )


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
async def test_chat_stream_starts_run_and_returns_run_id_without_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "OK"}], block=True)
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
async def test_second_run_in_same_session_is_rejected_while_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "OK"}], block=True)
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
    assert second_response["ok"] is False
    assert second_response["error"]["code"] == "active_run"

    adapter.release.set()
    await state.chat_runs.cancel(first_response["result"]["run_id"])


@pytest.mark.asyncio
async def test_chat_cancel_marks_running_run_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "OK"}], block=True)
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
    adapter = StubAdapter(stream_deltas=[{"type": "content_delta", "text": "Streamed response"}])
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
async def test_chat_stream_prefers_runtime_streaming_chat_loop_when_available(
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
        ) -> StubDelegateRun:
            captured["agent_id"] = agent_id
            captured["content"] = content
            captured["session_id"] = session_id
            return run

    runtime_streaming_loop = RuntimeStreamingLoop()
    state.runtime.streaming_chat_loop = runtime_streaming_loop
    monkeypatch.setattr(delegates, "_bridge_run_to_event_bus", lambda _state, _run: None)

    response = await dispatch_rpc(
        state,
        {
            "method": "chat.stream",
            "params": {"agent_id": "coder", "session_id": "session-one", "content": "Hi"},
        },
    )

    assert response["ok"] is True
    assert response["result"]["run_id"] == "runtime-stream-loop"
    assert captured == {"agent_id": "coder", "content": "Hi", "session_id": "session-one"}
    assert state.streaming_chat_loop is runtime_streaming_loop


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


class TestRemoveOpaqueProviderMetadata:
    """Tests for _remove_opaque_provider_metadata preserving canonical fields."""

    def test_strips_reasoning_meta(self) -> None:
        result = delegates._remove_opaque_provider_metadata(
            {"role": "assistant", "reasoning_meta": {"secret": "opaque"}}
        )
        assert result == {"role": "assistant"}

    def test_preserves_usage(self) -> None:
        result = delegates._remove_opaque_provider_metadata(
            {"role": "assistant", "usage": {"input_tokens": 100, "output_tokens": 50}}
        )
        assert result == {"role": "assistant", "usage": {"input_tokens": 100, "output_tokens": 50}}

    def test_preserves_usage_and_strips_reasoning_meta(self) -> None:
        result = delegates._remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "content": "Hello",
                "reasoning": "thinking",
                "reasoning_meta": {"secret": "opaque"},
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )
        assert result == {
            "role": "assistant",
            "content": "Hello",
            "reasoning": "thinking",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    def test_strips_nested_reasoning_meta(self) -> None:
        result = delegates._remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "read",
                        "arguments": {"path": "file.txt"},
                        "reasoning_meta": {"secret": "nested"},
                    }
                ],
            }
        )
        assert result == {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "read",
                    "arguments": {"path": "file.txt"},
                }
            ],
        }

    def test_preserves_usage_nested_in_dict(self) -> None:
        result = delegates._remove_opaque_provider_metadata(
            {
                "role": "assistant",
                "content": "file contents",
                "usage": {"input_tokens": 10},
            }
        )
        assert result == {
            "role": "assistant",
            "content": "file contents",
            "usage": {"input_tokens": 10},
        }


class TestVisibleMessage:
    """Tests for _visible_message preserving usage and stripping reasoning_meta."""

    def test_visible_message_includes_usage_on_assistant(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            usage={"input_tokens": 200, "output_tokens": 30},
        )
        result = delegates._visible_message(message)
        assert result["usage"] == {"input_tokens": 200, "output_tokens": 30}

    def test_visible_message_strips_reasoning_meta(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            reasoning_meta={"secret": "opaque"},
        )
        result = delegates._visible_message(message)
        assert "reasoning_meta" not in result

    def test_visible_message_preserves_usage_and_strips_reasoning_meta(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
            reasoning="visible thinking",
            reasoning_meta={"secret": "opaque"},
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        result = delegates._visible_message(message)
        assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert result["reasoning"] == "visible thinking"
        assert "reasoning_meta" not in result

    def test_visible_message_excludes_usage_when_none(self) -> None:
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Hello",
        )
        result = delegates._visible_message(message)
        assert "usage" not in result


class TestServerEventFromRunEvent:
    """Tests for _server_event_from_run_event preserving usage in run_completed."""

    def _make_event(
        self,
        event_type: str,
        payload: JsonObject | None = None,
        sequence: int = 1,
    ) -> Any:
        """Create a minimal RunEvent for testing."""
        from core.chat.runs import RunEvent

        return RunEvent(
            sequence=sequence,
            run_id="run-test",
            agent_id="agent-test",
            session_id="session-test",
            type=event_type,
            payload=payload or {},
        )

    def test_run_completed_includes_usage_when_present(self) -> None:
        event = self._make_event(
            delegates.RUN_COMPLETED_EVENT,
            {"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 50}},
        )
        result = delegates._server_event_from_run_event(event)

        assert result["payload"]["usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_run_completed_omits_usage_when_absent(self) -> None:
        event = self._make_event(
            delegates.RUN_COMPLETED_EVENT,
            {"status": "completed"},
        )
        result = delegates._server_event_from_run_event(event)

        assert "usage" not in result["payload"]

    def test_run_completed_strips_reasoning_meta_from_usage(self) -> None:
        event = self._make_event(
            delegates.RUN_COMPLETED_EVENT,
            {
                "status": "completed",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "reasoning_meta": {"secret": "opaque"},
                },
            },
        )
        result = delegates._server_event_from_run_event(event)

        assert result["payload"]["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert "reasoning_meta" not in result["payload"]["usage"]

    def test_run_completed_preserves_usage_with_estimated_flag(self) -> None:
        event = self._make_event(
            delegates.RUN_COMPLETED_EVENT,
            {
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 50, "estimated": True},
            },
        )
        result = delegates._server_event_from_run_event(event)

        assert result["payload"]["usage"] == {
            "input_tokens": 100,
            "output_tokens": 50,
            "estimated": True,
        }

    def test_run_completed_includes_status_alongside_usage(self) -> None:
        event = self._make_event(
            delegates.RUN_COMPLETED_EVENT,
            {"status": "completed", "usage": {"input_tokens": 200, "output_tokens": 30}},
        )
        result = delegates._server_event_from_run_event(event)

        assert result["payload"]["status"] == "completed"
        assert result["payload"]["usage"] == {"input_tokens": 200, "output_tokens": 30}

    def test_non_completed_terminal_event_excludes_usage(self) -> None:
        """run_failed and run_cancelled should not carry usage even if payload has it."""
        event = self._make_event(
            delegates.RUN_FAILED_EVENT,
            {"status": "failed", "usage": {"input_tokens": 10}},
        )
        result = delegates._server_event_from_run_event(event)

        assert "usage" not in result["payload"]
        assert result["payload"]["status"] == "failed"


# ---------------------------------------------------------------------------
# prompt.* RPC handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_list_returns_all_five_fragments_in_order(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "prompt.list"})

    assert response["ok"] is True
    fragments = response["result"]["fragments"]
    assert [f["name"] for f in fragments] == [
        "system.md",
        "runtime.md",
        "tools.md",
        "channels.md",
        "skills.md",
    ]


@pytest.mark.asyncio
async def test_prompt_list_includes_content_is_modified_and_variables(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "prompt.list"})

    assert response["ok"] is True
    fragments = {f["name"]: f for f in response["result"]["fragments"]}
    system = fragments["system.md"]
    assert system["content"] == "# System\nDefault system prompt."
    assert system["is_modified"] is False
    assert any(v["placeholder"] == "{app_version}" for v in system["variables"])
    tools = fragments["tools.md"]
    assert any(v["placeholder"] == "{tool_list}" for v in tools["variables"])


@pytest.mark.asyncio
async def test_prompt_list_reflects_user_modified_fragment(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.write_prompt_fragment("runtime.md", "My custom runtime.")

    response = await dispatch_rpc(state, {"method": "prompt.list"})

    assert response["ok"] is True
    fragments = {f["name"]: f for f in response["result"]["fragments"]}
    assert fragments["runtime.md"]["is_modified"] is True
    assert fragments["runtime.md"]["content"] == "My custom runtime."
    assert fragments["system.md"]["is_modified"] is False


@pytest.mark.asyncio
async def test_prompt_update_writes_content_and_returns_is_modified_true(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.update", "params": {"name": "tools.md", "content": "# Custom tools"}},
    )

    assert response["ok"] is True
    assert response["result"] == {
        "name": "tools.md",
        "content": "# Custom tools",
        "is_modified": True,
    }
    assert state.runtime.storage.read_prompt_fragment("tools.md") == "# Custom tools"


@pytest.mark.asyncio
async def test_prompt_update_rejects_unknown_fragment_name(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.update", "params": {"name": "../../etc/passwd", "content": "bad"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_prompt_update_rejects_missing_name(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.update", "params": {"content": "no name here"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_prompt_reset_restores_default_and_returns_content(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.write_prompt_fragment("skills.md", "Modified skills")

    response = await dispatch_rpc(
        state,
        {"method": "prompt.reset", "params": {"name": "skills.md"}},
    )

    assert response["ok"] is True
    assert response["result"]["name"] == "skills.md"
    assert response["result"]["content"] == "# skills.md\nDefault skills.md content."


@pytest.mark.asyncio
async def test_prompt_reset_rejects_unknown_fragment_name(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.reset", "params": {"name": "unknown.md"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_prompt_preview_returns_rendered_text_and_token_estimate(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["text"] == "System for coder"
    assert isinstance(result["tokens"], int)
    assert result["tokens"] > 0
    assert result["estimated"] is True


@pytest.mark.asyncio
async def test_prompt_preview_rejects_unknown_agent_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "nobody"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "nobody" in response["error"]["message"]


@pytest.mark.asyncio
async def test_prompt_preview_rejects_missing_agent_id(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
