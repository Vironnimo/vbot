"""Storage manager for vBot data directories and prompt fragments."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from core.utils.config import load_env_file_into_environment
from core.utils.errors import VBotError

DEFAULT_DATA_DIR = Path.home() / ".vbot"
PROMPT_FRAGMENT_NAMES = frozenset({"system.md", "runtime.md", "tools.md", "skills.md"})
PHASE_TWO_DIRECTORIES = (
    ".tmp",
    "agents",
    "archive",
    "channels",
    "cron",
    "oauth",
    "prompts",
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

    def load_environment(self) -> None:
        """Load ``<data_dir>/.env`` into the process environment.

        Existing process environment variables stay authoritative so users can
        override data-directory secrets from their shell or service manager.
        """

        load_env_file_into_environment(self.data_dir / ".env")

    def load_settings(self) -> dict[str, Any]:
        """Load ``settings.json`` or return an empty mapping when it does not exist."""

        if not self.settings_path.exists():
            return {}

        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"Invalid JSON in {self.settings_path}: {exc}") from exc
        except OSError as exc:
            raise StorageError(f"Cannot read {self.settings_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise StorageError(
                f"Expected a JSON object at {self.settings_path}, got {type(data).__name__}"
            )

        return data

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

    @staticmethod
    def _validate_prompt_fragment_name(fragment_name: str) -> str:
        path = Path(fragment_name)
        if path.name != fragment_name or path.is_absolute():
            raise StorageError(f"Unsafe prompt fragment name: {fragment_name}")
        if fragment_name not in PROMPT_FRAGMENT_NAMES:
            raise StorageError(f"Unknown prompt fragment: {fragment_name}")
        return fragment_name

    def _temporary_path(self, target_path: Path) -> Path:
        temp_dir = self.data_dir / ".tmp"
        return temp_dir / f".{target_path.name}.{uuid4().hex}.tmp"

    @staticmethod
    def _remove_temporary_file(temp_path: Path) -> None:
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)
