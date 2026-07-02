"""Prompt-fragment storage: bundled defaults, optional user copies, Agent scopes.

A :class:`PromptFragmentStore` resolves the system-prompt fragment default texts.
The bundled resource under ``resources/prompts/`` is the normal source; a
hand-created copy in ``<data_dir>/prompts/`` overrides it (nothing seeds or writes
those copies anymore — user prompt edits live in the block override store, see
``prompt_blocks.py``). Agent scopes under ``<data_dir>/agents/<agent_id>/prompts``
are seeded once when an Agent's custom prompt scope is enabled.
``StorageManager`` owns one instance and delegates its prompt methods here.
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
        "runtime.md",
        "tools.md",
        "tools_list.md",
        "channels.md",
        "skills.md",
        "compaction.md",
    }
)
AGENT_PROMPT_FRAGMENT_NAMES = frozenset(
    {
        "runtime.md",
        "tools.md",
        "tools_list.md",
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

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Read a prompt fragment, preferring a hand-created data-dir copy.

        The bundled resource is the normal source; a copy the user placed in
        ``<data_dir>/prompts/`` overrides it (nothing seeds those copies).
        """

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
