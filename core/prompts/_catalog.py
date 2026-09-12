"""Editable prompt scopes, definitions and override persistence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.memory import (
    memory_block_definition,
)
from core.prompts._types import (
    AGENT_SCOPE_KEY_PREFIX,
    BLOCK_OWNER_ALWAYS,
    BLOCK_OWNER_CHANNEL,
    BLOCK_OWNER_IDENTITY,
    BLOCK_OWNER_SKILL_MANAGE,
    CORE_AGENT_BODY_BLOCK_ID,
    CORE_CHANNELS_BLOCK_ID,
    CORE_IDENTITY_RUNTIME_BLOCK_ID,
    CORE_RUNTIME_BLOCK_ID,
    CORE_SKILL_MAINTENANCE_BLOCK_ID,
    CORE_SKILLS_BLOCK_ID,
    CORE_SOUL_BLOCK_ID,
    CORE_TOOLS_BLOCK_ID,
    CORE_TOOLS_LIST_BLOCK_ID,
    CORE_WORKING_PROJECT_BLOCK_ID,
    DEFAULT_SCOPE_KEY,
    INHERITANCE_AGENT_OVERRIDE,
    INHERITANCE_DEFAULT_OVERRIDE,
    INHERITANCE_OWNER_DEFAULT,
    USER_BLOCK_ID_PREFIX,
    USER_BLOCK_SOURCE,
    JsonObject,
    PromptAgent,
    PromptAgentStore,
    PromptFragmentReader,
    PromptScope,
)
from core.prompts.blocks import (
    BLOCK_KIND_DATA,
    BlockDefinition,
    BlockRenderContext,
    BlockStore,
    LayoutEntry,
    PromptError,
    dedupe_definitions,
    load_layout_entries,
    resolve_layout,
)
from core.settings import is_valid_agent_id
from core.utils.logging import get_logger

_RESOURCES_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "resources" / "prompts"
_LAYOUT_FILENAME = "layout.json"
_LOGGER = get_logger("prompts")


class PromptBlockCatalog:
    """Own editable scopes, block definitions, layouts and override inheritance."""

    def __init__(
        self,
        storage: PromptFragmentReader,
        block_store: BlockStore,
        default_layout: Sequence[LayoutEntry],
        agent_store: PromptAgentStore | None,
        definitions: Sequence[BlockDefinition],
    ) -> None:
        self._storage = storage
        self._block_store = block_store
        self._default_layout = tuple(default_layout)
        self._agent_store = agent_store
        self.definitions = tuple(definitions)

    def list_scopes(self) -> list[JsonObject]:
        """Return prompt scopes available to the System Prompt editor.

        ``default`` plus every Agent with ``custom_system_prompt_enabled`` (the
        Agent owns its own layout/overrides for that scope). With no agent store
        the default scope is the only scope. Each agent-scope entry carries
        ``has_customizations``: true when that scope owns a saved layout and/or at
        least one block text override, so the editor can warn before the custom
        prompt is disabled. The default scope carries no such flag.
        """
        scopes: list[JsonObject] = [{"type": "default", "label": "Default"}]
        if self._agent_store is None:
            return scopes
        for agent in sorted(self._agent_store.list(), key=lambda item: item.id):
            if not agent.custom_system_prompt_enabled:
                continue
            scopes.append(
                {
                    "type": "agent",
                    "agent_id": agent.id,
                    "label": agent.name or agent.id,
                    "has_customizations": self._agent_scope_has_customizations(agent),
                }
            )
        return scopes

    def _agent_scope_has_customizations(self, agent: PromptAgent) -> bool:
        """Return whether an agent scope owns any persisted prompt customization.

        True when the scope has a saved layout (its own order/on-off) and/or at
        least one editable block carries an agent-scope text override — both read
        straight off the injected :class:`BlockStore`, the same reads the block
        list uses. A custom ``user:`` block shows up through the saved layout (its
        existence is a layout entry), so a non-empty layout already catches it.
        """
        prompt_scope = PromptScope(type="agent", agent_id=agent.id)
        scope_key = self.scope_key(prompt_scope)
        if self._block_store.read_layout(scope_key):
            return True
        for definition in self._listing_block_definitions(prompt_scope):
            if not definition.editable:
                continue
            if self._block_store.read_block_override(scope_key, definition.id) is not None:
                return True
        return False

    def validate_scope(self, scope: Any = None) -> PromptScope:
        """Resolve and validate a public prompt scope payload (RPC edge helper)."""
        return self._resolve_edit_scope(scope)

    def list_blocks(self, scope: Any = None) -> list[JsonObject]:
        """Return per-block static metadata for a scope, in layout order (D3).

        Each entry carries ``id``, ``owner``, ``kind`` (``text``/``data``),
        ``editable``, ``enabled`` (the active scope's on/off), ``rank`` (its
        position in layout order), and ``source``. Editable text blocks also carry
        their effective ``text`` (the override cascade result) and ``is_modified``;
        for an agent scope every block carries its ``inheritance`` layer (T5). Non-
        editable blocks carry only the read-only metadata (the tab renders a label).
        "Owner currently active?" is deliberately **not** here — that is the
        preview's job, not static metadata.
        """
        prompt_scope = self._resolve_edit_scope(scope)
        scope_key = self.scope_key(prompt_scope)
        definitions = self._listing_block_definitions(prompt_scope)
        layout = self.resolve_layout(scope_key)
        resolved = resolve_layout(definitions, layout)
        return [
            self._block_metadata(block.definition, prompt_scope, rank=rank, enabled=block.enabled)
            for rank, block in enumerate(resolved)
        ]

    def update_block(self, block_id: str, content: str, scope: Any = None) -> JsonObject:
        """Write a block's text override and return its new state (T6 autosave).

        Only an **editable** block (a static ``text`` block with a default, or a
        ``user:`` custom block) accepts an update; updating a data/dynamic block is
        a :class:`PromptError` (mapped to ``invalid_request`` at the edge). The
        override is written through the store's cascade (Phase 2). Returns the new
        effective ``text`` plus ``is_modified``/``inheritance``.
        """
        prompt_scope = self._resolve_edit_scope(scope)
        definition = self._require_editable_block(prompt_scope, block_id)
        self._block_store.write_block_override(self.scope_key(prompt_scope), definition.id, content)
        return self._block_metadata_with_text(definition, prompt_scope)

    def reset_block(self, block_id: str, scope: Any = None) -> JsonObject:
        """Reset a block to its inherited/default text (T5 "reset → inherited").

        Default scope: drop the default override → the owner default. Agent scope:
        drop the agent override → the inherited (default-scope override, else owner
        default). A ``user:`` block has no default to fall back to — resetting one
        is a :class:`PromptError` (delete it with :meth:`remove_block`).
        """
        prompt_scope = self._resolve_edit_scope(scope)
        definition = self._require_block(prompt_scope, block_id)
        if definition.source == USER_BLOCK_SOURCE:
            raise PromptError(f"a custom block has no default to reset: {block_id}")
        if not definition.editable:
            raise PromptError(f"block is not editable: {block_id}")
        self._block_store.remove_block_override(self.scope_key(prompt_scope), definition.id)
        return self._block_metadata_with_text(definition, prompt_scope)

    def set_layout(self, layout: Sequence[Mapping[str, Any]], scope: Any = None) -> JsonObject:
        """Persist a scope's order + on/off, tolerating a contributor-gone id (T6).

        ``layout`` is the client's ordered ``[{id, enabled}]`` list. Validation:
        each ``id`` is a non-empty string and ``enabled`` a bool; ``source`` is
        derived server-side from the live definition (or kept from the client only
        as inert metadata for a remembered id). Unknown ids are **tolerated**: the
        store prunes any entry whose id has no live definition on this write, so a
        reorder never errors on a contributor that vanished. Returns the persisted
        effective layout (post-prune), as ``[{id, enabled, source}]``.
        """
        prompt_scope = self._resolve_edit_scope(scope)
        entries = self._parse_layout_payload(layout)
        known_ids = self._known_block_ids(prompt_scope)
        scope_key = self.scope_key(prompt_scope)
        self._block_store.prune_layout(scope_key, entries, known_ids)
        live_entries = [entry for entry in entries if entry.id in known_ids]
        return {"layout": [self._layout_entry_dict(entry) for entry in live_entries]}

    def create_block(
        self,
        slug: str,
        content: str | None = None,
        scope: Any = None,
        *,
        position: int | None = None,
    ) -> JsonObject:
        """Create a custom ``user:<slug>`` text block (T1): override file + layout entry.

        The slug is validated with the canonical agent-id rule here too (defense in
        depth behind the RPC edge and the store). The block must not collide with an
        existing ``user:`` block in the scope. Writes the override (``content`` or
        empty) and inserts a layout entry at ``position`` (default: end). Owner
        ``always``, ``kind="text"``. Returns the new block's metadata.
        """
        prompt_scope = self._resolve_edit_scope(scope)
        if not is_valid_agent_id(slug):
            raise PromptError(f"invalid custom block slug: {slug!r}")
        block_id = f"{USER_BLOCK_ID_PREFIX}{slug}"
        scope_key = self.scope_key(prompt_scope)
        existing = self.resolve_layout(scope_key)
        if any(entry.id == block_id for entry in existing):
            raise PromptError(f"custom block already exists: {block_id}")

        self._block_store.write_block_override(scope_key, block_id, content or "")
        new_entry = LayoutEntry(id=block_id, enabled=True, source=USER_BLOCK_SOURCE)
        entries = self._insert_layout_entry(list(existing), new_entry, position)
        known_ids = self._known_block_ids(prompt_scope) | {block_id}
        self._block_store.prune_layout(scope_key, entries, known_ids)

        definition = self._custom_block_definition(block_id)
        rank = next(index for index, entry in enumerate(entries) if entry.id == block_id)
        return self._block_metadata(definition, prompt_scope, rank=rank, enabled=True)

    def remove_block(self, block_id: str, scope: Any = None) -> JsonObject:
        """Delete a custom ``user:`` block: drop the override file + the layout entry.

        Only a ``user:`` block is removable — a core/tool/extension block is toggled
        off through :meth:`set_layout`, never deleted (removing one is a
        :class:`PromptError`). Returns the updated effective layout.
        """
        prompt_scope = self._resolve_edit_scope(scope)
        if not block_id.startswith(USER_BLOCK_ID_PREFIX):
            raise PromptError(f"only custom user blocks can be removed: {block_id}")
        scope_key = self.scope_key(prompt_scope)
        self._block_store.remove_block_override(scope_key, block_id)
        remaining = [entry for entry in self.resolve_layout(scope_key) if entry.id != block_id]
        known_ids = self._known_block_ids(prompt_scope)
        self._block_store.prune_layout(scope_key, remaining, known_ids)
        live = [entry for entry in remaining if entry.id in known_ids]
        return {"layout": [self._layout_entry_dict(entry) for entry in live]}

    def reset_layout(self, scope: Any = None) -> JsonObject:
        """Reset a scope's layout (order + on/off) to the bundled default (T6).

        The agreed server-side reset so the tab need not reconstruct the default:
        the active scope's ``layout.json`` is overwritten with the bundled default
        layout. Per-block text overrides are untouched (use :meth:`reset_block` for
        text). Returns the restored effective layout.
        """
        prompt_scope = self._resolve_edit_scope(scope)
        scope_key = self.scope_key(prompt_scope)
        self._block_store.write_layout(scope_key, self._default_layout)
        return {"layout": [self._layout_entry_dict(entry) for entry in self._default_layout]}

    def _resolve_edit_scope(self, scope: Any = None) -> PromptScope:
        """Validate an edit-surface scope payload against the agent store.

        Mirrors the build-scope rule but without a per-call agent: ``default`` is
        always valid; an ``agent`` scope requires a known agent with
        ``custom_system_prompt_enabled``. A missing agent store means the default
        scope is the only valid scope.
        """
        prompt_scope = _normalize_prompt_scope(scope)
        if prompt_scope.type == "default":
            return prompt_scope
        if self._agent_store is None:
            raise PromptError("Agent prompt scopes are not available")
        agent_id = _require_scope_agent_id(prompt_scope)
        try:
            agent = self._agent_store.get(agent_id)
        except Exception as exc:
            raise PromptError(f"unknown prompt scope agent: {agent_id}") from exc
        if not agent.custom_system_prompt_enabled:
            raise PromptError(f"Agent prompt scope is not enabled: {agent_id}")
        return prompt_scope

    def _listing_block_definitions(self, prompt_scope: PromptScope) -> list[BlockDefinition]:
        """Build the full block-definition list for a scope's listing/editing.

        Same set the build collects (core text blocks, memory, the SOUL/Working-
        Project/agent-body data blocks, contributed blocks, and custom blocks from the
        active layout), deduped first-wins.
        The core text blocks read their default text scope-aware (the agent copy for
        an agent scope); the data blocks carry no per-run content here — for metadata
        their owner/kind suffice, and they are non-editable so their effective text
        is never part of the edit payload.
        """
        agent_id = prompt_scope.agent_id or ""
        layout = self.resolve_layout(self.scope_key(prompt_scope))
        definitions = [
            *self.core_text_definitions(prompt_scope, agent_id),
            memory_block_definition(),
            *self._data_listing_definitions(),
            *self.definitions,
            *self.custom_definitions(layout),
        ]
        return dedupe_definitions(definitions)

    def core_text_definitions(
        self, prompt_scope: PromptScope, agent_id: str
    ) -> list[BlockDefinition]:
        return [
            BlockDefinition(
                id=CORE_RUNTIME_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                default_text=self._read_prompt_fragment(prompt_scope, agent_id, "runtime.md"),
            ),
            BlockDefinition(
                id=CORE_IDENTITY_RUNTIME_BLOCK_ID,
                owner=BLOCK_OWNER_IDENTITY,
                default_text=self._read_prompt_fragment(
                    prompt_scope, agent_id, "identity_runtime.md"
                ),
            ),
            BlockDefinition(
                id=CORE_TOOLS_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                default_text=self._read_prompt_fragment(prompt_scope, agent_id, "tools.md"),
            ),
            BlockDefinition(
                id=CORE_TOOLS_LIST_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                default_text=self._read_prompt_fragment(prompt_scope, agent_id, "tools_list.md"),
                default_enabled=False,
            ),
            BlockDefinition(
                id=CORE_CHANNELS_BLOCK_ID,
                owner=BLOCK_OWNER_CHANNEL,
                default_text=self._read_prompt_fragment(prompt_scope, agent_id, "channels.md"),
            ),
            BlockDefinition(
                id=CORE_SKILLS_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                default_text=self._read_prompt_fragment(prompt_scope, agent_id, "skills.md"),
            ),
            BlockDefinition(
                id=CORE_SKILL_MAINTENANCE_BLOCK_ID,
                owner=BLOCK_OWNER_SKILL_MANAGE,
                default_text=self._read_prompt_fragment(
                    prompt_scope, agent_id, "skill_maintenance.md"
                ),
            ),
        ]

    def _data_listing_definitions(self) -> list[BlockDefinition]:
        """Return the SOUL/Working-Project/agent-body data blocks for listing only.

        Each is a non-editable ``data`` block; for metadata only the id/owner/kind
        matter, so a placeholder ``render`` stands in for the per-run content the
        build would produce (the listing never calls these renders).
        """
        return [
            BlockDefinition(
                id=CORE_SOUL_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                kind=BLOCK_KIND_DATA,
                render=_listing_data_placeholder,
            ),
            BlockDefinition(
                id=CORE_WORKING_PROJECT_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                kind=BLOCK_KIND_DATA,
                render=_listing_data_placeholder,
            ),
            BlockDefinition(
                id=CORE_AGENT_BODY_BLOCK_ID,
                owner=BLOCK_OWNER_ALWAYS,
                kind=BLOCK_KIND_DATA,
                render=_listing_data_placeholder,
            ),
        ]

    @staticmethod
    def _custom_block_definition(block_id: str) -> BlockDefinition:
        """Return the definition for a custom ``user:`` block (owner always, text)."""
        return BlockDefinition(id=block_id, owner=BLOCK_OWNER_ALWAYS, default_text="")

    def _require_block(self, prompt_scope: PromptScope, block_id: str) -> BlockDefinition:
        """Return the definition for *block_id* in a scope, or raise if unknown.

        A custom ``user:`` block has no contributor definition, so it is resolved
        on the fly (its existence is its layout entry + override file).
        """
        if block_id.startswith(USER_BLOCK_ID_PREFIX):
            return self._custom_block_definition(block_id)
        by_id = {
            definition.id: definition
            for definition in self._listing_block_definitions(prompt_scope)
        }
        definition = by_id.get(block_id)
        if definition is None:
            raise PromptError(f"unknown block: {block_id}")
        return definition

    def _require_editable_block(self, prompt_scope: PromptScope, block_id: str) -> BlockDefinition:
        definition = self._require_block(prompt_scope, block_id)
        if not definition.editable:
            raise PromptError(f"block is not editable: {block_id}")
        return definition

    def _known_block_ids(self, prompt_scope: PromptScope) -> frozenset[str]:
        """Return the ids of the scope's live definitions plus its custom blocks.

        A custom ``user:`` block has no contributor definition, so its id would be
        pruned as inert on a layout write; keep the scope's existing ``user:``
        layout ids alive so a reorder/toggle never drops a custom block.
        """
        definition_ids = {
            definition.id for definition in self._listing_block_definitions(prompt_scope)
        }
        custom_ids = {
            entry.id
            for entry in self.resolve_layout(self.scope_key(prompt_scope))
            if entry.id.startswith(USER_BLOCK_ID_PREFIX)
        }
        return frozenset(definition_ids | custom_ids)

    def _block_metadata(
        self,
        definition: BlockDefinition,
        prompt_scope: PromptScope,
        *,
        rank: int,
        enabled: bool,
    ) -> JsonObject:
        """Build one block's metadata dict for ``prompt.list``."""
        metadata: JsonObject = {
            "id": definition.id,
            "owner": definition.owner,
            "kind": definition.kind,
            "editable": definition.editable,
            "enabled": enabled,
            "rank": rank,
            "source": definition.source,
        }
        if definition.editable:
            text, inheritance = self._effective_text_and_inheritance(definition, prompt_scope)
            metadata["text"] = text
            metadata["is_modified"] = inheritance != INHERITANCE_OWNER_DEFAULT
            if prompt_scope.type == "agent":
                metadata["inheritance"] = inheritance
        return metadata

    def _block_metadata_with_text(
        self, definition: BlockDefinition, prompt_scope: PromptScope
    ) -> JsonObject:
        """Return one block's post-edit state (effective text + flags, no rank).

        Used by update/reset, which mutate one block and report its new effective
        text and inheritance — the order is unchanged, so no rank/enabled is needed.
        """
        text, inheritance = self._effective_text_and_inheritance(definition, prompt_scope)
        result: JsonObject = {
            "id": definition.id,
            "text": text,
            "is_modified": inheritance != INHERITANCE_OWNER_DEFAULT,
        }
        if prompt_scope.type == "agent":
            result["inheritance"] = inheritance
        return result

    def _effective_text_and_inheritance(
        self, definition: BlockDefinition, prompt_scope: PromptScope
    ) -> tuple[str, str]:
        """Resolve an editable block's effective text and which layer it came from.

        The D3 cascade, read directly off the store: agent override (agent scope
        only) ← default-scope override ← owner default (``definition.default_text``).
        Returns the text and the inheritance layer label (T5 badge input).
        """
        override, inheritance = self._resolve_override(definition, prompt_scope)
        return (
            override if override is not None else definition.default_text or "",
            inheritance,
        )

    def _resolve_override(
        self, definition: BlockDefinition, prompt_scope: PromptScope
    ) -> tuple[str | None, str]:
        """Resolve the same override cascade for editing and prompt assembly."""
        if prompt_scope.type == "agent":
            agent_override = self._block_store.read_block_override(
                self.scope_key(prompt_scope), definition.id
            )
            if agent_override is not None:
                return agent_override, INHERITANCE_AGENT_OVERRIDE
        default_override = self._block_store.read_block_override(DEFAULT_SCOPE_KEY, definition.id)
        if default_override is not None:
            return default_override, INHERITANCE_DEFAULT_OVERRIDE
        return None, INHERITANCE_OWNER_DEFAULT

    @staticmethod
    def _parse_layout_payload(layout: Sequence[Mapping[str, Any]]) -> list[LayoutEntry]:
        """Parse the client ``[{id, enabled}]`` list into validated layout entries.

        ``source`` is not trusted from the client for a live block (it is derived on
        read); a remembered inert id may carry one through, so an optional string
        ``source`` is kept as metadata only.
        """
        if not isinstance(layout, Sequence) or isinstance(layout, str | bytes):
            raise PromptError("layout must be a list of entries")
        entries: list[LayoutEntry] = []
        seen: set[str] = set()
        for item in layout:
            if not isinstance(item, Mapping):
                raise PromptError("each layout entry must be an object")
            block_id = item.get("id")
            if not isinstance(block_id, str) or not block_id:
                raise PromptError("each layout entry requires a string id")
            if block_id in seen:
                raise PromptError(f"duplicate layout entry: {block_id}")
            seen.add(block_id)
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise PromptError(f"layout entry {block_id} enabled must be a boolean")
            source = item.get("source")
            entries.append(
                LayoutEntry(
                    id=block_id,
                    enabled=enabled,
                    source=source if isinstance(source, str) else None,
                )
            )
        return entries

    @staticmethod
    def _insert_layout_entry(
        entries: list[LayoutEntry], new_entry: LayoutEntry, position: int | None
    ) -> list[LayoutEntry]:
        """Insert *new_entry* at *position* (clamped), or append when ``None``."""
        if position is None:
            entries.append(new_entry)
            return entries
        index = max(0, min(position, len(entries)))
        entries.insert(index, new_entry)
        return entries

    @staticmethod
    def _layout_entry_dict(entry: LayoutEntry) -> JsonObject:
        return {"id": entry.id, "enabled": entry.enabled, "source": entry.source}

    def custom_definitions(self, layout: Sequence[LayoutEntry]) -> list[BlockDefinition]:
        """Synthesize a definition for each custom ``user:`` block in *layout* (T1).

        A custom block carries no contributor definition — its content lives only in
        its override file. So the build derives a static ``always``/``text``
        definition (``default_text=""``) per ``user:`` layout entry, and the override
        cascade fills its text. An empty custom block collapses (gate 3).
        """
        return [
            self._custom_block_definition(entry.id)
            for entry in layout
            if entry.id.startswith(USER_BLOCK_ID_PREFIX)
        ]

    def resolve_layout(self, scope_key: str) -> Sequence[LayoutEntry]:
        """Return the active scope's saved layout, or the bundled default if none.

        A scope with a saved ``layout.json`` owns its order + on/off (D3/D4); a
        scope with none defaults to the bundled layout so the shipped order applies.
        Either way, a definition absent from the layout still defaults in at its
        rank (the assembly engine handles that), and an inert entry is skipped.
        """
        saved = self._block_store.read_layout(scope_key)
        return saved if saved else self._default_layout

    def override_resolver(self, prompt_scope: PromptScope) -> Callable[..., str | None]:
        """Build the per-scope override resolver feeding the assembly cascade (D3).

        Implements the override cascade over the injected :class:`BlockStore`:
        agent-scope override ← default-scope override ← owner default (the engine's
        fallback to ``definition.default_text``). For a default build there is no
        agent layer. With the empty store no override exists, so every block uses
        its owner default text (its scope-aware fragment / data content).
        """

        def resolve(definition: BlockDefinition, _scope: str) -> str | None:
            override, _inheritance = self._resolve_override(definition, prompt_scope)
            return override

        return resolve

    @staticmethod
    def scope_key(prompt_scope: PromptScope) -> str:
        """Return the persistence key for a resolved build scope.

        ``default`` for the default scope, ``agent:<id>`` for an agent scope — the
        single string the :class:`BlockStore` and the render context use, so the
        colon-free path mapping (Phase 2) and the override cascade agree on the key.
        """
        if prompt_scope.type == "agent" and prompt_scope.agent_id:
            return f"{AGENT_SCOPE_KEY_PREFIX}{prompt_scope.agent_id}"
        return DEFAULT_SCOPE_KEY

    def _read_prompt_fragment(
        self,
        scope: PromptScope,
        agent_id: str,
        fragment_name: str,
    ) -> str:
        if scope.type == "agent":
            return self._storage.read_agent_prompt_fragment(agent_id, fragment_name)
        return self._storage.read_prompt_fragment(fragment_name)


