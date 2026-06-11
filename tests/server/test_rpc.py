"""Tests for server RPC dispatcher and delegates."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest

import server.delegates as delegates
from core.automation import TriggerService
from core.chat import ChatLoop, ChatMessage, ChatSessionManager, CommandDispatcher, ToolCall
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.memory import DEFAULT_MEMORY_PROMPT_MODE
from core.models import Capabilities, Model, ModelQuery, ReasoningCapabilities
from core.models.discovery import ModelDiscoveryError
from core.models.models import ModelRegistry
from core.runs import ChatRunManager, Run
from core.settings import AGENT_DEFAULT_FIELDS
from core.storage import StorageError
from core.tools import ToolRegistry, register_read_tool
from core.utils.errors import ConfigError
from server.delegates import dispatch_rpc
from server.events import ServerEventBus
from server.rpc.payloads import _model_response
from server.rpc.provider_access import _provider_has_credentials

JsonObject = dict[str, Any]
SettingsUpdateResult = TypeVar("SettingsUpdateResult")
STUB_SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)


@dataclass(frozen=True)
class StubAgent:
    id: str
    name: str = "Coder Agent"
    model: str = "openai/gpt-5.2"
    fallback_model: str = ""
    workspace: str = "C:/workspace"
    temperature: float | None = 0.1
    thinking_effort: str | None = ""
    memory_prompt_mode: str = DEFAULT_MEMORY_PROMPT_MODE
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    custom_system_prompt_enabled: bool = False
    current_session_id: str = ""
    created_at: str = "2026-05-04T00:00:00Z"
    updated_at: str = "2026-05-04T00:00:00Z"

    def __post_init__(self) -> None:
        if self.allowed_tools is None:
            object.__setattr__(self, "allowed_tools", ["*"])
        if self.allowed_skills is None:
            object.__setattr__(self, "allowed_skills", ["*"])


class StubAgents:
    def __init__(
        self,
        agent: StubAgent,
        *,
        defaults_provider: Callable[[], JsonObject] | None = None,
    ) -> None:
        self._agents: dict[str, StubAgent] = {agent.id: agent}
        self._defaults_provider = defaults_provider

    def _get_raw(self, agent_id: str) -> StubAgent:
        if agent_id not in self._agents:
            raise KeyError(agent_id)
        return self._agents[agent_id]

    def _apply_defaults(self, agent: StubAgent) -> StubAgent:
        defaults = self._defaults_provider() if self._defaults_provider is not None else {}

        model = agent.model
        fallback_model = agent.fallback_model
        temperature = agent.temperature
        thinking_effort = agent.thinking_effort

        default_model = defaults.get("model")
        if model == "" and isinstance(default_model, str):
            model = default_model

        default_fallback_model = defaults.get("fallback_model")
        if fallback_model == "" and isinstance(default_fallback_model, str):
            fallback_model = default_fallback_model

        default_temperature = defaults.get("temperature")
        if (
            temperature is None
            and isinstance(default_temperature, int | float)
            and not isinstance(default_temperature, bool)
        ):
            temperature = float(default_temperature)

        default_thinking_effort = defaults.get("thinking_effort")
        if thinking_effort is None and isinstance(default_thinking_effort, str):
            thinking_effort = default_thinking_effort

        return StubAgent(
            **{
                **agent.__dict__,
                "model": model,
                "fallback_model": fallback_model,
                "temperature": temperature,
                "thinking_effort": thinking_effort,
            }
        )

    def get(self, agent_id: str) -> StubAgent:
        return self._apply_defaults(self._get_raw(agent_id))

    def list(self) -> list[StubAgent]:
        return [self._apply_defaults(self._agents[agent_id]) for agent_id in sorted(self._agents)]

    def create(self, agent_id: str, name: str, **changes: Any) -> StubAgent:
        agent = StubAgent(id=agent_id, name=name, **changes)
        self._agents[agent_id] = agent
        return self._get_raw(agent_id)

    def update(self, agent_id: str, **changes: Any) -> StubAgent:
        agent = self._get_raw(agent_id)
        updated = StubAgent(**{**agent.__dict__, **changes})
        self._agents[agent_id] = updated
        return self.get(agent_id)

    def delete(self, agent_id: str) -> Path:
        self._get_raw(agent_id)
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

    def query(self, model_query: ModelQuery) -> list[tuple[str, Model]]:
        provider_filter = model_query.provider_id
        matches: list[tuple[str, Model]] = []
        for provider_id, models in self._models.items():
            if provider_filter and provider_id != provider_filter:
                continue
            for model in models:
                if model_query.matches(model):
                    matches.append((provider_id, model))
        return sorted(matches, key=lambda item: (item[0], item[1].model_id))


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
        self._credentials: dict[str, str] = {}
        self._prompt_fragments: dict[str, str] = {
            "system.md": "# System\nDefault system prompt.",
            "runtime.md": "# Runtime\nDefault runtime info.",
            "tools.md": "# Tools\nDefault tools list.",
            "channels.md": "# Channels\nDefault channels list.",
            "skills.md": "# Skills\nDefault skills list.",
        }
        self._agent_prompt_fragments: dict[tuple[str, str], str] = {}

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

    def load_compaction_settings(self) -> JsonObject:
        defaults: JsonObject = {
            "auto": True,
            "threshold": 0.8,
            "tail_tokens": 15_000,
            "summary_model": None,
        }
        stored = self._settings.get("compaction")
        if not isinstance(stored, dict):
            return dict(defaults)

        normalized = dict(defaults)
        if isinstance(stored.get("auto"), bool):
            normalized["auto"] = stored["auto"]

        threshold = stored.get("threshold")
        if isinstance(threshold, int | float) and not isinstance(threshold, bool):
            normalized["threshold"] = float(threshold)

        tail_tokens = stored.get("tail_tokens")
        if isinstance(tail_tokens, int) and not isinstance(tail_tokens, bool):
            normalized["tail_tokens"] = tail_tokens

        summary_model = stored.get("summary_model")
        if isinstance(summary_model, str) or summary_model is None:
            normalized["summary_model"] = summary_model

        return normalized

    def load_recall_settings(self) -> JsonObject:
        stored = self._settings.get("recall")
        if not isinstance(stored, dict):
            return {"backend": "jsonl_scan"}

        backend = stored.get("backend")
        if not isinstance(backend, str) or not backend.strip():
            return {"backend": "jsonl_scan"}
        return {"backend": backend.strip()}

    def load_web_search_settings(self) -> JsonObject:
        stored = self._settings.get("web_search")
        defaults: JsonObject = {
            "provider": "brave",
            "searxng": {"base_url": "http://localhost:8888"},
        }
        if not isinstance(stored, dict):
            return defaults

        provider = stored.get("provider")
        if not isinstance(provider, str) or provider not in {"brave", "searxng"}:
            provider = "brave"

        searxng = stored.get("searxng")
        if not isinstance(searxng, dict):
            searxng = {}
        base_url = searxng.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            base_url = "http://localhost:8888"

        return {
            "provider": provider,
            "searxng": {"base_url": base_url.strip()},
        }

    def load_debug_settings(self) -> JsonObject:
        return {"enabled": False, "trace_limit": 50}

    def load_model_task_settings(self) -> JsonObject:
        stored = self._settings.get("model_tasks")
        return dict(stored) if isinstance(stored, dict) else {}

    def update_recall_settings(self, recall: object) -> JsonObject:
        if not isinstance(recall, dict):
            raise StorageError("Recall settings must be an object")

        self._settings = {
            **self._settings,
            "recall": dict(self.load_recall_settings() | recall),
        }
        return self.load_recall_settings()

    def update_web_search_settings(self, web_search: object) -> JsonObject:
        if not isinstance(web_search, dict):
            raise StorageError("Web search settings must be an object")

        current = self.load_web_search_settings()
        searxng = web_search.get("searxng")
        if searxng is None:
            searxng = {}
        if not isinstance(searxng, dict):
            raise StorageError("Expected settings.web_search.searxng to be an object")

        self._settings = {
            **self._settings,
            "web_search": {
                **current,
                **web_search,
                "searxng": {
                    **current["searxng"],
                    **searxng,
                },
            },
        }
        return self.load_web_search_settings()

    def update_compaction_settings(self, compaction: object) -> JsonObject:
        if not isinstance(compaction, dict):
            raise StorageError("Compaction settings must be an object")

        current = self.load_compaction_settings()
        current.update(compaction)
        self._settings = {
            **self._settings,
            "compaction": dict(current),
        }
        return dict(current)

    def load_defaults(self) -> JsonObject:
        defaults = self._settings.get("defaults")
        if not isinstance(defaults, dict):
            return {}

        raw_agent_defaults = defaults.get("agent")
        if not isinstance(raw_agent_defaults, dict):
            return {}

        unsupported_fields = sorted(set(raw_agent_defaults) - AGENT_DEFAULT_FIELDS)
        if unsupported_fields:
            raise StorageError(
                f"Unsupported defaults.agent settings: {', '.join(unsupported_fields)}"
            )

        normalized_agent_defaults: JsonObject = {}
        for field, value in raw_agent_defaults.items():
            normalized_value = self._normalize_agent_default_value(field, value)
            if normalized_value is None:
                continue
            normalized_agent_defaults[field] = normalized_value

        if not normalized_agent_defaults:
            return {}
        return {"agent": normalized_agent_defaults}

    def update_defaults(self, section: str, values: object) -> JsonObject:
        if section != "agent":
            raise StorageError(f"Unsupported defaults section: {section}")
        if not isinstance(values, dict):
            raise StorageError("Defaults values must be a mapping")

        unsupported_fields = sorted(set(values) - AGENT_DEFAULT_FIELDS)
        if unsupported_fields:
            raise StorageError(
                f"Unsupported defaults.agent settings: {', '.join(unsupported_fields)}"
            )

        current_agent_defaults = dict(self.load_defaults().get("agent", {}))
        for field, value in values.items():
            normalized_value = self._normalize_agent_default_value(field, value)
            if normalized_value is None:
                current_agent_defaults.pop(field, None)
                continue
            current_agent_defaults[field] = normalized_value

        merged_settings = dict(self._settings)
        merged_defaults = merged_settings.get("defaults")
        if not isinstance(merged_defaults, dict):
            merged_defaults = {}

        if current_agent_defaults:
            merged_defaults["agent"] = current_agent_defaults
        else:
            merged_defaults.pop("agent", None)

        if merged_defaults:
            merged_settings["defaults"] = merged_defaults
        else:
            merged_settings.pop("defaults", None)

        self._settings = merged_settings
        return self.load_defaults()

    @staticmethod
    def _normalize_agent_default_value(field: str, value: Any) -> str | float | None:
        if value is None:
            return None

        if field in {"model", "fallback_model"}:
            if not isinstance(value, str):
                raise StorageError(f"Agent default {field} must be a string")
            return value

        if field == "temperature":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise StorageError("Agent default temperature must be a number or null")
            temperature = float(value)
            if not math.isfinite(temperature):
                raise StorageError("Agent default temperature must be finite")
            if temperature < delegates.MIN_TEMPERATURE or temperature > delegates.MAX_TEMPERATURE:
                raise StorageError(
                    "Agent default temperature must be between "
                    f"{delegates.MIN_TEMPERATURE:g} and {delegates.MAX_TEMPERATURE:g}"
                )
            return temperature

        if field == "thinking_effort":
            if not isinstance(value, str):
                raise StorageError("Agent default thinking_effort must be a string or null")
            if value not in delegates.ALLOWED_THINKING_EFFORTS:
                allowed = ", ".join(
                    repr(item) for item in sorted(delegates.ALLOWED_THINKING_EFFORTS)
                )
                raise StorageError(f"Agent default thinking_effort must be one of: {allowed}")
            return value

        raise StorageError(f"Unsupported defaults.agent setting: {field}")

    def load_settings(self) -> JsonObject:
        return dict(self._settings)

    def update_settings(
        self,
        mutator: Callable[[JsonObject], SettingsUpdateResult],
    ) -> SettingsUpdateResult:
        merged_settings = dict(self._settings)
        result = mutator(merged_settings)
        self.save_settings(merged_settings)
        return result

    def update_settings_sections(self, settings_update: JsonObject) -> JsonObject:
        updated_sections: JsonObject = {}
        if "appearance" in settings_update:
            updated_sections["appearance"] = self.update_appearance_settings(
                settings_update["appearance"]
            )
        if "skills" in settings_update:
            updated_sections["skills"] = {
                "directories": self.update_skill_directory_settings(
                    settings_update["skills"]["directories"]
                )
            }
        if "subagents" in settings_update:
            subagents = settings_update["subagents"]
            merged_settings = dict(self._settings)
            for field in STUB_SUBAGENT_SETTING_FIELDS:
                merged_settings[field] = subagents[field]
            self.save_settings(merged_settings)
            updated_sections["subagents"] = {
                field: subagents[field] for field in STUB_SUBAGENT_SETTING_FIELDS
            }
        if "compaction" in settings_update:
            updated_sections["compaction"] = self.update_compaction_settings(
                settings_update["compaction"]
            )
        if "defaults" in settings_update:
            defaults_update = settings_update["defaults"]
            if "agent" in defaults_update:
                updated_sections["defaults"] = self.update_defaults(
                    "agent",
                    defaults_update["agent"],
                )
        if "recall" in settings_update:
            updated_sections["recall"] = self.update_recall_settings(settings_update["recall"])
        if "web_search" in settings_update:
            updated_sections["web_search"] = self.update_web_search_settings(
                settings_update["web_search"]
            )
        if "model_tasks" in settings_update:
            self._settings = {**self._settings, "model_tasks": settings_update["model_tasks"]}
            updated_sections["model_tasks"] = self.load_model_task_settings()
        return updated_sections

    def save_settings(self, settings: JsonObject) -> None:
        self._settings = dict(settings)

    def load_environment(self) -> dict[str, str]:
        return dict(self._credentials)

    def set_data_dir_credential(self, key: str, value: str) -> None:
        self._credentials[key] = value

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

    def copy_agent_prompt_fragments(self, agent_id: str, *, overwrite: bool = False) -> list[Path]:
        written_paths: list[Path] = []
        for name, content in sorted(self._prompt_fragments.items()):
            key = (agent_id, name)
            if key in self._agent_prompt_fragments and not overwrite:
                continue
            self._agent_prompt_fragments[key] = content
            path = self.agent_prompts_dir(agent_id) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written_paths.append(path)
        return written_paths

    def agent_prompts_dir(self, agent_id: str) -> Path:
        return self.data_dir / "agents" / agent_id / "prompts"

    def agent_prompt_fragment_exists(self, agent_id: str, name: str) -> bool:
        return (agent_id, name) in self._agent_prompt_fragments

    def read_agent_prompt_fragment(self, agent_id: str, name: str) -> str:
        return self._agent_prompt_fragments.get((agent_id, name), "")

    def write_agent_prompt_fragment(self, agent_id: str, name: str, content: str) -> None:
        if name not in self._prompt_fragments:
            raise StorageError(f"Unknown prompt fragment: {name}")
        self._agent_prompt_fragments[(agent_id, name)] = content
        path = self.agent_prompts_dir(agent_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def reset_agent_prompt_fragment(self, agent_id: str, name: str) -> None:
        if name not in self._prompt_fragments:
            raise StorageError(f"Unknown prompt fragment: {name}")
        self.write_agent_prompt_fragment(agent_id, name, self._prompt_fragments[name])


class StubPrompts:
    app_dir = Path("app")

    def build_system_prompt(self, agent: StubAgent, scope: object = None) -> str:
        if getattr(scope, "type", None) == "agent":
            scope_agent_id = getattr(scope, "agent_id", None)
            return f"Custom system for {scope_agent_id}"
        if scope is None and agent.custom_system_prompt_enabled:
            return f"Effective custom system for {agent.id}"
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

    def availability_for(self, _name: str) -> Any:
        return SimpleNamespace(state="available", missing=(), optional_missing=())

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

    def availability_for(self, _name: str) -> Any:
        return SimpleNamespace(state="available", missing=(), optional_missing=())

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


class StubProcessManager:
    def cancel_scope(self, run_id: str) -> None:
        del run_id


class RecordingCompactionService:
    def __init__(self) -> None:
        self.calls = 0

    async def compact(self, *args: Any, **kwargs: Any) -> ChatMessage:
        self.calls += 1
        return ChatMessage.compaction_checkpoint(
            summary="Compacted context",
            tail_boundary_id="tail-boundary",
            compacted_token_count=1,
        )


class StubRuntime:
    def __init__(self, tmp_path: Path, adapter: StubAdapter) -> None:
        self.resources_dir = tmp_path / "resources"
        self.storage = StubStorage(tmp_path)
        self.agents = StubAgents(
            StubAgent(id="coder", allowed_tools=["*"]),
            defaults_provider=lambda: self.storage.load_defaults().get("agent", {}),
        )
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.system_prompts = StubPrompts()
        self.tools = ToolRegistry()
        self.skills: Any = StubSkills()
        self._models = StubModels()
        self.providers = StubProviders()
        self.adapter = adapter
        self.chat_runs: ChatRunManager | None = None
        self.extensions: Any = None
        self.process_manager = StubProcessManager()
        self.trigger_service: Any = None
        self.recall_reload_count = 0
        self.chat_loop = ChatLoop(cast(Any, self))
        self.streaming_chat_loop = ChatLoop(cast(Any, self), streaming=True)
        self.command_dispatcher = CommandDispatcher(
            self.chat_run_manager,
            agents=cast(Any, self.agents),
            sessions=self.chat_sessions,
            models=cast(Any, self._models),
        )

    @property
    def chat_run_manager(self) -> ChatRunManager:
        if self.chat_runs is None:
            self.chat_runs = ChatRunManager()
        return self.chat_runs

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
            bool(self._credential_value(connection.auth.credential_key))
            for connection in provider.connections
        )

    def _credential_value(self, key: str) -> str:
        if key in os.environ:
            return os.environ[key]
        return self.storage.load_environment().get(key, "")

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
                return bool(runtime._credential_value(connection.auth.credential_key))

            def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str:
                provider = cast(Any, runtime.providers.get(provider_id))
                if connection_id is None:
                    for connection in provider.connections:
                        credential = runtime._credential_value(connection.auth.credential_key)
                        if credential:
                            return credential
                    raise ConfigError(
                        f"Provider credentials not found for provider '{provider_id}'"
                    )
                local_id = connection_id.removeprefix(f"{provider_id}:")
                connection = next(
                    connection for connection in provider.connections if connection.id == local_id
                )
                credential = runtime._credential_value(connection.auth.credential_key)
                if credential:
                    return credential
                raise ConfigError(f"Provider credentials not found for provider '{provider_id}'")

        return CredentialResolver()

    def _resolve_resources_path(self) -> Path:
        return self.resources_dir

    def reload_skills(self) -> None:
        self.skills = ReloadableStubRuntimeSkills(self)

    def reload_recall_backend(self) -> None:
        self.recall_reload_count += 1

    def reload_provider_credentials(self) -> None:
        return None


def make_state(
    tmp_path: Path,
    adapter: StubAdapter,
    *,
    compaction_service: Any | None = None,
) -> SimpleNamespace:
    runtime: Any = StubRuntime(tmp_path, adapter)
    chat_runs = ChatRunManager()
    runtime.chat_runs = chat_runs
    chat_loop = ChatLoop(runtime, compaction_service=compaction_service)
    streaming_chat_loop = ChatLoop(runtime, streaming=True, compaction_service=compaction_service)
    runtime.streaming_chat_loop = streaming_chat_loop
    runtime.trigger_service = TriggerService(chat_loop, chat_runs, cast(Any, runtime))
    return SimpleNamespace(
        runtime=runtime,
        chat_runs=chat_runs,
        chat_loop=chat_loop,
        streaming_chat_loop=streaming_chat_loop,
        command_dispatcher=CommandDispatcher(
            chat_runs,
            agents=cast(Any, runtime.agents),
            sessions=runtime.chat_sessions,
            models=cast(Any, runtime.models),
        ),
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
                            "connectable": False,
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
        "defaults": {},
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
        "debug": {
            "enabled": False,
            "trace_limit": 50,
            "trace_count": 0,
        },
        "model_tasks": {},
        "skills": {
            "default_directory": str(tmp_path / "skills"),
            "directories": [],
        },
    }
    assert "sk-live-secret" not in str(response)
    assert "show_token_counts" not in str(response)
    assert "origin" not in response["result"]["general"]["server"]


@pytest.mark.asyncio
async def test_settings_get_marks_device_flow_oauth_connections_connectable(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="github-copilot",
            name="GitHub Copilot",
            base_url="https://api.githubcopilot.com",
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="oauth",
                    type="oauth",
                    label="Sign in with GitHub",
                    auth=SimpleNamespace(credential_key=""),
                    oauth=SimpleNamespace(flow="device"),
                )
            ],
        )
    )
    state.runtime.models._models["github-copilot"] = []

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True
    provider = next(
        item for item in response["result"]["providers"]["items"] if item["id"] == "github-copilot"
    )
    assert provider["connections"] == [
        {
            "id": "github-copilot:oauth",
            "type": "oauth",
            "label": "Sign in with GitHub",
            "configured": False,
            "connectable": True,
        }
    ]


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
async def test_settings_get_includes_defaults_key_when_unconfigured(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

    assert response["ok"] is True
    assert "defaults" in response["result"]
    assert response["result"]["defaults"] == {}


@pytest.mark.asyncio
async def test_settings_get_rejects_params(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.get", "params": {"extra": True}})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_get_raw_returns_raw_settings_payload(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings(
        {
            "server_port": 9001,
            "feature_flags": {"logs": True},
        }
    )

    response = await dispatch_rpc(state, {"method": "settings.get_raw", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "settings": {
                "server_port": 9001,
                "feature_flags": {"logs": True},
            }
        },
    }


@pytest.mark.asyncio
async def test_settings_set_key_updates_settings_and_returns_raw_payload(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.save_settings({"server_host": "127.0.0.1"})

    response = await dispatch_rpc(
        state,
        {"method": "settings.set_key", "params": {"key": "server_port", "value": 9000}},
    )

    assert response == {
        "ok": True,
        "result": {
            "settings": {
                "server_host": "127.0.0.1",
                "server_port": 9000,
            }
        },
    }
    assert state.runtime.storage.load_settings() == {
        "server_host": "127.0.0.1",
        "server_port": 9000,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"key": "compaction", "value": "hello"},
            r"\$\.compaction: must be an object",
        ),
        (
            {"key": "attachment_max_size_bytes", "value": -1},
            r"\$\.attachment_max_size_bytes: must be a positive integer",
        ),
        (
            {"key": "server_port", "value": 0},
            r"\$\.server_port: must be between 1 and 65535",
        ),
    ],
)
async def test_settings_set_key_rejects_invalid_raw_settings_without_partial_write(
    tmp_path: Path,
    params: JsonObject,
    message: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_settings = {"server_port": 9000, "feature_flags": {"logs": True}}
    state.runtime.storage.save_settings(original_settings)

    response = await dispatch_rpc(state, {"method": "settings.set_key", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert re.search(message, response["error"]["message"])
    assert state.runtime.storage.load_settings() == original_settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"key": "server_port"},
        {"value": 9000},
    ],
)
async def test_settings_set_key_rejects_missing_key_or_value(
    tmp_path: Path,
    params: JsonObject,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "settings.set_key", "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert response["error"]["message"] == "settings.set_key requires 'key' and 'value'"


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
async def test_provider_set_key_writes_api_key_credential_and_reloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "sk-or-test"},
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "credential_key": "OPENROUTER_API_KEY",
            "configured": True,
        },
    }
    assert state.runtime.storage.load_environment() == {"OPENROUTER_API_KEY": "sk-or-test"}
    assert state.runtime.provider_credentials.has_credentials("openrouter", "openrouter:api-key")


@pytest.mark.asyncio
async def test_provider_set_key_rejects_oauth_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider_with_secondary_connection())

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {
                "provider_id": "openrouter",
                "connection_id": "openrouter:oauth",
                "value": "secret",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "not an API key connection" in response["error"]["message"]


@pytest.mark.asyncio
async def test_provider_set_key_rejects_ambiguous_api_key_connection(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    provider = openrouter_provider()
    provider.connections.append(
        SimpleNamespace(
            id="secondary",
            type="api_key",
            label="Secondary API Key",
            auth=SimpleNamespace(credential_key="OPENROUTER_SECONDARY_API_KEY"),
        )
    )
    state.runtime.providers.add(provider)

    response = await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {"provider_id": "openrouter", "value": "secret"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "multiple API key connections" in response["error"]["message"]


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
                    "context_window": 200000,
                    "max_output_tokens": 64000,
                    "connections": [],
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
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "max_output_tokens": 8192,
                    "connections": [],
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
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "max_output_tokens": 16000,
                    "connections": [],
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
                    "connections": [],
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
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "max_output_tokens": 16000,
                    "connections": [],
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
                    "connections": [],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_model_list_outputs_per_model_connections_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model.list`` propagates the per-model ``connections`` allowlist
    from the registry into the RPC payload. The WebUI uses this list to
    decide which provider connections to offer for a given model — a
    model tagged ``["subscription"]`` is not offered on ``api-key``."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"] = [
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
        ),
        Model(
            model_id="gpt-5.5",
            name="GPT-5.5",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("subscription",),
        ),
    ]

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response["ok"] is True
    by_id = {model["id"]: model for model in response["result"]["models"]}
    assert by_id["openai/gpt-5.2"]["connections"] == ["api-key"]
    assert by_id["openai/gpt-5.5"]["connections"] == ["subscription"]


@pytest.mark.asyncio
async def test_model_list_outputs_empty_connections_for_unrestricted_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model with no ``connections`` allowlist surfaces ``connections``
    as an empty list — the WebUI treats that as "valid for every
    connection of the provider"."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response["ok"] is True
    for model in response["result"]["models"]:
        assert model["connections"] == []


@pytest.mark.asyncio
async def test_model_list_filters_by_task_and_modality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"].append(
        Model(
            model_id="gpt-image",
            name="GPT Image",
            capabilities=Capabilities(
                vision=True,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text", "image"),
                output_modalities=("text", "image"),
            ),
            context_window=128000,
            max_output_tokens=32000,
        )
    )

    image_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"task": "image_generation"}},
    )
    audio_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"output_modality": "audio"}},
    )
    context_response = await dispatch_rpc(
        state,
        {
            "method": "model.list",
            "params": {"capability": "tools", "min_context_window": 200000},
        },
    )

    assert [model["id"] for model in image_response["result"]["models"]] == ["openai/gpt-image"]
    assert audio_response["result"]["models"] == []
    assert [model["id"] for model in context_response["result"]["models"]] == ["openai/gpt-5.2"]


@pytest.mark.asyncio
async def test_model_list_filters_by_provider_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    state = make_state(tmp_path, StubAdapter())

    openai_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "openai"}},
    )
    anthropic_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "anthropic"}},
    )
    uppercase_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "OpenAI"}},
    )
    unknown_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "nonexistent"}},
    )

    assert [model["id"] for model in openai_response["result"]["models"]] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-5.2",
    ]
    assert [model["id"] for model in anthropic_response["result"]["models"]] == [
        "anthropic/claude-sonnet-4-20250219"
    ]
    assert [model["id"] for model in uppercase_response["result"]["models"]] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-5.2",
    ]
    assert unknown_response["result"]["models"] == []


@pytest.mark.asyncio
async def test_model_list_rejects_unsupported_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "openai", "extra": True}},
    )

    assert response == {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": "unsupported model.list fields: extra",
        },
    }


@pytest.mark.asyncio
async def test_model_list_rejects_invalid_filter_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"min_context_window": -1}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "non-negative integer" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_list_delegates_filtering_to_model_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RPC result must match what ``ModelQuery.from_filters`` + ``query`` produce.

    This locks in the byte-identical contract while routing filtering through
    the core query instead of duplicating it in the RPC layer.
    """

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "model.list",
            "params": {"task": "image_generation", "min_context_window": 1000},
        },
    )

    # Cross-check the RPC result against the core query path directly. If
    # either path diverges, this test fails — making the "delegate to the
    # core query" contract enforced.
    expected = sorted(
        (
            (
                provider_id,
                _model_response(provider_id, model),
            )
            for provider_id, model in state.runtime.models.query(
                ModelQuery.from_filters({"task": "image_generation", "min_context_window": 1000})
            )
            if _provider_has_credentials(state.runtime, provider_id)
        ),
        key=lambda item: (item[1]["provider_id"], item[1]["model_id"]),
    )
    expected_models = [item[1] for item in expected]

    assert response["result"]["models"] == expected_models


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
async def test_model_refresh_db_iterates_every_refreshable_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with multiple endpoint-bearing credentialed connections is
    refreshed once per connection.

    Confirms the RPC layer walks the full connection list rather than
    stopping at the first usable one. The registry is reloaded exactly
    once at the end of the call, and the merged catalog is the union of
    every connection's result (here all the same stub ``fresh-model``).
    """

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("OPENAI_SECONDARY_KEY", "secondary-key")
    monkeypatch.setattr(delegates, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            defaults={},
            extra_headers={},
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_PRIMARY_KEY"),
                ),
                SimpleNamespace(
                    id="secondary",
                    type="api_key",
                    label="Secondary",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_SECONDARY_KEY"),
                ),
                SimpleNamespace(
                    id="missing-creds",
                    type="api_key",
                    label="Missing Credentials",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_MISSING_KEY"),
                ),
            ],
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
    )

    assert response["ok"] is True, response
    # Two refreshes — the third connection has no credentials.
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openai", "openai"]
    assert sorted(FAKE_REFRESH_MODEL_CALLS) == ["primary-key", "secondary-key"]
    connection_ids = [kwargs["credential_connection"].id for kwargs in FAKE_REFRESH_MODEL_KWARGS]
    assert connection_ids == ["api-key", "secondary"]
    # The registry reloads once and the merged catalog is readable.
    refreshed_model = state.runtime.models.get("openai", "fresh-model")
    assert refreshed_model.name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_skips_connections_without_effective_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection without an effective ``models_endpoint`` is silently skipped.

    Provider-level ``models_endpoint=None`` and connection-level
    ``models_endpoint=None`` together mean there is no catalog to fetch,
    so the connection is excluded from the iteration — even when it has
    valid credentials.
    """

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setattr(delegates, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            defaults={},
            extra_headers={},
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    base_url=None,
                    models_endpoint=None,
                    auth=SimpleNamespace(credential_key="OPENAI_PRIMARY_KEY"),
                ),
            ],
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "provider 'openai' does not support model refresh" in response["error"]["message"]
    assert FAKE_REFRESH_MODEL_CALLS == []


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
                    "state": "available",
                    "requirements": {"missing": [], "optional_missing": []},
                },
                {
                    "name": "warned",
                    "description": "Loads with warnings.",
                    "valid": False,
                    "warnings": ["Name does not match directory."],
                    "state": "available",
                    "requirements": {"missing": [], "optional_missing": []},
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
async def test_settings_update_persists_compaction_settings_and_returns_full_payload(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "compaction": {
                    "auto": False,
                    "threshold": 0.9,
                    "tail_tokens": 12000,
                    "summary_model": "openai/gpt-5.2",
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_compaction_settings() == {
        "auto": False,
        "threshold": 0.9,
        "tail_tokens": 12000,
        "summary_model": "openai/gpt-5.2",
    }
    assert response["result"]["compaction"] == {
        "auto": False,
        "threshold": 0.9,
        "tail_tokens": 12000,
        "summary_model": "openai/gpt-5.2",
    }


@pytest.mark.asyncio
async def test_settings_update_persists_recall_backend_and_reloads_runtime(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "recall": {
                    "backend": "sqlite_fts",
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_recall_settings() == {"backend": "sqlite_fts"}
    assert state.runtime.recall_reload_count == 1
    assert response["result"]["recall"] == {
        "backend": "sqlite_fts",
        "available_backends": ["hybrid", "jsonl_scan", "sqlite_fts", "vector"],
    }


@pytest.mark.asyncio
async def test_settings_update_accepts_vector_recall_backend(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "recall": {
                    "backend": "vector",
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_recall_settings() == {"backend": "vector"}
    assert state.runtime.recall_reload_count == 1
    assert response["result"]["recall"]["backend"] == "vector"
    assert "vector" in response["result"]["recall"]["available_backends"]


@pytest.mark.asyncio
async def test_settings_update_persists_web_search_provider(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "web_search": {
                    "provider": "searxng",
                    "searxng": {"base_url": "http://localhost:9999"},
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_web_search_settings() == {
        "provider": "searxng",
        "searxng": {"base_url": "http://localhost:9999"},
    }
    assert response["result"]["web_search"] == {
        "provider": "searxng",
        "available_providers": ["brave", "searxng"],
        "searxng": {"base_url": "http://localhost:9999"},
    }


@pytest.mark.asyncio
async def test_settings_update_persists_agent_default_model_and_returns_defaults(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "defaults": {
                    "agent": {
                        "model": "openai/gpt-4.1-mini",
                    }
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_defaults() == {"agent": {"model": "openai/gpt-4.1-mini"}}
    assert response["result"]["defaults"] == {"agent": {"model": "openai/gpt-4.1-mini"}}


@pytest.mark.asyncio
async def test_settings_update_removes_agent_default_temperature_on_null(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_defaults(
        "agent",
        {
            "model": "openai/gpt-4.1-mini",
            "temperature": 0.6,
        },
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "defaults": {
                    "agent": {
                        "temperature": None,
                    }
                }
            },
        },
    )

    assert response["ok"] is True, response
    assert state.runtime.storage.load_defaults() == {"agent": {"model": "openai/gpt-4.1-mini"}}
    assert response["result"]["defaults"] == {"agent": {"model": "openai/gpt-4.1-mini"}}


@pytest.mark.asyncio
async def test_settings_update_rejects_unknown_defaults_agent_field(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "defaults": {
                    "agent": {
                        "unknown_field": True,
                    }
                }
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "unsupported defaults.agent settings" in response["error"]["message"]


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
                    "state": "available",
                    "requirements": {"missing": [], "optional_missing": []},
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
        {"recall": []},
        {"recall": {"extra": True}},
        {"recall": {"backend": "unknown_backend"}},
        {"web_search": []},
        {"web_search": {"provider": "unknown"}},
        {"web_search": {"provider": "searxng", "searxng": {"base_url": ""}}},
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
async def test_settings_update_rejects_compaction_threshold_out_of_range(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "compaction": {
                    "auto": True,
                    "threshold": 1.5,
                    "tail_tokens": 15000,
                    "summary_model": None,
                }
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "params.compaction.threshold" in response["error"]["message"]


@pytest.mark.asyncio
async def test_settings_update_maps_storage_section_error_without_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    original_settings = {
        "appearance": {"language": "en", "theme": "legacy"},
        "server_port": 8500,
    }
    state.runtime.storage.save_settings(original_settings)

    def fail_settings_update(_settings_update: object) -> JsonObject:
        raise StorageError("compaction write failed")

    monkeypatch.setattr(
        state.runtime.storage,
        "update_settings_sections",
        fail_settings_update,
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "appearance": {"language": "en"},
                "compaction": {
                    "auto": False,
                    "threshold": 0.9,
                    "tail_tokens": 12000,
                    "summary_model": None,
                },
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert response["error"]["message"] == "compaction write failed"
    assert state.runtime.storage.load_settings() == original_settings


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
    assert create_response["result"]["custom_system_prompt_enabled"] is False
    assert create_response["result"]["memory_prompt_mode"] == "agent_user"
    assert update_response["result"]["name"] == "Updated Writer"
    assert delete_response["result"]["agent_id"] == "writer"


@pytest.mark.asyncio
async def test_agent_update_enabling_custom_prompt_seeds_agent_fragments(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.write_prompt_fragment("system.md", "custom default system")

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "custom_system_prompt_enabled": True},
        },
    )

    assert response["ok"] is True
    assert response["result"]["custom_system_prompt_enabled"] is True
    assert (
        state.runtime.storage.read_agent_prompt_fragment("coder", "system.md")
        == "custom default system"
    )


@pytest.mark.asyncio
async def test_agent_update_reenabling_custom_prompt_preserves_agent_fragments(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)
    state.runtime.storage.write_agent_prompt_fragment("coder", "system.md", "agent custom")
    state.runtime.agents.update("coder", custom_system_prompt_enabled=False)

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.update",
            "params": {"id": "coder", "custom_system_prompt_enabled": True},
        },
    )

    assert response["ok"] is True
    assert state.runtime.storage.read_agent_prompt_fragment("coder", "system.md") == "agent custom"


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
async def test_agent_update_accepts_null_temperature_to_clear_override(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", temperature=0.9)

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "temperature": None}},
    )

    assert response["ok"] is True
    assert response["result"]["temperature"] is None
    assert state.runtime.agents.get("coder").temperature is None


@pytest.mark.asyncio
async def test_agent_update_accepts_memory_prompt_mode(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "memory_prompt_mode": "off"}},
    )

    assert response["ok"] is True
    assert response["result"]["memory_prompt_mode"] == "off"
    assert state.runtime.agents.get("coder").memory_prompt_mode == "off"


@pytest.mark.asyncio
async def test_agent_get_reflects_configured_default_model(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_defaults("agent", {"model": "openai/gpt-4.1-mini"})
    state.runtime.agents.update("coder", model="")

    response = await dispatch_rpc(
        state,
        {"method": "agent.get", "params": {"id": "coder"}},
    )

    assert response["ok"] is True
    assert response["result"]["id"] == "coder"
    assert response["result"]["model"] == "openai/gpt-4.1-mini"


@pytest.mark.asyncio
async def test_agent_create_returns_and_publishes_resolved_defaults(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.storage.update_defaults(
        "agent",
        {
            "model": "openai/gpt-5.2",
            "temperature": 0.6,
            "thinking_effort": "high",
        },
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {
                "id": "writer",
                "name": "Writer",
                "model": "",
                "temperature": None,
                "thinking_effort": None,
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["model"] == "openai/gpt-5.2"
    assert response["result"]["temperature"] == 0.6
    assert response["result"]["thinking_effort"] == "high"
    assert response["result"]["context_window"] == 256000

    raw_agent = state.runtime.agents._get_raw("writer")
    assert raw_agent.model == ""
    assert raw_agent.temperature is None
    assert raw_agent.thinking_effort is None

    assert len(state.event_bus.events) == 1
    event = state.event_bus.events[0]
    assert event["type"] == "agent.created"
    assert event["payload"]["id"] == "writer"
    assert event["payload"]["model"] == "openai/gpt-5.2"
    assert event["payload"]["temperature"] == 0.6
    assert event["payload"]["thinking_effort"] == "high"
    assert event["payload"]["context_window"] == 256000


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
        ("agent.update", {"id": "coder", "memory_prompt_mode": "sometimes"}),
        ("agent.update", {"id": "coder", "name": ""}),
        ("agent.update", {"id": "coder", "model": 5}),
        ("agent.update", {"id": "coder", "custom_system_prompt_enabled": "yes"}),
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
async def test_agent_create_rpc_rejects_workspace_field(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "agent.create",
            "params": {"id": "writer", "name": "Writer", "workspace": "C:/escape"},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_agent_update_rpc_accepts_workspace_mutation(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    workspace = tmp_path / "updated-workspace"

    response = await dispatch_rpc(
        state,
        {"method": "agent.update", "params": {"id": "coder", "workspace": str(workspace)}},
    )

    assert response["ok"] is True
    assert response["result"]["workspace"] == str(workspace.resolve())
    assert state.runtime.agents.get("coder").workspace == str(workspace.resolve())


@pytest.mark.asyncio
async def test_agent_delete_rejects_last_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "last_agent"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_active_run(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    release = asyncio.Event()
    coder = state.runtime.agents.get("coder")

    async def hold_run(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        agent_id="coder",
        session_id=coder.current_session_id,
        executor=hold_run,
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_busy"
    assert state.runtime.agents.get("coder").id == "coder"

    release.set()
    assert await run.wait() == "done"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_channel_reference(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.channel_service = SimpleNamespace(
        list_channels=lambda: [SimpleNamespace(id="tg-coder", agent_id="coder")]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "channel:tg-coder" in response["error"]["message"]
    assert state.runtime.agents.get("coder").id == "coder"


@pytest.mark.asyncio
async def test_agent_delete_rejects_agent_with_cron_reference(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("writer", "Writer")
    state.runtime.cron_service = SimpleNamespace(
        list_jobs=lambda: [SimpleNamespace(id="job-coder", agent_id="coder")]
    )

    response = await dispatch_rpc(state, {"method": "agent.delete", "params": {"id": "coder"}})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_in_use"
    assert "cron:job-coder" in response["error"]["message"]
    assert state.runtime.agents.get("coder").id == "coder"


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
        agent_id="coder",
        session_id="active-session",
        executor=_blocking_executor,
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
            tail_boundary_id=user_message.id,
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
    assert checkpoint["tail_boundary_id"] == user_message.id
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
    session.append(ChatMessage.run_summary(run_id="run-one", status="completed", timing=timing))

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
    assert "session does not exist" in response["error"]["message"]


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
    assert command_names == ["compact", "handoff", "help", "new", "retry", "status", "stop"]
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
    assert result["reply"].startswith("New session started: ")
    assert result["data"]["command"] == "new"
    new_session_id = result["data"]["session_id"]
    assert isinstance(new_session_id, str)
    assert new_session_id != "session-one"
    assert state.runtime.agents.get("coder").current_session_id == new_session_id
    assert state.runtime.chat_sessions.get("coder", new_session_id).load() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "streaming"),
    [("chat.send", False), ("chat.stream", True)],
)
async def test_chat_methods_handle_retry_command_as_run_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    streaming: bool,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    run = StubDelegateRun(
        run_id="run-retry",
        agent_id="coder",
        session_id="session-one",
        status="running" if streaming else "completed",
        final_message=ChatMessage.assistant(model="openai/gpt-5.2", content="Retried"),
    )
    captured: JsonObject = {}

    async def fake_retry_run(agent_id: str, session_id: str) -> StubDelegateRun:
        captured["agent_id"] = agent_id
        captured["session_id"] = session_id
        return run

    if streaming:
        monkeypatch.setattr(
            delegates,
            "_streaming_chat_loop",
            lambda _state: SimpleNamespace(retry_run=fake_retry_run),
        )
    else:
        monkeypatch.setattr(state.chat_loop, "retry_run", fake_retry_run)
    monkeypatch.setattr(delegates, "_bridge_run_to_event_bus", lambda _state, _run: None)

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": " /RETRY ",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["run_id"] == "run-retry"
    assert captured == {"agent_id": "coder", "session_id": "session-one"}
    if streaming:
        assert response["result"]["sse_url"] == "/api/runs/run-retry/events"
    else:
        assert response["result"]["message"]["content"] == "Retried"


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
        agent_id="coder",
        session_id="session-one",
        executor=_blocking_run_executor,
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

    assert response == {
        "ok": True,
        "result": {
            "command_handled": True,
            "reply": "Cannot compact while a run is active for this session.",
        },
    }
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

    assert response == {
        "ok": True,
        "result": {
            "command_handled": True,
            "reply": "Compaction is not available.",
        },
    }
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_compact_command_model_errors_as_command_reply(
    tmp_path: Path,
    method: str,
) -> None:
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService()
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", model="")

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

    assert response == {
        "ok": True,
        "result": {
            "command_handled": True,
            "reply": "Compaction failed: agent has no model set",
        },
    }
    assert compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []


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
    ) -> StubDelegateRun:
        captured["agent_id"] = agent_id
        captured["content"] = content
        captured["session_id"] = session_id
        captured["input_origin"] = input_origin
        return run

    class StubStreamingLoop:
        async def start_run(
            self,
            agent_id: str,
            content: str | list[Any],
            *,
            session_id: str,
            input_origin: str | None = None,
        ) -> StubDelegateRun:
            return await fake_start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
            )

    monkeypatch.setattr(state.chat_loop, "start_run", fake_start_run)
    monkeypatch.setattr(delegates, "_streaming_chat_loop", lambda _state: StubStreamingLoop())
    monkeypatch.setattr(delegates, "_bridge_run_to_event_bus", lambda _state, _run: None)

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
    assert "params.input_origin" in response["error"]["message"]


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
        },
        "display": {"summary": "note.txt", "hidden_argument_keys": []},
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

    removed = state.chat_runs.remove_queued("coder", "session-one", queued_item["id"])
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
        ) -> StubDelegateRun:
            captured["agent_id"] = agent_id
            captured["content"] = content
            captured["session_id"] = session_id
            return run

    runtime_streaming_loop = RuntimeStreamingLoop()
    state.streaming_chat_loop = runtime_streaming_loop
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
    runtime: Any = StubRuntime(tmp_path, StubAdapter())
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

    def test_visible_message_preserves_tool_timing(self) -> None:
        timing = {
            "started_at": "2026-05-03T14:30:01+00:00",
            "completed_at": "2026-05-03T14:30:02+00:00",
            "duration_ms": 1000,
        }
        message = ChatMessage.tool(
            tool_call_id="call-one",
            name="read",
            content='{"ok":true,"error":null,"data":{},"artifacts":[]}',
            timing=timing,
        )
        result = delegates._visible_message(message)
        assert result["timing"] == timing


class TestServerEventFromRunEvent:
    """Tests for _server_event_from_run_event preserving usage in run_completed."""

    def _make_event(
        self,
        event_type: str,
        payload: JsonObject | None = None,
        sequence: int = 1,
    ) -> Any:
        """Create a minimal RunEvent for testing."""
        from core.runs import RunEvent

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

    def test_terminal_event_includes_timing(self) -> None:
        timing = {
            "started_at": "2026-05-03T14:30:01+00:00",
            "completed_at": "2026-05-03T14:30:02+00:00",
            "duration_ms": 1000,
        }
        event = self._make_event(
            delegates.RUN_FAILED_EVENT,
            {"status": "failed", "timing": timing},
        )
        result = delegates._server_event_from_run_event(event)

        assert result["payload"]["status"] == "failed"
        assert result["payload"]["timing"] == timing

    def test_non_completed_terminal_event_excludes_usage(self) -> None:
        """run_failed and run_cancelled should not carry usage even if payload has it."""
        event = self._make_event(
            delegates.RUN_FAILED_EVENT,
            {"status": "failed", "usage": {"input_tokens": 10}},
        )
        result = delegates._server_event_from_run_event(event)

        assert "usage" not in result["payload"]
        assert result["payload"]["status"] == "failed"

    def test_run_started_includes_output_with_queue_item_id(self) -> None:
        """run_started WS summary surfaces queue_item_id so the client can drop the queued item."""
        from core.runs import RUN_STARTED_EVENT

        event = self._make_event(
            RUN_STARTED_EVENT,
            {"status": "running", "queue_item_id": "qi-abc-123"},
        )
        result = delegates._server_event_from_run_event(event)

        assert result["type"] == delegates.SERVER_EVENT_TYPES[RUN_STARTED_EVENT]
        assert result["payload"]["output"] == {
            "status": "running",
            "queue_item_id": "qi-abc-123",
        }

    def test_run_started_includes_output_without_queue_item_id(self) -> None:
        """run_started remains backward compatible when no queue_item_id is present."""
        from core.runs import RUN_STARTED_EVENT

        event = self._make_event(
            RUN_STARTED_EVENT,
            {"status": "running"},
        )
        result = delegates._server_event_from_run_event(event)

        assert result["type"] == delegates.SERVER_EVENT_TYPES[RUN_STARTED_EVENT]
        assert result["payload"]["output"] == {"status": "running"}


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
    runtime = fragments["runtime.md"]
    assert any(v["placeholder"] == "{app_version}" for v in runtime["variables"])
    tools = fragments["tools.md"]
    assert any(v["placeholder"] == "{tool_list}" for v in tools["variables"])


@pytest.mark.asyncio
async def test_prompt_list_includes_default_and_enabled_agent_scopes(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create(
        "custom",
        "Custom Agent",
        custom_system_prompt_enabled=True,
    )
    state.runtime.agents.create("plain", "Plain Agent")

    response = await dispatch_rpc(state, {"method": "prompt.list", "params": {}})

    assert response["ok"] is True
    assert response["result"]["scopes"] == [
        {"type": "default", "label": "Default"},
        {"type": "agent", "agent_id": "custom", "label": "Custom Agent"},
    ]


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
async def test_prompt_update_writes_enabled_agent_scope(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.update",
            "params": {
                "name": "system.md",
                "content": "Agent system",
                "scope": {"type": "agent", "agent_id": "coder"},
            },
        },
    )

    assert response["ok"] is True
    assert response["result"] == {
        "name": "system.md",
        "content": "Agent system",
        "is_modified": True,
    }
    assert state.runtime.storage.read_agent_prompt_fragment("coder", "system.md") == "Agent system"


@pytest.mark.asyncio
async def test_prompt_list_reads_missing_agent_fragment_as_empty(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.list",
            "params": {"scope": {"type": "agent", "agent_id": "coder"}},
        },
    )

    assert response["ok"] is True
    fragments = {fragment["name"]: fragment for fragment in response["result"]["fragments"]}
    assert fragments["skills.md"]["content"] == ""
    assert fragments["skills.md"]["is_modified"] is False


@pytest.mark.asyncio
async def test_prompt_list_rejects_disabled_agent_scope(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.list",
            "params": {"scope": {"type": "agent", "agent_id": "coder"}},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


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
async def test_prompt_reset_agent_scope_uses_current_default_content(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)
    state.runtime.storage.write_prompt_fragment("skills.md", "Default skills now")
    state.runtime.storage.write_agent_prompt_fragment("coder", "skills.md", "Agent skills")

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.reset",
            "params": {
                "name": "skills.md",
                "scope": {"type": "agent", "agent_id": "coder"},
            },
        },
    )

    assert response["ok"] is True
    assert response["result"] == {
        "name": "skills.md",
        "content": "Default skills now",
        "is_modified": True,
    }


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
async def test_prompt_preview_without_scope_uses_effective_agent_prompt(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {"method": "prompt.preview", "params": {"agent_id": "coder"}},
    )

    assert response["ok"] is True
    assert response["result"]["text"] == "Effective custom system for coder"


@pytest.mark.asyncio
async def test_prompt_preview_explicit_default_scope_uses_default_prompt(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.preview",
            "params": {"agent_id": "coder", "scope": {"type": "default"}},
        },
    )

    assert response["ok"] is True
    assert response["result"]["text"] == "System for coder"


@pytest.mark.asyncio
async def test_prompt_preview_uses_enabled_agent_scope(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", custom_system_prompt_enabled=True)

    response = await dispatch_rpc(
        state,
        {
            "method": "prompt.preview",
            "params": {"scope": {"type": "agent", "agent_id": "coder"}},
        },
    )

    assert response["ok"] is True
    assert response["result"]["text"] == "Custom system for coder"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_handoff_command_for_same_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    bridged_runs: list[Any] = []
    monkeypatch.setattr(
        delegates,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/handoff",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["command_handled"] is True
    assert result["reply"] == f"Handoff sent to coder, session {result['data']['session_id']}."
    assert result["data"]["command"] == "handoff"
    assert result["data"]["agent_id"] == "coder"
    new_session_id = result["data"]["session_id"]
    assert isinstance(new_session_id, str)
    assert new_session_id != "session-one"
    assert state.runtime.agents.get("coder").current_session_id == new_session_id
    # The injected run was started (and bridged) in the new session.
    assert len(bridged_runs) == 1
    assert bridged_runs[0].agent_id == "coder"
    assert bridged_runs[0].session_id == new_session_id
    # Wait for the receiving run to write its user message and finish.
    await bridged_runs[0].wait()
    new_session = state.runtime.chat_sessions.get("coder", new_session_id)
    new_history = new_session.load()
    user_messages = [message for message in new_history if message.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "OK"
    # The handoff-writing run used a system-reminder note on the source session.
    source_history = state.runtime.chat_sessions.get("coder", "session-one").load()
    assert any(message.role == "note" for message in source_history)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_handoff_command_for_other_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("reviewer", name="Reviewer")
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    bridged_runs: list[Any] = []
    monkeypatch.setattr(
        delegates,
        "_bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/handoff reviewer",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["command_handled"] is True
    assert result["data"]["command"] == "handoff"
    assert result["data"]["agent_id"] == "reviewer"
    new_session_id = result["data"]["session_id"]
    assert state.runtime.agents.get("reviewer").current_session_id == new_session_id
    assert len(bridged_runs) == 1
    assert bridged_runs[0].agent_id == "reviewer"
    assert bridged_runs[0].session_id == new_session_id
    await bridged_runs[0].wait()
    new_session = state.runtime.chat_sessions.get("reviewer", new_session_id)
    new_history = new_session.load()
    user_messages = [message for message in new_history if message.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_handle_handoff_command_with_missing_target_agent(
    tmp_path: Path,
    method: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", current_session_id="session-one")

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "agent_id": "coder",
                "session_id": "session-one",
                "content": "/handoff ghost",
            },
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["command_handled"] is True
    assert "ghost" in result["reply"]
    assert "data" not in result
    # No new session was created and the source session remains current.
    sessions = state.runtime.chat_sessions.list_with_metadata("coder")
    assert [session["id"] for session in sessions] == ["session-one"]
    assert state.runtime.agents.get("coder").current_session_id == "session-one"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_chat_methods_reject_handoff_command_while_session_run_is_active(
    tmp_path: Path,
    method: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_run_executor(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    active_run = await state.chat_runs.start(
        agent_id="coder",
        session_id="session-one",
        executor=_blocking_run_executor,
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
                    "content": "/handoff",
                },
            },
        )
    finally:
        release.set()
        await active_run.wait()

    assert response == {
        "ok": True,
        "result": {
            "command_handled": True,
            "reply": "A handoff can be started after the current run finishes.",
        },
    }
    sessions = state.runtime.chat_sessions.list_with_metadata("coder")
    assert [session["id"] for session in sessions] == ["session-one"]
