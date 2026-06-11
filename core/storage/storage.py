"""Storage manager for vBot data directories, settings, and prompt fragments.

``StorageManager`` owns the data-directory lifecycle, ``.env`` credential snapshots,
and serialized read-modify-write transactions over ``settings.json``. Section
normalization is delegated to :mod:`core.settings.normalizers` (the settings domain
owns the section schemas) and prompt fragments to
:class:`core.storage.prompt_fragments.PromptFragmentStore`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, TypeVar

from core.model_tasks import SUPPORTED_TASK_TYPES
from core.settings import SettingsValidationError, load_validated_settings_json
from core.settings.normalizers import (
    SUPPORTED_APPEARANCE_LANGUAGES,
    coerce_defaults_section,
    coerce_defaults_update,
    coerce_skills_update,
    normalize_agent_default_value,
    normalize_agent_defaults,
    normalize_appearance_settings,
    normalize_compaction_settings,
    normalize_debug_settings,
    normalize_defaults_settings,
    normalize_json_object,
    normalize_model_task_settings,
    normalize_recall_settings,
    normalize_skill_directories,
    normalize_subagent_integer,
    normalize_web_search_settings,
    validate_supported_agent_default_fields,
)
from core.storage.atomic import remove_temporary_file, temporary_path
from core.storage.errors import StorageError
from core.storage.prompt_fragments import PromptFragmentStore
from core.utils.config import build_environment_snapshot, read_env_file

SettingsUpdateResult = TypeVar("SettingsUpdateResult")

DEFAULT_DATA_DIR = Path.home() / ".vbot"
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUBAGENT_SETTING_DEFAULTS = {
    "max_subagent_depth": 4,
    "max_subagents_per_turn": 8,
    "subagent_timeout_minutes": 60,
}
SETTINGS_UPDATE_SECTIONS = frozenset(
    {
        "appearance",
        "skills",
        "subagents",
        "compaction",
        "defaults",
        "recall",
        "model_tasks",
        "web_search",
        "debug",
    }
)
SUPPORTED_DEFAULTS_SECTIONS = frozenset({"agent"})
PHASE_TWO_DIRECTORIES = (
    ".tmp",
    "agents",
    "archive",
    "attachments",
    "channels",
    "cron",
    "debug",
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
        self._settings_lock = RLock()
        self._prompt_fragments = PromptFragmentStore(
            data_dir=self.data_dir,
            resources_dir=self.resources_dir,
            ensure_directories=self.ensure_directories,
        )

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

        self.ensure_directories()
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

        temp_path = temporary_path(self.data_dir, env_path)
        try:
            temp_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
            os.replace(temp_path, env_path)
        except OSError as exc:
            remove_temporary_file(temp_path)
            raise StorageError(f"Cannot write {env_path}: {exc}") from exc

    def build_environment_snapshot(self) -> dict[str, str]:
        """Return process-env-over-data-dir merged credentials without mutation."""

        return build_environment_snapshot(
            process_env=os.environ,
            fallback_env=self.load_data_dir_credentials(),
        )

    def load_settings(self) -> dict[str, Any]:
        """Load ``settings.json`` or return an empty mapping when it does not exist."""

        with self._settings_lock:
            try:
                return load_validated_settings_json(self.settings_path)
            except SettingsValidationError as exc:
                raise StorageError(str(exc)) from exc

    def update_settings(
        self,
        mutator: Callable[[dict[str, Any]], SettingsUpdateResult],
    ) -> SettingsUpdateResult:
        """Apply one read-modify-write transaction to ``settings.json``."""

        if not callable(mutator):
            raise StorageError("Settings mutator must be callable")

        with self._settings_lock:
            merged_settings = dict(self.load_settings())
            result = mutator(merged_settings)
            self.save_settings(merged_settings)
            return result

    def update_settings_sections(self, settings_update: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a parsed public Settings update in one settings transaction."""

        if not isinstance(settings_update, Mapping):
            raise StorageError("Settings update must be a mapping")

        unsupported_sections = sorted(set(settings_update) - SETTINGS_UPDATE_SECTIONS)
        if unsupported_sections:
            raise StorageError(f"Unsupported settings sections: {', '.join(unsupported_sections)}")

        updated_sections: dict[str, Any] = {}

        def apply_update(settings: dict[str, Any]) -> dict[str, Any]:
            if "appearance" in settings_update:
                updated_sections["appearance"] = self._apply_appearance_settings(
                    settings,
                    settings_update["appearance"],
                )
            if "skills" in settings_update:
                skills_update = coerce_skills_update(settings_update["skills"])
                updated_sections["skills"] = {
                    "directories": self._apply_skill_directory_settings(
                        settings,
                        skills_update["directories"],
                    )
                }
            if "subagents" in settings_update:
                updated_sections["subagents"] = self._apply_subagent_settings(
                    settings,
                    settings_update["subagents"],
                )
            if "compaction" in settings_update:
                updated_sections["compaction"] = self._apply_compaction_settings(
                    settings,
                    settings_update["compaction"],
                )
            if "defaults" in settings_update:
                defaults_update = coerce_defaults_update(settings_update["defaults"])
                updated_sections["defaults"] = self._apply_defaults(
                    settings,
                    "agent",
                    defaults_update["agent"],
                )
            if "recall" in settings_update:
                updated_sections["recall"] = self._apply_recall_settings(
                    settings,
                    settings_update["recall"],
                )
            if "web_search" in settings_update:
                updated_sections["web_search"] = self._apply_web_search_settings(
                    settings,
                    settings_update["web_search"],
                )
            if "model_tasks" in settings_update:
                updated_sections["model_tasks"] = self._apply_model_task_settings(
                    settings,
                    settings_update["model_tasks"],
                )
            if "debug" in settings_update:
                updated_sections["debug"] = self._apply_debug_settings(
                    settings,
                    settings_update["debug"],
                )
            return dict(updated_sections)

        return self.update_settings(apply_update)

    def supported_appearance_languages(self) -> list[str]:
        """Return language codes supported by the persisted Settings surface."""

        return sorted(SUPPORTED_APPEARANCE_LANGUAGES)

    def load_appearance_settings(self) -> dict[str, str]:
        """Return normalized persisted Appearance settings."""

        settings = self.load_settings()
        return normalize_appearance_settings(settings.get("appearance"))

    def update_appearance_settings(self, appearance: Mapping[str, Any]) -> dict[str, str]:
        """Persist the supported Appearance Settings subset and return it."""

        return self.update_settings(
            lambda settings: self._apply_appearance_settings(settings, appearance)
        )

    def _apply_appearance_settings(
        self,
        settings: dict[str, Any],
        appearance: Mapping[str, Any],
    ) -> dict[str, str]:
        """Merge Appearance settings into an in-memory settings mapping."""

        if not isinstance(appearance, Mapping):
            raise StorageError("Appearance settings must be a mapping")

        unsupported_fields = sorted(set(appearance) - {"language"})
        if unsupported_fields:
            raise StorageError(f"Unsupported appearance settings: {', '.join(unsupported_fields)}")

        if "language" not in appearance:
            raise StorageError("Appearance settings must include language")

        settings["appearance"] = normalize_appearance_settings(appearance)
        return dict(settings["appearance"])

    def load_skill_directory_settings(self) -> list[str]:
        """Return normalized extra skill directory settings."""

        settings = self.load_settings()
        return normalize_skill_directories(settings.get("skill_directories"))

    def update_skill_directory_settings(self, directories: Any) -> list[str]:
        """Persist the extra skill directory list and return it."""

        return self.update_settings(
            lambda settings: self._apply_skill_directory_settings(settings, directories)
        )

    def _apply_skill_directory_settings(
        self,
        settings: dict[str, Any],
        directories: Any,
    ) -> list[str]:
        """Merge extra skill directories into an in-memory settings mapping."""

        normalized_directories = normalize_skill_directories(directories)
        settings["skill_directories"] = normalized_directories
        return normalized_directories

    def load_subagent_settings(self) -> dict[str, int]:
        """Return normalized persisted Sub-Agent settings."""

        settings = self.load_settings()
        return {
            key: normalize_subagent_integer(key, settings.get(key), default)
            for key, default in SUBAGENT_SETTING_DEFAULTS.items()
        }

    def _apply_subagent_settings(
        self,
        settings: dict[str, Any],
        subagents: Mapping[str, Any],
    ) -> dict[str, int]:
        """Merge Sub-Agent settings into an in-memory settings mapping."""

        if not isinstance(subagents, Mapping):
            raise StorageError("Sub-agent settings must be a mapping")

        expected_fields = set(SUBAGENT_SETTING_DEFAULTS)
        unsupported_fields = sorted(set(subagents) - expected_fields)
        if unsupported_fields:
            raise StorageError(f"Unsupported sub-agent settings: {', '.join(unsupported_fields)}")

        missing_fields = sorted(expected_fields - set(subagents))
        if missing_fields:
            raise StorageError(f"Missing sub-agent settings: {', '.join(missing_fields)}")

        normalized_subagents = {
            key: normalize_subagent_integer(key, subagents[key], default)
            for key, default in SUBAGENT_SETTING_DEFAULTS.items()
        }
        settings.update(normalized_subagents)
        return normalized_subagents

    def load_compaction_settings(self) -> dict[str, Any]:
        """Return normalized persisted compaction settings."""

        settings = self.load_settings()
        return normalize_compaction_settings(settings.get("compaction"))

    def load_defaults(self) -> dict[str, Any]:
        """Return normalized persisted defaults settings."""

        settings = self.load_settings()
        return normalize_defaults_settings(settings.get("defaults"))

    def load_recall_settings(self) -> dict[str, str]:
        """Return normalized persisted recall backend settings."""

        settings = self.load_settings()
        return normalize_recall_settings(settings.get("recall"))

    def load_debug_settings(self) -> dict[str, Any]:
        """Return normalized persisted debug settings."""

        settings = self.load_settings()
        return normalize_debug_settings(settings.get("debug"))

    def load_web_search_settings(self) -> dict[str, Any]:
        """Return normalized persisted web search provider settings."""

        settings = self.load_settings()
        return normalize_web_search_settings(settings.get("web_search"))

    def load_model_task_settings(self) -> dict[str, dict[str, Any]]:
        """Return normalized persisted task-model bindings."""

        settings = self.load_settings()
        return normalize_model_task_settings(settings.get("model_tasks"))

    def update_recall_settings(self, recall: Mapping[str, Any]) -> dict[str, str]:
        """Persist the supported recall settings subset and return it."""

        return self.update_settings(lambda settings: self._apply_recall_settings(settings, recall))

    def _apply_recall_settings(
        self,
        settings: dict[str, Any],
        recall: Mapping[str, Any],
    ) -> dict[str, str]:
        """Merge recall settings into an in-memory settings mapping."""

        if not isinstance(recall, Mapping):
            raise StorageError("Recall settings must be a mapping")

        unsupported_fields = sorted(set(recall) - {"backend"})
        if unsupported_fields:
            raise StorageError(f"Unsupported recall settings: {', '.join(unsupported_fields)}")

        normalized_recall = normalize_recall_settings(recall)
        settings["recall"] = normalized_recall
        return dict(normalized_recall)

    def update_debug_settings(self, debug: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the supported debug settings subset and return it."""

        return self.update_settings(lambda settings: self._apply_debug_settings(settings, debug))

    def _apply_debug_settings(
        self,
        settings: dict[str, Any],
        debug: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge debug settings into an in-memory settings mapping."""

        if not isinstance(debug, Mapping):
            raise StorageError("Debug settings must be a mapping")

        unsupported_fields = sorted(set(debug) - {"enabled", "trace_limit"})
        if unsupported_fields:
            raise StorageError(f"Unsupported debug settings: {', '.join(unsupported_fields)}")

        normalized_debug = normalize_debug_settings(
            {
                **normalize_debug_settings(settings.get("debug")),
                **dict(debug),
            }
        )
        settings["debug"] = normalized_debug
        return dict(normalized_debug)

    def update_web_search_settings(self, web_search: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the supported web search provider settings and return them."""

        return self.update_settings(
            lambda settings: self._apply_web_search_settings(settings, web_search)
        )

    def _apply_web_search_settings(
        self,
        settings: dict[str, Any],
        web_search: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge web search settings into an in-memory settings mapping."""

        if not isinstance(web_search, Mapping):
            raise StorageError("Web search settings must be a mapping")

        unsupported_fields = sorted(set(web_search) - {"provider", "searxng"})
        if unsupported_fields:
            raise StorageError(f"Unsupported web_search settings: {', '.join(unsupported_fields)}")

        current_settings = normalize_web_search_settings(settings.get("web_search"))
        raw_searxng_update = web_search.get("searxng", {})
        if raw_searxng_update is None:
            raw_searxng_update = {}
        if not isinstance(raw_searxng_update, Mapping):
            raise StorageError("Expected settings.web_search.searxng to be an object")

        normalized_web_search = normalize_web_search_settings(
            {
                **current_settings,
                **dict(web_search),
                "searxng": {
                    **current_settings["searxng"],
                    **dict(raw_searxng_update),
                },
            }
        )
        settings["web_search"] = normalized_web_search
        return dict(normalized_web_search)

    def update_model_task_settings(
        self,
        model_tasks: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Persist sparse task-model binding updates and return the full section."""

        if not isinstance(model_tasks, Mapping):
            raise StorageError("Model task settings must be a mapping")

        return self.update_settings(
            lambda settings: self._apply_model_task_settings(settings, model_tasks)
        )

    def _apply_model_task_settings(
        self,
        settings: dict[str, Any],
        model_tasks: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Merge task-model bindings into an in-memory settings mapping."""

        merged_model_tasks = normalize_model_task_settings(settings.get("model_tasks"))

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
                options = normalize_json_object(
                    raw_binding["options"],
                    f"settings.model_tasks.{task_type}.options",
                )

            merged_model_tasks[task_type] = {
                "target": target,
                "options": options,
            }

        if merged_model_tasks:
            settings["model_tasks"] = merged_model_tasks
        else:
            settings.pop("model_tasks", None)

        return normalize_model_task_settings(settings.get("model_tasks"))

    def update_defaults(self, section: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Persist normalized defaults for a single section and return persisted values."""

        if section not in SUPPORTED_DEFAULTS_SECTIONS:
            raise StorageError(f"Unsupported defaults section: {section}")
        if not isinstance(values, Mapping):
            raise StorageError("Defaults values must be a mapping")

        return self.update_settings(
            lambda settings: self._apply_defaults(settings, section, values)
        )

    def _apply_defaults(
        self,
        settings: dict[str, Any],
        section: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge defaults into an in-memory settings mapping."""

        merged_defaults = coerce_defaults_section(settings.get("defaults"))

        if section == "agent":
            current_agent_defaults = normalize_agent_defaults(merged_defaults.get("agent"))
            validate_supported_agent_default_fields(values)
            for field, value in values.items():
                normalized_value = normalize_agent_default_value(field, value)
                if normalized_value is None:
                    current_agent_defaults.pop(field, None)
                    continue
                current_agent_defaults[field] = normalized_value

            if current_agent_defaults:
                merged_defaults["agent"] = current_agent_defaults
            else:
                merged_defaults.pop("agent", None)

        if merged_defaults:
            settings["defaults"] = merged_defaults
        else:
            settings.pop("defaults", None)

        return normalize_defaults_settings(merged_defaults)

    def update_compaction_settings(self, compaction: Mapping[str, Any]) -> dict[str, Any]:
        """Persist compaction settings and return normalized values."""

        return self.update_settings(
            lambda settings: self._apply_compaction_settings(settings, compaction)
        )

    def _apply_compaction_settings(
        self,
        settings: dict[str, Any],
        compaction: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge compaction settings into an in-memory settings mapping."""

        if not isinstance(compaction, Mapping):
            raise StorageError("Compaction settings must be a mapping")

        normalized_compaction = normalize_compaction_settings(
            {
                **normalize_compaction_settings(settings.get("compaction")),
                **dict(compaction),
            }
        )
        settings["compaction"] = normalized_compaction
        return dict(normalized_compaction)

    def save_settings(self, settings: Mapping[str, Any]) -> None:
        """Atomically write ``settings.json`` as UTF-8 JSON."""

        if not isinstance(settings, Mapping):
            raise StorageError("Settings must be a mapping")

        with self._settings_lock:
            self.ensure_directories()
            temp_path = temporary_path(self.data_dir, self.settings_path)
            try:
                with temp_path.open("w", encoding="utf-8") as file:
                    json.dump(dict(settings), file, ensure_ascii=False, indent=2, sort_keys=True)
                    file.write("\n")
                os.replace(temp_path, self.settings_path)
            except TypeError as exc:
                remove_temporary_file(temp_path)
                raise StorageError(
                    f"Settings contain a value that cannot be serialized: {exc}"
                ) from exc
            except OSError as exc:
                remove_temporary_file(temp_path)
                raise StorageError(f"Cannot write {self.settings_path}: {exc}") from exc

    def copy_prompt_fragments(self, *, overwrite: bool = False) -> list[Path]:
        """Copy bundled prompt fragments into ``<data_dir>/prompts``."""

        return self._prompt_fragments.copy_prompt_fragments(overwrite=overwrite)

    def copy_agent_prompt_fragments(self, agent_id: str, *, overwrite: bool = False) -> list[Path]:
        """Seed an Agent prompt scope from the currently effective default fragments."""

        return self._prompt_fragments.copy_agent_prompt_fragments(agent_id, overwrite=overwrite)

    def agent_prompts_dir(self, agent_id: str) -> Path:
        """Return the prompt-fragment directory for one Agent."""

        return self._prompt_fragments.agent_prompts_dir(agent_id)

    def agent_prompt_fragment_exists(self, agent_id: str, fragment_name: str) -> bool:
        """Return whether an Agent prompt fragment exists on disk."""

        return self._prompt_fragments.agent_prompt_fragment_exists(agent_id, fragment_name)

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        """Read an Agent prompt fragment, returning an empty string when absent."""

        return self._prompt_fragments.read_agent_prompt_fragment(agent_id, fragment_name)

    def write_agent_prompt_fragment(self, agent_id: str, fragment_name: str, content: str) -> Path:
        """Write one Agent prompt fragment."""

        return self._prompt_fragments.write_agent_prompt_fragment(agent_id, fragment_name, content)

    def reset_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> Path:
        """Reset one Agent prompt fragment to the current default-scope content."""

        return self._prompt_fragments.reset_agent_prompt_fragment(agent_id, fragment_name)

    def reset_prompt_fragment(self, fragment_name: str) -> Path:
        """Reset a user-copy prompt fragment to its bundled default."""

        return self._prompt_fragments.reset_prompt_fragment(fragment_name)

    def write_prompt_fragment(self, fragment_name: str, content: str) -> Path:
        """Write arbitrary content to a user-copy prompt fragment."""

        return self._prompt_fragments.write_prompt_fragment(fragment_name, content)

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Read a prompt fragment from the data directory, falling back to resources."""

        return self._prompt_fragments.read_prompt_fragment(fragment_name)

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
