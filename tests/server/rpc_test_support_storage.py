"""Storage persistence double for RPC tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from core.prompts import LayoutEntry
from core.settings.normalizers import (
    normalize_agent_default_value,
    normalize_agent_defaults,
    normalize_extensions_settings,
    normalize_speech_settings,
)
from core.settings.settings import parse_openrouter_routing
from core.storage import DataDirectoryLayout, StorageError

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
        self.layout = DataDirectoryLayout(tmp_path)
        self.prompts_dir = tmp_path / "prompts"
        self._appearance = {
            "language": "en",
            "chat_width": "comfortable",
            "chat_working_mode": "normal",
        }
        self._skill_directories: list[str] = []
        self._settings: JsonObject = {}
        self._credentials: dict[str, str] = {}
        self._prompt_fragments: dict[str, str] = {
            "identity_runtime.md": "# Identity Environment\nDefault identity environment info.",
            "runtime.md": "# Runtime\nDefault runtime info.",
            "working_project.md": "# Working Project\nDefault working project info.",
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
        unsupported_fields = sorted(
            set(appearance) - {"language", "chat_width", "chat_working_mode"}
        )
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
        chat_working_mode = appearance.get("chat_working_mode")
        if chat_working_mode not in {"normal", "compact"}:
            chat_working_mode = "normal"
        self._appearance = {
            "language": language,
            "chat_width": chat_width,
            "chat_working_mode": chat_working_mode,
        }
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
            return {"backend": "canonical_scan"}

        backend = stored.get("backend")
        if not isinstance(backend, str) or not backend.strip():
            return {"backend": "canonical_scan"}
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
        if not isinstance(provider, str) or provider not in {
            "brave",
            "duckduckgo",
            "exa",
            "firecrawl",
            "perplexity",
            "searxng",
            "serper",
            "tavily",
        }:
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
            "skill_model_step_interval": 10,
        }
        stored = self._settings.get("reflection")
        if isinstance(stored, dict):
            defaults.update(stored)
        return defaults

    def load_speech_settings(self) -> JsonObject:
        return normalize_speech_settings(self._settings.get("speech"))

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
        normalized = normalize_agent_defaults(defaults.get("agent"))
        return {"agent": normalized} if normalized else {}

    def _apply_defaults(self, section: str, values: object) -> JsonObject:
        if section != "agent":
            raise StorageError(f"Unsupported defaults section: {section}")
        if not isinstance(values, dict):
            raise StorageError("Defaults values must be a mapping")

        current_agent_defaults = dict(self.load_defaults().get("agent", {}))
        for field, value in values.items():
            normalized_value = normalize_agent_default_value(field, value)
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
        if "speech" in settings_update:
            normalized = normalize_speech_settings(settings_update["speech"])
            self._settings = {**self._settings, "speech": normalized}
            updated_sections["speech"] = normalized
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
        if "server" in settings_update:
            updated_sections["server"] = {}
            if "keep_awake" in settings_update["server"]:
                keep_awake = settings_update["server"]["keep_awake"] is True
                self._settings = {**self._settings, "keep_awake": keep_awake}
                updated_sections["server"] = {"keep_awake": keep_awake}
            if "timezone" in settings_update["server"]:
                timezone = settings_update["server"]["timezone"]
                self._settings = {**self._settings, "timezone": timezone}
                updated_sections["server"]["timezone"] = timezone
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
