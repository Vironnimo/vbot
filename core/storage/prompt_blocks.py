"""Block-model System Prompt persistence: ``layout.json`` + per-block overrides.

A :class:`PromptBlockStore` owns the β-persistence half of the block model
(D3/D4/T1 in ``stuff/HANDOFF-system-prompt-architecture.md``): per scope it
persists an ordered ``layout.json`` plus thin per-block text overrides under
``blocks/<namespace>/<slug>.md``. It sits beside
:class:`core.storage.prompt_fragments.PromptFragmentStore` and shares its
data-dir / agent-scope / atomic-write idioms; ``StorageManager`` owns one
instance and delegates its block methods here.

Scope vocabulary: ``None`` is the default scope (rooted at ``<data_dir>/prompts``);
a non-empty agent-id string is that agent's scope (rooted at
``<data_dir>/agents/<agent-id>/prompts``). The block id ``<namespace>:<slug>``
maps to ``blocks/<namespace>/<slug>.md`` — the colon never reaches the disk
(Windows-safe), the namespace is a fixed closed set, and the slug is validated
with the canonical agent-id rule. This module is the single id-to-path writer;
unsafe ids, slugs, and namespaces are rejected with :class:`StorageError`, never
sanitized into a path.

This store carries **no** owner default text: a missing override file reads as
``None`` and the definition layer (Phase 1/3) supplies the default. A dynamic
block has no override path at all — the store never invents one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

from core.prompts import LayoutEntry
from core.settings import is_valid_agent_id
from core.storage.atomic import remove_temporary_file, temporary_path
from core.storage.errors import StorageError

# The block-id source prefixes that may appear on disk as a ``blocks/<namespace>``
# subfolder. A fixed closed set (D3): an unknown namespace is invalid storage
# data, not something to coerce into a path. ``memory`` ships as a core block
# under the ``memory:`` source (see ``core/prompts/blocks.py`` ``BlockSource``).
BLOCK_NAMESPACES = frozenset({"core", "tool", "extension", "user", "memory"})

# Subfolder under a scope root that holds the per-block override files.
BLOCKS_DIRNAME = "blocks"
# The ordered layout file name, written per scope beside ``blocks/``.
LAYOUT_FILENAME = "layout.json"
# Override files are plain Markdown text bodies.
OVERRIDE_SUFFIX = ".md"


class PromptBlockStore:
    """Owns ``layout.json`` and per-block override I/O for the data directory.

    Every write is atomic (temp file under ``<data_dir>/.tmp`` + ``os.replace``);
    every id is validated before a path is built. The store is the only place
    that turns a block id into a filesystem path.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        ensure_directories: Callable[[], None],
    ) -> None:
        self._data_dir = data_dir
        self._ensure_directories = ensure_directories

    # -- Scope roots --------------------------------------------------------

    @property
    def prompts_dir(self) -> Path:
        """Default-scope prompts directory (``<data_dir>/prompts``)."""

        return self._data_dir / "prompts"

    def agent_prompts_dir(self, agent_id: str) -> Path:
        """Agent-scope prompts directory (``<data_dir>/agents/<id>/prompts``)."""

        safe_agent_id = self._validate_agent_id(agent_id)
        return self._data_dir / "agents" / safe_agent_id / "prompts"

    def scope_root(self, scope: str | None) -> Path:
        """Resolve a scope token to its prompts directory.

        ``None`` is the default scope; a non-empty string is an agent id and
        resolves to that agent's scope. The agent id is validated here, so an
        unsafe id is rejected before any path is built.
        """

        if scope is None:
            return self.prompts_dir
        return self.agent_prompts_dir(scope)

    def layout_path(self, scope: str | None) -> Path:
        """Return the ``layout.json`` path for a scope."""

        return self.scope_root(scope) / LAYOUT_FILENAME

    def block_override_path(self, scope: str | None, block_id: str) -> Path:
        """Map a block id to its override ``.md`` path under a scope.

        The single id-to-path mapping: validates the namespace and slug, then
        joins ``blocks/<namespace>/<slug>.md`` under the scope root. The colon
        in the id never reaches the path.
        """

        namespace, slug = self._split_block_id(block_id)
        return self.scope_root(scope) / BLOCKS_DIRNAME / namespace / f"{slug}{OVERRIDE_SUFFIX}"

    # -- Layout I/O ---------------------------------------------------------

    def read_layout(self, scope: str | None) -> list[LayoutEntry]:
        """Read a scope's ordered layout, or ``[]`` when none is written yet.

        A missing ``layout.json`` reads as empty — the scope owns no order, so
        Phase 1 defaults every block in at its definition rank. Each JSON object
        becomes a :class:`LayoutEntry`; ``enabled`` defaults to ``True`` and a
        missing ``source`` is left ``None`` (Phase 1 re-derives it from the
        definition).
        """

        layout_path = self.layout_path(scope)
        if not layout_path.exists():
            return []

        try:
            raw = layout_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot read layout {layout_path}: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageError(f"Invalid layout JSON in {layout_path}: {exc}") from exc

        if not isinstance(parsed, list):
            raise StorageError(f"Layout {layout_path} must be a JSON array of entries")

        return [self._parse_layout_entry(item, layout_path) for item in parsed]

    def write_layout(self, scope: str | None, entries: Sequence[LayoutEntry]) -> Path:
        """Atomically write a scope's ordered layout to ``layout.json``.

        Writes the entries verbatim, in order — pruning is a separate caller
        decision (see :meth:`prune_layout`). The on-disk shape is the D3 list
        ``[{"id", "enabled", "source"}, ...]``; ``source`` is omitted from a
        record when the entry does not carry one.
        """

        payload = [self._serialize_layout_entry(entry) for entry in entries]
        target_path = self.layout_path(scope)
        self._write_json_atomic(target_path, payload, label="layout")
        return target_path

    def prune_layout(
        self,
        scope: str | None,
        entries: Sequence[LayoutEntry],
        known_ids: frozenset[str] | set[str],
    ) -> Path:
        """Write a scope's layout keeping only entries with a live definition.

        Inert pruning (D4): an entry whose id is not in *known_ids* (its
        contributor is gone) is omitted on this next write, never an error. The
        surviving entries keep their order and flags. The caller supplies the
        current definition id set; new entries it wants to add must already be in
        *entries*.
        """

        live_entries = [entry for entry in entries if entry.id in known_ids]
        return self.write_layout(scope, live_entries)

    def seed_agent_layout(
        self,
        agent_id: str,
        default_layout: Sequence[LayoutEntry],
        *,
        overwrite: bool = False,
    ) -> Path | None:
        """Seed an agent scope's ``layout.json`` from the default layout (D4).

        On ``custom_system_prompt_enabled`` activation the agent's scope owns its
        own order + on/off, seeded once from the current effective default layout
        and independent afterwards. Mirrors ``copy_agent_prompt_fragments``: an
        existing agent ``layout.json`` is preserved unless ``overwrite`` is true
        (returns ``None`` when it preserves an existing file). Text overrides are
        **not** copied — the agent inherits block text until it overrides it.
        """

        target_path = self.layout_path(agent_id)
        if target_path.exists() and not overwrite:
            return None
        return self.write_layout(agent_id, default_layout)

    # -- Per-block override I/O ---------------------------------------------

    def read_block_override(self, scope: str | None, block_id: str) -> str | None:
        """Read a block's text override in a scope, or ``None`` when absent.

        ``None`` means the scope does not override this block, so the cascade
        falls back to the next layer (default scope, then the owner default from
        the definition). A dynamic block has no override file by design.
        """

        override_path = self.block_override_path(scope, block_id)
        if not override_path.exists():
            return None

        try:
            return override_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Cannot read block override {override_path}: {exc}") from exc

    def write_block_override(self, scope: str | None, block_id: str, content: str) -> Path:
        """Atomically write a block's text override in a scope.

        Creates the ``blocks/<namespace>/`` subfolder on demand and writes the
        body (UTF-8). For a ``user:<slug>`` custom block this is the file half of
        its existence (T1); the layout entry half is a separate call.
        """

        target_path = self.block_override_path(scope, block_id)
        self._ensure_directories()
        target_path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = temporary_path(self._data_dir, target_path)
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)
        except OSError as exc:
            remove_temporary_file(temp_path)
            raise StorageError(f"Cannot write block override {target_path}: {exc}") from exc

        return target_path

    def remove_block_override(self, scope: str | None, block_id: str) -> bool:
        """Remove a block's text override in a scope.

        Returns whether a file existed. Removing a missing override is a no-op
        (returns ``False``) — resetting an inherited block or removing a custom
        block twice must not error.
        """

        override_path = self.block_override_path(scope, block_id)
        try:
            override_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageError(f"Cannot remove block override {override_path}: {exc}") from exc
        return True

    # -- Override cascade ---------------------------------------------------

    def resolve_effective_text(
        self,
        agent_scope: str | None,
        block_id: str,
        owner_default: str | None,
    ) -> str | None:
        """Resolve a static block's effective text through the D3 cascade.

        Precedence: **agent override ← default-scope override ← owner default**.
        The agent scope is checked only when *agent_scope* is given (the agent
        owns its form with ``custom_system_prompt_enabled``); otherwise the
        cascade starts at the default scope. *owner_default* is the definition's
        default text and is the final fallback.

        A dynamic block has no default text (``owner_default is None``) and no
        override path, so it resolves to ``None`` here — the engine renders it
        from its render function instead, never from this store.
        """

        if owner_default is None:
            # Dynamic block: no override path, no default text. The store must
            # not invent one — the engine renders it from its render function.
            return None

        if agent_scope is not None:
            agent_override = self.read_block_override(agent_scope, block_id)
            if agent_override is not None:
                return agent_override

        default_override = self.read_block_override(None, block_id)
        if default_override is not None:
            return default_override

        return owner_default

    # -- Validation & id-to-path mapping ------------------------------------

    @staticmethod
    def _split_block_id(block_id: str) -> tuple[str, str]:
        """Split a block id into ``(namespace, slug)`` with full validation.

        The single id parser: the namespace is the text before the first ``:``
        and must be in the closed :data:`BLOCK_NAMESPACES` set; the slug is the
        remainder and must pass the canonical agent-id rule. Path traversal,
        absolute paths, and separators of either POSIX or Windows flavor all fail
        the agent-id rule and are rejected with :class:`StorageError`.
        """

        if not isinstance(block_id, str):
            raise StorageError(f"Block id must be a string: {block_id!r}")

        namespace, separator, slug = block_id.partition(":")
        if not separator:
            raise StorageError(f"Block id is missing a source prefix: {block_id}")
        if namespace not in BLOCK_NAMESPACES:
            raise StorageError(f"Unknown block namespace: {block_id}")
        if not is_valid_agent_id(slug):
            raise StorageError(f"Unsafe block slug: {block_id}")

        # Defense in depth on top of the slug rule: the slug must be a bare path
        # component under both path flavors, so a separator/drive that somehow
        # slipped the agent-id rule still cannot escape the namespace folder.
        if (
            PurePosixPath(slug).name != slug
            or PurePosixPath(slug).is_absolute()
            or PureWindowsPath(slug).name != slug
            or PureWindowsPath(slug).is_absolute()
        ):
            raise StorageError(f"Unsafe block slug: {block_id}")

        return namespace, slug

    @staticmethod
    def _validate_agent_id(agent_id: str) -> str:
        if not is_valid_agent_id(agent_id):
            raise StorageError(f"Unsafe agent id: {agent_id}")
        return agent_id

    @staticmethod
    def _parse_layout_entry(item: object, layout_path: Path) -> LayoutEntry:
        if not isinstance(item, dict):
            raise StorageError(f"Layout entry in {layout_path} must be an object")

        block_id = item.get("id")
        if not isinstance(block_id, str) or not block_id:
            raise StorageError(f"Layout entry in {layout_path} is missing a string id")

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise StorageError(
                f"Layout entry {block_id} in {layout_path} has a non-boolean enabled"
            )

        source = item.get("source")
        if source is not None and not isinstance(source, str):
            raise StorageError(f"Layout entry {block_id} in {layout_path} has a non-string source")

        return LayoutEntry(id=block_id, enabled=enabled, source=source)

    @staticmethod
    def _serialize_layout_entry(entry: LayoutEntry) -> dict[str, object]:
        record: dict[str, object] = {"id": entry.id, "enabled": entry.enabled}
        if entry.source is not None:
            record["source"] = entry.source
        return record

    def _write_json_atomic(self, target_path: Path, payload: object, *, label: str) -> None:
        self._ensure_directories()
        target_path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = temporary_path(self._data_dir, target_path)
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temp_path, target_path)
        except OSError as exc:
            remove_temporary_file(temp_path)
            raise StorageError(f"Cannot write {label} {target_path}: {exc}") from exc
