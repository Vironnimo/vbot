"""Runtime and persistence doubles shared by server RPC tests."""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

from core.automation import ReflectionService, TriggerService
from core.chat import (
    ChatMessage,
    ChatSessionManager,
    CommandDispatcher,
)
from core.prompts import LayoutEntry
from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    ProviderAccount,
    account_id_from_credential_key,
    derive_credential_key,
    split_connection_id,
)
from core.runs import ChatRunManager
from core.settings import AGENT_DEFAULT_FIELDS
from core.settings.normalizers import normalize_extensions_settings
from core.settings.settings import parse_openrouter_routing
from core.storage import StorageError
from core.tools import FileReadState, ToolRegistry
from core.utils.errors import ConfigError
from server.events import ServerEventBus
from server.rpc import agent_methods
from tests.core.chat.chat_loop_support import build_chat_loop
from tests.server.rpc_test_support_common import (
    StubAgent,
    StubAgentResolver,
    StubAgents,
    StubModels,
    StubProjects,
    StubProviders,
)

JsonObject = dict[str, Any]
SettingsUpdateResult = TypeVar("SettingsUpdateResult")
STUB_SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)


class StubStorage:
    def __init__(self, tmp_path: Path) -> None:
        self.data_dir = tmp_path
        self.prompts_dir = tmp_path / "prompts"
        self._appearance = {"language": "en", "chat_width": "comfortable"}
        self._skill_directories: list[str] = []
        self._settings: JsonObject = {}
        self._credentials: dict[str, str] = {}
        self._prompt_fragments: dict[str, str] = {
            "runtime.md": "# Runtime\nDefault runtime info.",
            "tools.md": "# Tools\nDefault tools list.",
            "channels.md": "# Channels\nDefault channels list.",
            "skills.md": "# Skills\nDefault skills list.",
            "handoff.md": "Write a handoff for the next agent.",
            "learn.md": "Author a reusable skill from the source.",
        }
        self._agent_prompt_fragments: dict[tuple[str, str], str] = {}
        self._block_layouts: dict[str | None, list[LayoutEntry]] = {}

    def load_appearance_settings(self) -> JsonObject:
        return dict(self._appearance)

    def supported_appearance_languages(self) -> list[str]:
        return ["en"]

    def _apply_appearance_settings(self, appearance: JsonObject) -> JsonObject:
        unsupported_fields = sorted(set(appearance) - {"language", "chat_width"})
        if unsupported_fields:
            raise StorageError(f"unsupported appearance settings: {', '.join(unsupported_fields)}")
        language = appearance.get("language")
        if not isinstance(language, str) or not language:
            raise StorageError("Appearance language must be a non-empty string")
        if language != "en":
            raise StorageError(f"Unsupported appearance language: {language}")
        chat_width = appearance.get("chat_width")
        if chat_width not in {"comfortable", "wide", "full"}:
            chat_width = "comfortable"
        self._appearance = {"language": language, "chat_width": chat_width}
        return dict(self._appearance)

    def load_skill_directory_settings(self) -> list[str]:
        return list(self._skill_directories)

    def _apply_skill_directory_settings(self, directories: object) -> list[str]:
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
            "enabled": True,
            "trigger": {"type": "context_ratio", "threshold": 0.8},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 15_000,
                "summary_model": None,
            },
        }
        stored = self._settings.get("compaction")
        if not isinstance(stored, dict):
            return defaults
        return dict(stored)

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
            "default_count": 12,
            "searxng": {"base_url": "http://localhost:8888"},
        }
        if not isinstance(stored, dict):
            return defaults

        provider = stored.get("provider")
        if not isinstance(provider, str) or provider not in {"brave", "searxng"}:
            provider = "brave"

        default_count = stored.get("default_count")
        if not isinstance(default_count, int) or isinstance(default_count, bool):
            default_count = 12

        searxng = stored.get("searxng")
        if not isinstance(searxng, dict):
            searxng = {}
        base_url = searxng.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            base_url = "http://localhost:8888"

        return {
            "provider": provider,
            "default_count": default_count,
            "searxng": {"base_url": base_url.strip()},
        }

    def load_debug_settings(self) -> JsonObject:
        return {"enabled": False, "trace_limit": 50}

    def load_local_models_settings(self) -> JsonObject:
        stored = self._settings.get("local_models")
        if isinstance(stored, dict) and isinstance(stored.get("context_windows"), dict):
            return {"context_windows": dict(stored["context_windows"])}
        return {"context_windows": {}}

    def load_openrouter_routing_settings(self) -> JsonObject:
        providers = self._settings.get("providers")
        if not isinstance(providers, dict):
            return parse_openrouter_routing({})
        openrouter = providers.get("openrouter")
        if not isinstance(openrouter, dict):
            return parse_openrouter_routing({})
        return parse_openrouter_routing(openrouter.get("routing", {}))

    def load_reflection_settings(self) -> JsonObject:
        defaults: JsonObject = {
            "enabled": False,
            "memory_turn_interval": 10,
            "skill_tool_call_interval": 25,
        }
        stored = self._settings.get("reflection")
        if isinstance(stored, dict):
            defaults.update(stored)
        return defaults

    def load_model_task_settings(self) -> JsonObject:
        stored = self._settings.get("model_tasks")
        return dict(stored) if isinstance(stored, dict) else {}

    def load_session_title_settings(self) -> JsonObject:
        stored = self._settings.get("session_titles")
        if not isinstance(stored, dict):
            return {"enabled": False, "model": ""}
        return {
            "enabled": stored.get("enabled") is True,
            "model": str(stored.get("model") or ""),
        }

    def _apply_recall_settings(self, recall: object) -> JsonObject:
        if not isinstance(recall, dict):
            raise StorageError("Recall settings must be an object")

        self._settings = {
            **self._settings,
            "recall": dict(self.load_recall_settings() | recall),
        }
        return self.load_recall_settings()

    def _apply_web_search_settings(self, web_search: object) -> JsonObject:
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

    def _apply_compaction_settings(self, compaction: object) -> JsonObject:
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

    def _apply_defaults(self, section: str, values: object) -> JsonObject:
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
            if (
                temperature < agent_methods.MIN_TEMPERATURE
                or temperature > agent_methods.MAX_TEMPERATURE
            ):
                raise StorageError(
                    "Agent default temperature must be between "
                    f"{agent_methods.MIN_TEMPERATURE:g} and {agent_methods.MAX_TEMPERATURE:g}"
                )
            return temperature

        if field == "thinking_effort":
            if not isinstance(value, str):
                raise StorageError("Agent default thinking_effort must be a string or null")
            if value not in agent_methods.ALLOWED_THINKING_EFFORTS:
                allowed = ", ".join(
                    repr(item) for item in sorted(agent_methods.ALLOWED_THINKING_EFFORTS)
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
            updated_sections["appearance"] = self._apply_appearance_settings(
                settings_update["appearance"]
            )
        if "skills" in settings_update:
            updated_sections["skills"] = {
                "directories": self._apply_skill_directory_settings(
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
            updated_sections["compaction"] = self._apply_compaction_settings(
                settings_update["compaction"]
            )
        if "defaults" in settings_update:
            defaults_update = settings_update["defaults"]
            if "agent" in defaults_update:
                updated_sections["defaults"] = self._apply_defaults(
                    "agent",
                    defaults_update["agent"],
                )
        if "recall" in settings_update:
            updated_sections["recall"] = self._apply_recall_settings(settings_update["recall"])
        if "web_search" in settings_update:
            updated_sections["web_search"] = self._apply_web_search_settings(
                settings_update["web_search"]
            )
        if "model_tasks" in settings_update:
            self._settings = {**self._settings, "model_tasks": settings_update["model_tasks"]}
            updated_sections["model_tasks"] = self.load_model_task_settings()
        if "providers" in settings_update:
            current_providers = self._settings.get("providers")
            if not isinstance(current_providers, dict):
                current_providers = {}
            routing = parse_openrouter_routing(
                settings_update["providers"]["openrouter"]["routing"]
            )
            normalized = {
                **current_providers,
                "openrouter": {"routing": routing},
            }
            self._settings = {**self._settings, "providers": normalized}
            updated_sections["providers"] = normalized
        if "extensions" in settings_update:
            normalized = normalize_extensions_settings(settings_update["extensions"])
            self._settings = {**self._settings, "extensions": normalized}
            updated_sections["extensions"] = normalized
        if "reflection" in settings_update:
            merged_reflection = {
                **self.load_reflection_settings(),
                **dict(settings_update["reflection"]),
            }
            self._settings = {**self._settings, "reflection": merged_reflection}
            updated_sections["reflection"] = merged_reflection
        if "session_titles" in settings_update:
            normalized = {
                "enabled": settings_update["session_titles"]["enabled"],
                "model": settings_update["session_titles"].get("model", ""),
            }
            self._settings = {**self._settings, "session_titles": normalized}
            updated_sections["session_titles"] = normalized
        return updated_sections

    def load_extensions_settings(self) -> JsonObject:
        return normalize_extensions_settings(self._settings.get("extensions"))

    def save_settings(self, settings: JsonObject) -> None:
        self._settings = dict(settings)

    def load_environment(self) -> dict[str, str]:
        return dict(self._credentials)

    def set_data_dir_credential(self, key: str, value: str) -> None:
        self._credentials[key] = value

    def remove_data_dir_credential(self, key: str) -> bool:
        return self._credentials.pop(key, None) is not None

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

    def read_block_layout(self, scope: str | None) -> list[LayoutEntry]:
        return list(self._block_layouts.get(scope, []))

    def write_block_layout(self, scope: str | None, entries: list[LayoutEntry]) -> Path:
        self._block_layouts[scope] = list(entries)
        return self._layout_path(scope)

    def seed_agent_block_layout(
        self,
        agent_id: str,
        default_layout: list[LayoutEntry],
        *,
        overwrite: bool = False,
    ) -> Path | None:
        if agent_id in self._block_layouts and not overwrite:
            return None
        self._block_layouts[agent_id] = list(default_layout)
        layout_path = self._layout_path(agent_id)
        layout_path.parent.mkdir(parents=True, exist_ok=True)
        layout_path.write_text(
            json.dumps([{"id": entry.id, "enabled": entry.enabled} for entry in default_layout]),
            encoding="utf-8",
        )
        return layout_path

    def _layout_path(self, scope: str | None) -> Path:
        root = self.prompts_dir if scope is None else self.agent_prompts_dir(scope)
        return root / "layout.json"


class StubPrompts:
    app_dir = Path("app")

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    def validate_scope(self, scope: object = None) -> Any:
        # The preview handler validates an explicit scope through the manager; mirror
        # the real PromptScope shape the handler reads (``type`` / ``agent_id``).
        if isinstance(scope, dict) and scope.get("type") == "agent":
            return SimpleNamespace(type="agent", agent_id=scope.get("agent_id"))
        return SimpleNamespace(type="default", agent_id=None)

    def build_system_prompt(
        self,
        agent: StubAgent,
        scope: object = None,
        *,
        agent_body: str = "",
        project_context: object = None,
        agent_project_id: str | None = None,
        skill_registry: object = None,
        skill_catalog: object = None,
        read_paths: list[Path] | None = None,
        effective_tool_names: object = None,
        session_tool_grants: object = (),
    ) -> str:
        del agent_project_id
        if getattr(scope, "type", None) == "agent":
            scope_agent_id = getattr(scope, "agent_id", None)
            return f"Custom system for {scope_agent_id}"
        if scope is None and agent.custom_system_prompt_enabled:
            base = f"Effective custom system for {agent.id}"
        else:
            base = f"System for {agent.id}"
        # Echo the two project-preview inputs so wiring tests can assert they
        # reached the builder; both empty for an identity preview, leaving the
        # identity-path output byte-identical.
        extras = []
        if agent_body:
            extras.append(f"body={agent_body}")
        if project_context is not None:
            extras.append(f"project_cwd={getattr(project_context, 'cwd', '')}")
        return " ".join([base, *extras])

    def render_skill_catalog(self, _agent: StubAgent, skill_registry: object = None) -> Any:
        from core.prompts import PinnedSkillCatalog

        return PinnedSkillCatalog(catalog_text="")

    def provider_tool_definitions(
        self,
        _agent: StubAgent,
        *,
        skill_registry: object = None,
        skill_catalog: object = None,
        session_tool_grants: tuple[str, ...] = (),
    ) -> list[JsonObject]:
        return self._tools.provider_definitions(
            _agent.allowed_tools,
            session_grants=session_tool_grants,
        )


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

    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
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
            projection=[ChatMessage.user("tail")],
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
        self.agent_resolver = StubAgentResolver(self.agents)
        self.projects = StubProjects()
        self.chat_sessions = ChatSessionManager(tmp_path)
        self.file_read_state = FileReadState()
        self.tools = ToolRegistry()
        self.system_prompts = StubPrompts(self.tools)
        self.skills: Any = StubSkills()
        self._models = StubModels()
        self.providers = StubProviders()
        self.adapter = adapter
        self.chat_runs: ChatRunManager | None = None
        self.extensions: Any = None
        self.process_manager = StubProcessManager()
        self.trigger_service: Any = None
        self.recall_reload_count = 0
        self.extension_reload_count = 0
        self.extension_disabled_changes: list[set[str]] = []
        self.chat_loop = build_chat_loop(cast(Any, self))
        self.streaming_chat_loop = build_chat_loop(cast(Any, self), streaming=True)
        self.command_dispatcher = CommandDispatcher(
            self.chat_run_manager,
            agent_resolver=cast(Any, self.agent_resolver),
            sessions=self.chat_sessions,
            models=cast(Any, self._models),
            projects=cast(Any, self.projects),
            agents=cast(Any, self.agents),
            storage=cast(Any, self.storage),
        )

    @property
    def chat_run_manager(self) -> ChatRunManager:
        if self.chat_runs is None:
            self.chat_runs = ChatRunManager()
        return self.chat_runs

    def skills_for(self, _project_id: str | None = None, _agent_id: str | None = None) -> Any:
        return self.skills

    def project_skill_names(self, _project_id: str | None = None) -> frozenset[str]:
        return frozenset()

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

    def _connection(self, provider_id: str, local_connection_id: str) -> Any:
        provider = cast(Any, self.providers.get(provider_id))
        return next(
            connection
            for connection in provider.connections
            if connection.id == local_connection_id
        )

    @property
    def provider_credentials(self) -> Any:
        runtime = self

        class CredentialResolver:
            def list_accounts(
                self, provider_id: str, local_connection_id: str
            ) -> list[ProviderAccount]:
                connection = runtime._connection(provider_id, local_connection_id)
                if getattr(connection, "oauth", None) is not None:
                    return []
                base_key = connection.auth.credential_key
                accounts: dict[str, ProviderAccount] = {}
                sources: list[tuple[str, dict[str, str]]] = [
                    ("process_env", dict(os.environ)),
                    ("data_dir", runtime.storage.load_environment()),
                ]
                for source, mapping in sources:
                    for env_key, value in mapping.items():
                        account_id = account_id_from_credential_key(base_key, env_key)
                        if account_id is None or account_id in accounts:
                            continue
                        accounts[account_id] = ProviderAccount(
                            id=account_id,
                            usable=bool(value),
                            source=source,
                            credential_key=derive_credential_key(base_key, account_id),
                        )
                return sorted(
                    accounts.values(),
                    key=lambda account: (account.id != DEFAULT_ACCOUNT_ID, account.id),
                )

            def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
                if connection_id is None:
                    return runtime.has_provider_credentials(provider_id)
                local_id, account_id = split_connection_id(provider_id, connection_id)
                accounts = self.list_accounts(provider_id, local_id)
                if account_id is None:
                    return any(account.usable for account in accounts)
                return any(account.id == account_id and account.usable for account in accounts)

            def is_connection_enabled(
                self, provider_id: str, connection_id: str | None = None
            ) -> bool:
                return True

            def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
                return self.has_credentials(provider_id, connection_id)

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
                local_id, account_id = split_connection_id(provider_id, connection_id)
                accounts = self.list_accounts(provider_id, local_id)
                if account_id is not None:
                    accounts = [account for account in accounts if account.id == account_id]
                for account in accounts:
                    if account.usable:
                        return runtime._credential_value(account.credential_key)
                raise ConfigError(f"Provider credentials not found for provider '{provider_id}'")

        return CredentialResolver()

    def _resolve_resources_path(self) -> Path:
        return self.resources_dir

    def reload_skills(self) -> None:
        self.skills = ReloadableStubRuntimeSkills(self)

    def reload_recall_backend(self) -> None:
        self.recall_reload_count += 1

    async def reload_extensions(self) -> None:
        self.extension_reload_count += 1

    async def apply_extension_disabled_change(self, newly_disabled: set[str]) -> None:
        self.extension_disabled_changes.append(set(newly_disabled))

    def reload_environment_credentials(self) -> None:
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
    chat_loop = build_chat_loop(runtime, compaction_service=compaction_service)
    streaming_chat_loop = build_chat_loop(
        runtime, streaming=True, compaction_service=compaction_service
    )
    runtime.streaming_chat_loop = streaming_chat_loop
    runtime.trigger_service = TriggerService(chat_loop, chat_runs, cast(Any, runtime))
    runtime.reflection = ReflectionService(cast(Any, runtime))
    return SimpleNamespace(
        runtime=runtime,
        chat_runs=chat_runs,
        chat_loop=chat_loop,
        streaming_chat_loop=streaming_chat_loop,
        command_dispatcher=CommandDispatcher(
            chat_runs,
            agent_resolver=cast(Any, runtime.agent_resolver),
            sessions=runtime.chat_sessions,
            models=cast(Any, runtime.models),
            projects=cast(Any, runtime.projects),
            agents=cast(Any, runtime.agents),
            trigger_service=runtime.trigger_service,
            reflection_service=runtime.reflection,
            storage=cast(Any, runtime.storage),
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
