"""Storage manager for vBot data directories and prompt fragments."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.search_config import (
    DEFAULT_SEARXNG_BASE_URL,
    DEFAULT_WEB_SEARCH_PROVIDER,
    FIRST_PARTY_WEB_SEARCH_PROVIDERS,
)
from core.settings import SettingsValidationError, load_validated_settings_json
from core.utils.config import build_environment_snapshot, read_env_file
from core.utils.errors import VBotError

DEFAULT_DATA_DIR = Path.home() / ".vbot"
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROMPT_FRAGMENT_NAMES = frozenset(
    {
        "system.md",
        "runtime.md",
        "tools.md",
        "channels.md",
        "skills.md",
        "compaction.md",
    }
)
AGENT_PROMPT_FRAGMENT_NAMES = frozenset(
    {
        "system.md",
        "runtime.md",
        "tools.md",
        "channels.md",
        "skills.md",
    }
)
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
DEFAULT_APPEARANCE_LANGUAGE = "en"
SUPPORTED_APPEARANCE_LANGUAGES = frozenset({DEFAULT_APPEARANCE_LANGUAGE})
SUBAGENT_SETTING_DEFAULTS = {
    "max_subagent_depth": 4,
    "max_subagents_per_turn": 8,
    "subagent_timeout_minutes": 60,
}
DEFAULT_RECALL_SETTINGS = {"backend": "jsonl_scan"}
DEFAULT_WEB_SEARCH_SETTINGS = {
    "provider": DEFAULT_WEB_SEARCH_PROVIDER,
    "searxng": {"base_url": DEFAULT_SEARXNG_BASE_URL},
}
COMPACTION_SETTING_DEFAULTS: dict[str, Any] = {
    "auto": True,
    "threshold": 0.8,
    "tail_tokens": 15_000,
    "summary_model": None,
}
SUPPORTED_DEFAULTS_SECTIONS = frozenset({"agent"})
AGENT_DEFAULT_FIELDS = frozenset({"model", "fallback_model", "temperature", "thinking_effort"})
ALLOWED_THINKING_EFFORTS = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
PHASE_TWO_DIRECTORIES = (
    ".tmp",
    "agents",
    "archive",
    "attachments",
    "channels",
    "cron",
    "oauth",
    "prompts",
    "recall",
    "speech",
    "skills",
    "logs",
)


class ConfigProtocol(Protocol):
    """Minimal config interface used to resolve the data directory."""

    def get(self, key: str, default: Any = None) -> Any:
        """Return a config value."""


class StorageError(VBotError):
    """Raised for invalid storage data or unsafe storage paths."""


class StorageManager:
    """Owns Phase 2 data-directory setup, settings JSON, and prompt fragments."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        config: ConfigProtocol | None = None,
        resources_dir: str | Path | None = None,
    ) -> None:
        self.data_dir = self._resolve_data_dir(data_dir, config).expanduser()
        self.resources_dir = self._resolve_resources_dir(resources_dir)

    @property
    def settings_path(self) -> Path:
        """Path to the instance settings JSON file."""

        return self.data_dir / "settings.json"

    @property
    def prompts_dir(self) -> Path:
        """Path to user-copy prompt fragments in the data directory."""

        return self.data_dir / "prompts"

    @property
    def resource_prompts_dir(self) -> Path:
        """Path to bundled default prompt fragments."""

        return self.resources_dir / "prompts"

    def ensure_directories(self) -> None:
        """Create the Phase 2 data-directory structure if it is missing."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        for directory_name in PHASE_TWO_DIRECTORIES:
            (self.data_dir / directory_name).mkdir(parents=True, exist_ok=True)

    def load_environment(self) -> dict[str, str]:
        """Return a read-only snapshot of credentials from ``<data_dir>/.env``.

        The returned mapping is suitable for later merging with the live
        process environment, but this method never mutates ``os.environ``.
        """

        return self.load_data_dir_credentials()

    def load_data_dir_credentials(self) -> dict[str, str]:
        """Read ``<data_dir>/.env`` as a credential fallback snapshot."""

        return read_env_file(self.data_dir / ".env")

    def set_data_dir_credential(self, key: str, value: str) -> None:
        """Write or replace one credential in ``<data_dir>/.env``."""

        if not ENV_KEY_PATTERN.fullmatch(key):
            raise StorageError(f"Invalid environment key: {key}")
        if not value:
            raise StorageError("Credential value must not be empty")
        if "\n" in value or "\r" in value:
            raise StorageError("Credential value must be a single line")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        env_path = self.data_dir / ".env"
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        except OSError as exc:
            raise StorageError(f"Cannot read {env_path}: {exc}") from exc

        new_line = f"{key}={value}"
        updated_lines: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                candidate_key = stripped.partition("=")[0].strip()
                if candidate_key == key:
                    if not replaced:
                        updated_lines.append(new_line)
                        replaced = True
                    continue
            updated_lines.append(line)

        if not replaced:
            updated_lines.append(new_line)

        try:
            env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot write {env_path}: {exc}") from exc

    def build_environment_snapshot(self) -> dict[str, str]:
        """Return process-env-over-data-dir merged credentials without mutation."""

        return build_environment_snapshot(
            process_env=os.environ,
            fallback_env=self.load_data_dir_credentials(),
        )

    def load_settings(self) -> dict[str, Any]:
        """Load ``settings.json`` or return an empty mapping when it does not exist."""

        try:
            return load_validated_settings_json(self.settings_path)
        except SettingsValidationError as exc:
            raise StorageError(str(exc)) from exc

    def supported_appearance_languages(self) -> list[str]:
        """Return language codes supported by the persisted Settings surface."""

        return sorted(SUPPORTED_APPEARANCE_LANGUAGES)

    def load_appearance_settings(self) -> dict[str, str]:
        """Return normalized persisted Appearance settings."""

        settings = self.load_settings()
        return self._normalize_appearance_settings(settings.get("appearance"))

    def update_appearance_settings(self, appearance: Mapping[str, Any]) -> dict[str, str]:
        """Persist the supported Appearance Settings subset and return it."""

        if not isinstance(appearance, Mapping):
            raise StorageError("Appearance settings must be a mapping")

        unsupported_fields = sorted(set(appearance) - {"language"})
        if unsupported_fields:
            raise StorageError(f"Unsupported appearance settings: {', '.join(unsupported_fields)}")

        if "language" not in appearance:
            raise StorageError("Appearance settings must include language")

        settings = self.load_settings()
        merged_settings = dict(settings)
        merged_settings["appearance"] = self._normalize_appearance_settings(appearance)
        self.save_settings(merged_settings)
        return dict(merged_settings["appearance"])

    def load_skill_directory_settings(self) -> list[str]:
        """Return normalized extra skill directory settings."""

        settings = self.load_settings()
        return self._normalize_skill_directories(settings.get("skill_directories"))

    def update_skill_directory_settings(self, directories: Any) -> list[str]:
        """Persist the extra skill directory list and return it."""

        normalized_directories = self._normalize_skill_directories(directories)
        settings = self.load_settings()
        merged_settings = dict(settings)
        merged_settings["skill_directories"] = normalized_directories
        self.save_settings(merged_settings)
        return normalized_directories

    def load_subagent_settings(self) -> dict[str, int]:
        """Return normalized persisted Sub-Agent settings."""

        settings = self.load_settings()
        return {
            key: self._normalize_subagent_integer(key, settings.get(key), default)
            for key, default in SUBAGENT_SETTING_DEFAULTS.items()
        }

    def load_compaction_settings(self) -> dict[str, Any]:
        """Return normalized persisted compaction settings."""

        settings = self.load_settings()
        return self._normalize_compaction_settings(settings.get("compaction"))

    def load_defaults(self) -> dict[str, Any]:
        """Return normalized persisted defaults settings."""

        settings = self.load_settings()
        return self._normalize_defaults_settings(settings.get("defaults"))

    def load_recall_settings(self) -> dict[str, str]:
        """Return normalized persisted recall backend settings."""

        settings = self.load_settings()
        return self._normalize_recall_settings(settings.get("recall"))

    def load_web_search_settings(self) -> dict[str, Any]:
        """Return normalized persisted web search provider settings."""

        settings = self.load_settings()
        return self._normalize_web_search_settings(settings.get("web_search"))

    def load_model_task_settings(self) -> dict[str, dict[str, Any]]:
        """Return normalized persisted task-model bindings."""

        settings = self.load_settings()
        return self._normalize_model_task_settings(settings.get("model_tasks"))

    def update_recall_settings(self, recall: Mapping[str, Any]) -> dict[str, str]:
        """Persist the supported recall settings subset and return it."""

        if not isinstance(recall, Mapping):
            raise StorageError("Recall settings must be a mapping")

        unsupported_fields = sorted(set(recall) - {"backend"})
        if unsupported_fields:
            raise StorageError(f"Unsupported recall settings: {', '.join(unsupported_fields)}")

        normalized_recall = self._normalize_recall_settings(recall)
        settings = self.load_settings()
        merged_settings = dict(settings)
        merged_settings["recall"] = normalized_recall
        self.save_settings(merged_settings)
        return dict(normalized_recall)

    def update_web_search_settings(self, web_search: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the supported web search provider settings and return them."""

        if not isinstance(web_search, Mapping):
            raise StorageError("Web search settings must be a mapping")

        unsupported_fields = sorted(set(web_search) - {"provider", "searxng"})
        if unsupported_fields:
            raise StorageError(f"Unsupported web_search settings: {', '.join(unsupported_fields)}")

        settings = self.load_settings()
        merged_settings = dict(settings)
        current_settings = self._normalize_web_search_settings(settings.get("web_search"))
        raw_searxng_update = web_search.get("searxng", {})
        if raw_searxng_update is None:
            raw_searxng_update = {}
        if not isinstance(raw_searxng_update, Mapping):
            raise StorageError("Expected settings.web_search.searxng to be an object")

        normalized_web_search = self._normalize_web_search_settings(
            {
                **current_settings,
                **dict(web_search),
                "searxng": {
                    **current_settings["searxng"],
                    **dict(raw_searxng_update),
                },
            }
        )
        merged_settings["web_search"] = normalized_web_search
        self.save_settings(merged_settings)
        return dict(normalized_web_search)

    def update_model_task_settings(
        self,
        model_tasks: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Persist sparse task-model binding updates and return the full section."""

        if not isinstance(model_tasks, Mapping):
            raise StorageError("Model task settings must be a mapping")

        settings = self.load_settings()
        merged_settings = dict(settings)
        merged_model_tasks = self._normalize_model_task_settings(settings.get("model_tasks"))

        for task_type, raw_binding in model_tasks.items():
            if task_type not in SUPPORTED_TASK_TYPES:
                raise StorageError(f"Unsupported model task type: {task_type}")
            if not isinstance(raw_binding, Mapping):
                raise StorageError(f"Model task binding {task_type} must be a mapping")

            unsupported_fields = sorted(set(raw_binding) - {"target", "options"})
            if unsupported_fields:
                raise StorageError(
                    f"Unsupported model task settings for {task_type}: "
                    f"{', '.join(unsupported_fields)}"
                )

            current_binding = dict(merged_model_tasks.get(task_type, {}))
            target = current_binding.get("target", "")
            if "target" in raw_binding:
                raw_target = raw_binding["target"]
                if not isinstance(raw_target, str):
                    raise StorageError(f"Model task target for {task_type} must be a string")
                target = raw_target.strip()

            if not target:
                merged_model_tasks.pop(task_type, None)
                continue

            options = current_binding.get("options", {})
            if "options" in raw_binding:
                options = self._normalize_json_object(
                    raw_binding["options"],
                    f"settings.model_tasks.{task_type}.options",
                )

            merged_model_tasks[task_type] = {
                "target": target,
                "options": options,
            }

        if merged_model_tasks:
            merged_settings["model_tasks"] = merged_model_tasks
        else:
            merged_settings.pop("model_tasks", None)

        self.save_settings(merged_settings)
        return self._normalize_model_task_settings(merged_settings.get("model_tasks"))

    def update_defaults(self, section: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Persist normalized defaults for a single section and return persisted values."""

        if section not in SUPPORTED_DEFAULTS_SECTIONS:
            raise StorageError(f"Unsupported defaults section: {section}")
        if not isinstance(values, Mapping):
            raise StorageError("Defaults values must be a mapping")

        settings = self.load_settings()
        merged_settings = dict(settings)
        merged_defaults = self._coerce_defaults_section(merged_settings.get("defaults"))

        if section == "agent":
            current_agent_defaults = self._normalize_agent_defaults(merged_defaults.get("agent"))
            self._validate_supported_agent_default_fields(values)
            for field, value in values.items():
                normalized_value = self._normalize_agent_default_value(field, value)
                if normalized_value is None:
                    current_agent_defaults.pop(field, None)
                    continue
                current_agent_defaults[field] = normalized_value

            if current_agent_defaults:
                merged_defaults["agent"] = current_agent_defaults
            else:
                merged_defaults.pop("agent", None)

        if merged_defaults:
            merged_settings["defaults"] = merged_defaults
        else:
            merged_settings.pop("defaults", None)

        self.save_settings(merged_settings)
        return self._normalize_defaults_settings(merged_defaults)

    def update_compaction_settings(self, compaction: Mapping[str, Any]) -> dict[str, Any]:
        """Persist compaction settings and return normalized values."""

        if not isinstance(compaction, Mapping):
            raise StorageError("Compaction settings must be a mapping")

        settings = self.load_settings()
        merged_settings = dict(settings)
        normalized_compaction = self._normalize_compaction_settings(
            {
                **self._normalize_compaction_settings(settings.get("compaction")),
                **dict(compaction),
            }
        )
        merged_settings["compaction"] = normalized_compaction
        self.save_settings(merged_settings)
        return dict(normalized_compaction)

    def save_settings(self, settings: Mapping[str, Any]) -> None:
        """Atomically write ``settings.json`` as UTF-8 JSON."""

        if not isinstance(settings, Mapping):
            raise StorageError("Settings must be a mapping")

        self.ensure_directories()
        temp_path = self._temporary_path(self.settings_path)
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(dict(settings), file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temp_path, self.settings_path)
        except TypeError as exc:
            self._remove_temporary_file(temp_path)
            raise StorageError(
                f"Settings contain a value that cannot be serialized: {exc}"
            ) from exc
        except OSError as exc:
            self._remove_temporary_file(temp_path)
            raise StorageError(f"Cannot write {self.settings_path}: {exc}") from exc

    def copy_prompt_fragments(self, *, overwrite: bool = False) -> list[Path]:
        """Copy bundled prompt fragments into ``<data_dir>/prompts``.

        Existing user-copy fragments are preserved unless ``overwrite`` is true.
        Returns the data-directory prompt paths that were written.
        """

        self.ensure_directories()
        written_paths: list[Path] = []
        for fragment_name in sorted(PROMPT_FRAGMENT_NAMES):
            source_path = self.resource_prompts_dir / fragment_name
            target_path = self.prompts_dir / fragment_name
            if target_path.exists() and not overwrite:
                continue

            try:
                content = source_path.read_text(encoding="utf-8")
                target_path.write_text(content, encoding="utf-8")
            except OSError as exc:
                raise StorageError(f"Cannot copy prompt fragment {fragment_name}: {exc}") from exc
            written_paths.append(target_path)
        return written_paths

    def copy_agent_prompt_fragments(self, agent_id: str, *, overwrite: bool = False) -> list[Path]:
        """Seed an Agent prompt scope from the currently effective default fragments.

        Existing Agent copies are preserved unless ``overwrite`` is true. Only
        normal editable system-prompt fragments are copied; backend-only prompt
        fragments such as ``compaction.md`` are never Agent-scoped.
        """

        safe_agent_id = self._validate_agent_id(agent_id)
        self.ensure_directories()
        target_dir = self.agent_prompts_dir(safe_agent_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        written_paths: list[Path] = []
        for fragment_name in sorted(AGENT_PROMPT_FRAGMENT_NAMES):
            target_path = target_dir / fragment_name
            if target_path.exists() and not overwrite:
                continue

            content = self.read_prompt_fragment(fragment_name)
            temp_path = self._temporary_path(target_path)
            try:
                temp_path.write_text(content, encoding="utf-8")
                os.replace(temp_path, target_path)
            except OSError as exc:
                self._remove_temporary_file(temp_path)
                raise StorageError(
                    f"Cannot copy Agent prompt fragment {fragment_name}: {exc}"
                ) from exc
            written_paths.append(target_path)
        return written_paths

    def agent_prompts_dir(self, agent_id: str) -> Path:
        """Return the prompt-fragment directory for one Agent."""

        safe_agent_id = self._validate_agent_id(agent_id)
        return self.data_dir / "agents" / safe_agent_id / "prompts"

    def agent_prompt_fragment_exists(self, agent_id: str, fragment_name: str) -> bool:
        """Return whether an Agent prompt fragment exists on disk."""

        safe_name = self._validate_agent_prompt_fragment_name(fragment_name)
        return (self.agent_prompts_dir(agent_id) / safe_name).exists()

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        """Read an Agent prompt fragment, returning an empty string when absent."""

        safe_name = self._validate_agent_prompt_fragment_name(fragment_name)
        prompt_path = self.agent_prompts_dir(agent_id) / safe_name
        if not prompt_path.exists():
            return ""

        try:
            return prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot read Agent prompt fragment {safe_name}: {exc}") from exc

    def write_agent_prompt_fragment(self, agent_id: str, fragment_name: str, content: str) -> Path:
        """Write one Agent prompt fragment."""

        safe_name = self._validate_agent_prompt_fragment_name(fragment_name)
        target_dir = self.agent_prompts_dir(agent_id)
        target_path = target_dir / safe_name

        self.ensure_directories()
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self._temporary_path(target_path)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        except OSError as exc:
            self._remove_temporary_file(temp_path)
            raise StorageError(f"Cannot write Agent prompt fragment {safe_name}: {exc}") from exc

        return target_path

    def reset_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> Path:
        """Reset one Agent prompt fragment to the current default-scope content."""

        safe_name = self._validate_agent_prompt_fragment_name(fragment_name)
        return self.write_agent_prompt_fragment(
            agent_id, safe_name, self.read_prompt_fragment(safe_name)
        )

    def reset_prompt_fragment(self, fragment_name: str) -> Path:
        """Reset a user-copy prompt fragment to its bundled default.

        Validates the name, reads the bundled resource fragment, and atomically
        overwrites the user copy in ``<data_dir>/prompts/``.  Returns the
        written path.
        """

        safe_name = self._validate_prompt_fragment_name(fragment_name)
        source_path = self.resource_prompts_dir / safe_name
        target_path = self.prompts_dir / safe_name

        try:
            content = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot read bundled prompt fragment {safe_name}: {exc}") from exc

        self.ensure_directories()
        temp_path = self._temporary_path(target_path)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        except OSError as exc:
            self._remove_temporary_file(temp_path)
            raise StorageError(f"Cannot write prompt fragment {safe_name}: {exc}") from exc

        return target_path

    def write_prompt_fragment(self, fragment_name: str, content: str) -> Path:
        """Write arbitrary content to a user-copy prompt fragment.

        Validates the name against the allowlist and atomically writes the
        given string (UTF-8) to ``<data_dir>/prompts/<fragment_name>``.
        Returns the written path.
        """

        safe_name = self._validate_prompt_fragment_name(fragment_name)
        target_path = self.prompts_dir / safe_name

        self.ensure_directories()
        temp_path = self._temporary_path(target_path)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        except OSError as exc:
            self._remove_temporary_file(temp_path)
            raise StorageError(f"Cannot write prompt fragment {safe_name}: {exc}") from exc

        return target_path

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Read a prompt fragment from the data directory, falling back to resources."""

        safe_name = self._validate_prompt_fragment_name(fragment_name)
        data_path = self.prompts_dir / safe_name
        resource_path = self.resource_prompts_dir / safe_name
        prompt_path = data_path if data_path.exists() else resource_path

        try:
            return prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot read prompt fragment {safe_name}: {exc}") from exc

    @staticmethod
    def _resolve_data_dir(data_dir: str | Path | None, config: ConfigProtocol | None) -> Path:
        if data_dir is not None:
            return Path(data_dir)

        if config is not None and hasattr(config, "data_dir"):
            return Path(config.data_dir)

        if config is not None:
            configured = config.get("DATA_DIR") or config.get("VBOT_DATA_DIR")
            if configured:
                return Path(configured)

        return DEFAULT_DATA_DIR

    @staticmethod
    def _resolve_resources_dir(resources_dir: str | Path | None) -> Path:
        if resources_dir is not None:
            return Path(resources_dir)
        return Path(__file__).resolve().parents[2] / "resources"

    @classmethod
    def _normalize_appearance_settings(cls, appearance: Any) -> dict[str, str]:
        return {"language": cls._normalize_appearance_language(appearance)}

    @classmethod
    def _normalize_appearance_language(cls, appearance: Any) -> str:
        section = cls._coerce_appearance_section(appearance)
        value = section.get("language")
        if value is None:
            return DEFAULT_APPEARANCE_LANGUAGE
        return cls._validate_appearance_language(value)

    @staticmethod
    def _coerce_appearance_section(appearance: Any) -> dict[str, Any]:
        if appearance is None:
            return {}
        if not isinstance(appearance, Mapping):
            raise StorageError("Expected settings.appearance to be an object")
        return dict(appearance)

    @staticmethod
    def _validate_appearance_language(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise StorageError("Appearance language must be a non-empty string")
        if value not in SUPPORTED_APPEARANCE_LANGUAGES:
            supported = ", ".join(sorted(SUPPORTED_APPEARANCE_LANGUAGES))
            raise StorageError(f"Unsupported appearance language: {value}. Supported: {supported}")
        return value

    @staticmethod
    def _normalize_skill_directories(directories: Any) -> list[str]:
        if directories is None:
            return []
        if not isinstance(directories, list):
            raise StorageError("settings.skill_directories must be a list")

        normalized_directories: list[str] = []
        for directory in directories:
            if not isinstance(directory, str) or not directory.strip():
                raise StorageError("Skill directories must be non-empty strings")
            normalized_directory = directory.strip()
            if not _is_absolute_or_home_relative_path(normalized_directory):
                raise StorageError(
                    "Skill directories must be absolute paths or home-relative paths "
                    "starting with ~"
                )
            normalized_directories.append(normalized_directory)
        return normalized_directories

    @staticmethod
    def _normalize_subagent_integer(key: str, value: Any, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise StorageError(f"Sub-agent setting {key} must be an integer")
        if value <= 0:
            raise StorageError(f"Sub-agent setting {key} must be positive")
        return cast("int", value)

    @classmethod
    def _normalize_compaction_settings(cls, compaction: Any) -> dict[str, Any]:
        section = cls._coerce_compaction_section(compaction)
        return {
            "auto": cls._normalize_compaction_auto(section.get("auto")),
            "threshold": cls._normalize_compaction_threshold(section.get("threshold")),
            "tail_tokens": cls._normalize_compaction_tail_tokens(section.get("tail_tokens")),
            "summary_model": cls._normalize_compaction_summary_model(section.get("summary_model")),
        }

    @staticmethod
    def _coerce_compaction_section(compaction: Any) -> dict[str, Any]:
        if compaction is None:
            return {}
        if not isinstance(compaction, Mapping):
            raise StorageError("Expected settings.compaction to be an object")
        return dict(compaction)

    @staticmethod
    def _normalize_compaction_auto(value: Any) -> bool:
        if value is None:
            return cast("bool", COMPACTION_SETTING_DEFAULTS["auto"])
        if not isinstance(value, bool):
            raise StorageError("Compaction setting auto must be a boolean")
        return value

    @staticmethod
    def _normalize_compaction_threshold(value: Any) -> float:
        if value is None:
            return cast("float", COMPACTION_SETTING_DEFAULTS["threshold"])
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StorageError("Compaction setting threshold must be a number")

        normalized_value = float(value)
        if normalized_value <= 0 or normalized_value > 1:
            raise StorageError("Compaction setting threshold must be in (0, 1]")
        return normalized_value

    @staticmethod
    def _normalize_compaction_tail_tokens(value: Any) -> int:
        if value is None:
            return cast("int", COMPACTION_SETTING_DEFAULTS["tail_tokens"])
        if isinstance(value, bool) or not isinstance(value, int):
            raise StorageError("Compaction setting tail_tokens must be an integer")
        if value <= 0:
            raise StorageError("Compaction setting tail_tokens must be positive")
        return value

    @staticmethod
    def _normalize_compaction_summary_model(value: Any) -> str | None:
        if value is None:
            return cast("str | None", COMPACTION_SETTING_DEFAULTS["summary_model"])
        if not isinstance(value, str):
            raise StorageError("Compaction setting summary_model must be a string or null")
        return value

    @classmethod
    def _normalize_defaults_settings(cls, defaults: Any) -> dict[str, Any]:
        section = cls._coerce_defaults_section(defaults)
        normalized_agent_defaults = cls._normalize_agent_defaults(section.get("agent"))
        if not normalized_agent_defaults:
            return {}
        return {"agent": normalized_agent_defaults}

    @classmethod
    def _normalize_recall_settings(cls, recall: Any) -> dict[str, str]:
        section = cls._coerce_recall_section(recall)
        backend = section.get("backend", DEFAULT_RECALL_SETTINGS["backend"])
        if not isinstance(backend, str) or not backend.strip():
            raise StorageError("Recall backend must be a non-empty string")
        return {"backend": backend.strip()}

    @classmethod
    def _normalize_web_search_settings(cls, web_search: Any) -> dict[str, Any]:
        section = cls._coerce_web_search_section(web_search)
        provider = section.get("provider", DEFAULT_WEB_SEARCH_SETTINGS["provider"])
        if not isinstance(provider, str) or provider not in FIRST_PARTY_WEB_SEARCH_PROVIDERS:
            allowed = ", ".join(sorted(FIRST_PARTY_WEB_SEARCH_PROVIDERS))
            raise StorageError(f"Web search provider must be one of: {allowed}")

        searxng = section.get("searxng", {})
        if searxng is None:
            searxng = {}
        if not isinstance(searxng, Mapping):
            raise StorageError("Expected settings.web_search.searxng to be an object")

        unsupported_searxng_fields = sorted(set(searxng) - {"base_url"})
        if unsupported_searxng_fields:
            raise StorageError(
                "Unsupported SearXNG settings: " + ", ".join(unsupported_searxng_fields)
            )

        base_url = searxng.get("base_url", DEFAULT_SEARXNG_BASE_URL)
        if not isinstance(base_url, str) or not base_url.strip():
            raise StorageError("SearXNG base_url must be a non-empty string")

        return {
            "provider": provider,
            "searxng": {"base_url": base_url.strip()},
        }

    @classmethod
    def _normalize_model_task_settings(cls, model_tasks: Any) -> dict[str, dict[str, Any]]:
        section = cls._coerce_model_tasks_section(model_tasks)
        normalized: dict[str, dict[str, Any]] = {}

        for task_type, raw_binding in section.items():
            if task_type not in SUPPORTED_TASK_TYPES:
                raise StorageError(f"Unsupported model task type: {task_type}")
            if not isinstance(raw_binding, Mapping):
                raise StorageError(f"Expected settings.model_tasks.{task_type} to be an object")

            unsupported_fields = sorted(set(raw_binding) - {"target", "options"})
            if unsupported_fields:
                raise StorageError(
                    f"Unsupported model task settings for {task_type}: "
                    f"{', '.join(unsupported_fields)}"
                )

            target = raw_binding.get("target")
            if not isinstance(target, str) or not target.strip():
                raise StorageError(f"Model task target for {task_type} must be a non-empty string")

            normalized[task_type] = {
                "target": target.strip(),
                "options": cls._normalize_json_object(
                    raw_binding.get("options", {}),
                    f"settings.model_tasks.{task_type}.options",
                ),
            }
        return normalized

    @staticmethod
    def _coerce_model_tasks_section(model_tasks: Any) -> dict[str, Any]:
        if model_tasks is None:
            return {}
        if not isinstance(model_tasks, Mapping):
            raise StorageError("Expected settings.model_tasks to be an object")
        return dict(model_tasks)

    @staticmethod
    def _coerce_web_search_section(web_search: Any) -> dict[str, Any]:
        if web_search is None:
            return {}
        if not isinstance(web_search, Mapping):
            raise StorageError("Expected settings.web_search to be an object")
        unsupported_fields = sorted(set(web_search) - {"provider", "searxng"})
        if unsupported_fields:
            raise StorageError(f"Unsupported web_search settings: {', '.join(unsupported_fields)}")
        return dict(web_search)

    @classmethod
    def _normalize_json_object(cls, value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise StorageError(f"Expected {path} to be an object")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise StorageError(f"Expected {path} keys to be non-empty strings")
            normalized[key] = cls._normalize_json_value(item, f"{path}.{key}")
        return normalized

    @classmethod
    def _normalize_json_value(cls, value: Any, path: str) -> Any:
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, list):
            return [
                cls._normalize_json_value(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        if isinstance(value, Mapping):
            return cls._normalize_json_object(value, path)
        raise StorageError(f"Unsupported JSON value at {path}")

    @staticmethod
    def _coerce_recall_section(recall: Any) -> dict[str, Any]:
        if recall is None:
            return {}
        if not isinstance(recall, Mapping):
            raise StorageError("Expected settings.recall to be an object")
        return dict(recall)

    @staticmethod
    def _coerce_defaults_section(defaults: Any) -> dict[str, Any]:
        if defaults is None:
            return {}
        if not isinstance(defaults, Mapping):
            raise StorageError("Expected settings.defaults to be an object")
        return dict(defaults)

    @classmethod
    def _normalize_agent_defaults(cls, defaults: Any) -> dict[str, Any]:
        section = cls._coerce_agent_defaults_section(defaults)
        cls._validate_supported_agent_default_fields(section)

        normalized_agent_defaults: dict[str, Any] = {}
        for field, value in section.items():
            normalized_value = cls._normalize_agent_default_value(field, value)
            if normalized_value is None:
                continue
            normalized_agent_defaults[field] = normalized_value
        return normalized_agent_defaults

    @staticmethod
    def _coerce_agent_defaults_section(defaults: Any) -> dict[str, Any]:
        if defaults is None:
            return {}
        if not isinstance(defaults, Mapping):
            raise StorageError("Expected settings.defaults.agent to be an object")
        return dict(defaults)

    @staticmethod
    def _validate_supported_agent_default_fields(values: Mapping[str, Any]) -> None:
        unsupported_fields = sorted(set(values) - AGENT_DEFAULT_FIELDS)
        if unsupported_fields:
            raise StorageError(
                f"Unsupported defaults.agent settings: {', '.join(unsupported_fields)}"
            )

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
            if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
                raise StorageError(
                    "Agent default temperature must be between "
                    f"{MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}"
                )
            return temperature

        if field == "thinking_effort":
            if not isinstance(value, str):
                raise StorageError("Agent default thinking_effort must be a string or null")
            if value not in ALLOWED_THINKING_EFFORTS:
                allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
                raise StorageError(f"Agent default thinking_effort must be one of: {allowed}")
            return value

        raise StorageError(f"Unsupported defaults.agent setting: {field}")

    @staticmethod
    def _validate_prompt_fragment_name(fragment_name: str) -> str:
        path = Path(fragment_name)
        if path.name != fragment_name or path.is_absolute():
            raise StorageError(f"Unsafe prompt fragment name: {fragment_name}")
        if fragment_name not in PROMPT_FRAGMENT_NAMES:
            raise StorageError(f"Unknown prompt fragment: {fragment_name}")
        return fragment_name

    @staticmethod
    def _validate_agent_id(agent_id: str) -> str:
        if not isinstance(agent_id, str) or AGENT_ID_PATTERN.fullmatch(agent_id) is None:
            raise StorageError(f"Unsafe agent id: {agent_id}")
        return agent_id

    @staticmethod
    def _validate_agent_prompt_fragment_name(fragment_name: str) -> str:
        path = Path(fragment_name)
        if path.name != fragment_name or path.is_absolute():
            raise StorageError(f"Unsafe Agent prompt fragment name: {fragment_name}")
        if fragment_name not in AGENT_PROMPT_FRAGMENT_NAMES:
            raise StorageError(f"Unknown Agent prompt fragment: {fragment_name}")
        return fragment_name

    def _temporary_path(self, target_path: Path) -> Path:
        temp_dir = self.data_dir / ".tmp"
        return temp_dir / f".{target_path.name}.{uuid4().hex}.tmp"

    @staticmethod
    def _remove_temporary_file(temp_path: Path) -> None:
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)


def _is_absolute_or_home_relative_path(path: str) -> bool:
    if path == "~" or path.startswith(("~/", "~\\")):
        return True
    return Path(path).is_absolute()
