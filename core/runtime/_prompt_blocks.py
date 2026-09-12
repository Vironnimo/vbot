"""Adapt persisted prompt blocks to the Prompt domain storage contract."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from core.extensions import ExtensionRegistry
from core.prompts import (
    AGENT_SCOPE_KEY_PREFIX,
    DEFAULT_SCOPE_KEY,
    BlockDefinition,
    LayoutEntry,
    SystemPromptManager,
)
from core.tools.tools import ToolPromptBlockRegistry


class _StorageBlockBackend(Protocol):
    """The storage block surface the storage-backed ``BlockStore`` adapter bridges to.

    Phase 2's ``StorageManager`` exposes these with the storage scope convention:
    ``None`` = default, a bare ``"<agent-id>"`` = that agent's scope. Declared as a
    Protocol so the adapter depends on the read/write surface, not the concrete
    ``StorageManager``.
    """

    def read_block_layout(self, scope: str | None) -> list[LayoutEntry]:
        """Return a scope's saved block layout (``[]`` when none)."""
        ...

    def read_block_override(self, scope: str | None, block_id: str) -> str | None:
        """Return a block's saved override text in a scope (``None`` when absent)."""
        ...

    def write_block_layout(self, scope: str | None, entries: Sequence[LayoutEntry]) -> Path:
        """Atomically write a scope's ordered block layout."""
        ...

    def prune_block_layout(
        self,
        scope: str | None,
        entries: Sequence[LayoutEntry],
        known_ids: frozenset[str] | set[str],
    ) -> Path:
        """Write a scope's layout keeping only entries with a live definition."""
        ...

    def seed_agent_block_layout(
        self,
        agent_id: str,
        default_layout: Sequence[LayoutEntry],
        *,
        overwrite: bool = False,
    ) -> Path | None:
        """Seed an agent scope's block layout from the current default layout."""
        ...

    def write_block_override(self, scope: str | None, block_id: str, content: str) -> Path:
        """Atomically write a block's text override in a scope."""
        ...

    def remove_block_override(self, scope: str | None, block_id: str) -> bool:
        """Remove a block's text override in a scope (``True`` when one existed)."""
        ...


class _StorageManagerBlockStore:
    """Adapt the storage manager's block I/O to the prompts ``BlockStore``.

    This is the composition-root seam where the prompts-domain scope-key convention
    (``"default"`` / ``"agent:<id>"``) meets the storage-domain scope-token
    convention (``None`` / bare ``"<id>"``). It bridges **both** the method-name
    difference (``read_layout`` → ``read_block_layout``) and the scope translation,
    in one place, for the read **and** the write side. Every method routes its scope
    key through the single :meth:`_to_store_scope` translation so the two
    conventions never diverge.
    """

    def __init__(self, storage: _StorageBlockBackend) -> None:
        self._storage = storage

    def read_layout(self, scope_key: str) -> list[LayoutEntry]:
        return self._storage.read_block_layout(self._to_store_scope(scope_key))

    def read_block_override(self, scope_key: str, block_id: str) -> str | None:
        return self._storage.read_block_override(self._to_store_scope(scope_key), block_id)

    def write_layout(self, scope_key: str, entries: Sequence[LayoutEntry]) -> None:
        self._storage.write_block_layout(self._to_store_scope(scope_key), entries)

    def prune_layout(
        self, scope_key: str, entries: Sequence[LayoutEntry], known_ids: frozenset[str]
    ) -> None:
        self._storage.prune_block_layout(self._to_store_scope(scope_key), entries, known_ids)

    def seed_agent_layout(
        self, scope_key: str, default_layout: Sequence[LayoutEntry], *, overwrite: bool = False
    ) -> None:
        # Only an agent scope key seeds an agent layout; the storage method keys by
        # the bare agent id, so translate and pass it through.
        store_scope = self._to_store_scope(scope_key)
        if store_scope is None:
            return
        self._storage.seed_agent_block_layout(store_scope, default_layout, overwrite=overwrite)

    def write_block_override(self, scope_key: str, block_id: str, content: str) -> None:
        self._storage.write_block_override(self._to_store_scope(scope_key), block_id, content)

    def remove_block_override(self, scope_key: str, block_id: str) -> bool:
        return self._storage.remove_block_override(self._to_store_scope(scope_key), block_id)

    @staticmethod
    def _to_store_scope(scope_key: str) -> str | None:
        """Translate a prompts scope key to the storage scope token.

        ``"default"`` → ``None`` (the storage default scope); ``"agent:<id>"`` →
        the bare ``"<id>"`` the storage layer keys an agent scope by. Any other
        value is passed through unchanged as a defensive fallback.
        """
        if scope_key == DEFAULT_SCOPE_KEY:
            return None
        if scope_key.startswith(AGENT_SCOPE_KEY_PREFIX):
            return scope_key[len(AGENT_SCOPE_KEY_PREFIX) :]
        return scope_key


def _collect_prompt_block_definitions(
    tools: ToolPromptBlockRegistry | None, extensions: ExtensionRegistry | None
) -> list[BlockDefinition]:
    """Gather the contributed block definitions (tool + extension blocks).

    The runtime side of the unified contributor path (D6): it merges the
    tool-owned blocks (from :class:`ToolPromptBlockRegistry`) with the loaded
    extensions' blocks (from the extension registry) and hands the list to the
    prompt manager. The core/data/memory blocks are built by the manager
    itself; this method supplies only what contributors declare. Rebuilt on
    every extension/skill reload so the list never goes stale.
    """
    definitions: list[BlockDefinition] = []
    if tools is not None:
        definitions.extend(tools.block_definitions())
    if extensions is not None:
        definitions.extend(extensions.prompt_block_declarations())
    return definitions


def _loaded_extension_names(extensions: ExtensionRegistry | None) -> set[str]:
    """Return the loaded-extension name set for the prompt manager's gate 2."""
    if extensions is None:
        return set()
    return extensions.loaded_extension_names()


def refresh_prompt_blocks(
    prompts: SystemPromptManager | None,
    tools: ToolPromptBlockRegistry | None,
    extensions: ExtensionRegistry | None,
) -> None:
    """Replace contributed definitions and loaded-owner membership together."""
    if prompts is not None:
        prompts.update_block_definitions(
            _collect_prompt_block_definitions(tools, extensions),
            _loaded_extension_names(extensions),
        )