def _normalize_prompt_scope(scope: Any = None) -> PromptScope:
    if scope is None:
        return PromptScope("default")
    if isinstance(scope, PromptScope):
        return scope
    if not isinstance(scope, Mapping):
        raise PromptError("prompt scope must be an object")

    unsupported_fields = sorted(set(scope) - {"type", "agent_id"})
    if unsupported_fields:
        raise PromptError(f"unsupported prompt scope fields: {', '.join(unsupported_fields)}")

    scope_type = scope.get("type", "default")
    if scope_type == "default":
        if scope.get("agent_id") not in (None, ""):
            raise PromptError("default prompt scope must not include agent_id")
        return PromptScope("default")
    if scope_type == "agent":
        agent_id = scope.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise PromptError("agent prompt scope requires agent_id")
        return PromptScope("agent", agent_id)

    raise PromptError(f"unknown prompt scope type: {scope_type}")


def _require_scope_agent_id(scope: PromptScope) -> str:
    if scope.agent_id is None:
        raise PromptError("agent prompt scope requires agent_id")
    return scope.agent_id


def _listing_data_placeholder(_context: BlockRenderContext) -> str:
    """Stand in for a data block's per-run render during metadata listing.

    The block-edit listing never renders content (data blocks are non-editable),
    so the SOUL/Working-Project/agent-body definitions only need a valid ``render`` to
    satisfy the "exactly one of default_text/render" contract. This is never called.
    """
    return ""


def load_bundled_default_layout() -> list[LayoutEntry]:
    """Load the bundled default block layout from ``resources/prompts/layout.json``.

    The shipped order + on/off of the core blocks, kept in a resource file so the
    default layout lives as data, not code. A missing or malformed file reads as an
    empty layout — every block then defaults in at its rank, so the prompt still
    assembles (a broken bundled layout must never take a build down).
    """
    layout_path = _RESOURCES_PROMPTS_DIR / _LAYOUT_FILENAME
    try:
        raw = json.loads(layout_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LOGGER.warning("Bundled prompt layout not found: %s", layout_path)
        return []
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Skipping unreadable bundled prompt layout %s: %s", layout_path, exc)
        return []
    return load_layout_entries(raw)
