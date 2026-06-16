"""Prompt-fragment storage: bundled defaults, user copies, and Agent scopes.

A :class:`PromptFragmentStore` resolves and atomically writes the editable system
prompt fragments under ``<data_dir>/prompts`` and per-Agent scopes under
``<data_dir>/agents/<agent_id>/prompts``, falling back to the bundled resource
fragments. ``StorageManager`` owns one instance and delegates its prompt methods here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from core.settings import is_valid_agent_id
from core.storage.atomic import remove_temporary_file, temporary_path
from core.storage.errors import StorageError

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


class PromptFragmentStore:
    """Owns prompt-fragment resolution and atomic writes for the data directory."""

    def __init__(
        self,
        *,
        data_dir: Path,
        resources_dir: Path,
        ensure_directories: Callable[[], None],
    ) -> None:
        self._data_dir = data_dir
        self._resources_dir = resources_dir
        self._ensure_directories = ensure_directories

    @property
    def prompts_dir(self) -> Path:
        """Path to user-copy prompt fragments in the data directory."""

        return self._data_dir / "prompts"

    @property
    def resource_prompts_dir(self) -> Path:
        """Path to bundled default prompt fragments."""

        return self._resources_dir / "prompts"

    def copy_prompt_fragments(self, *, overwrite: bool = False) -> list[Path]:
        """Copy bundled prompt fragments into ``<data_dir>/prompts``.

        Existing user-copy fragments are preserved unless ``overwrite`` is true.
        Returns the data-directory prompt paths that were written.
        """

        self._ensure_directories()
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
        self._ensure_directories()
        target_dir = self.agent_prompts_dir(safe_agent_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        written_paths: list[Path] = []
        for fragment_name in sorted(AGENT_PROMPT_FRAGMENT_NAMES):
            target_path = target_dir / fragment_name
            if target_path.exists() and not overwrite:
                continue

            content = self.read_prompt_fragment(fragment_name)
            temp_path = temporary_path(self._data_dir, target_path)
            try:
                temp_path.write_text(content, encoding="utf-8")
                os.replace(temp_path, target_path)
            except OSError as exc:
                remove_temporary_file(temp_path)
                raise StorageError(
                    f"Cannot copy Agent prompt fragment {fragment_name}: {exc}"
                ) from exc
            written_paths.append(target_path)
        return written_paths

    def agent_prompts_dir(self, agent_id: str) -> Path:
        """Return the prompt-fragment directory for one Agent."""

        safe_agent_id = self._validate_agent_id(agent_id)
        return self._data_dir / "agents" / safe_agent_id / "prompts"

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

        self._ensure_directories()
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temporary_path(self._data_dir, target_path)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        except OSError as exc:
            remove_temporary_file(temp_path)
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

        self._ensure_directories()
        temp_path = temporary_path(self._data_dir, target_path)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        except OSError as exc:
            remove_temporary_file(temp_path)
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

        self._ensure_directories()
        temp_path = temporary_path(self._data_dir, target_path)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        except OSError as exc:
            remove_temporary_file(temp_path)
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
    def _validate_prompt_fragment_name(fragment_name: str) -> str:
        path = Path(fragment_name)
        if path.name != fragment_name or path.is_absolute():
            raise StorageError(f"Unsafe prompt fragment name: {fragment_name}")
        if fragment_name not in PROMPT_FRAGMENT_NAMES:
            raise StorageError(f"Unknown prompt fragment: {fragment_name}")
        return fragment_name

    @staticmethod
    def _validate_agent_id(agent_id: str) -> str:
        if not is_valid_agent_id(agent_id):
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
