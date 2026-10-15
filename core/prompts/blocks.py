"""Deterministic System Prompt block assembly and the public block API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.prompts._block_types import (
    BLOCK_KIND_DATA,
    BLOCK_KIND_TEXT,
    MAPPING_PROXY_EMPTY,
    BlockCatalog,
    BlockDefinition,
    BlockKind,
    BlockProducer,
    BlockRenderContext,
    BlockRenderer,
    BlockStore,
    CallableOwnerActivity,
    EmptyBlockStore,
    LayoutEntry,
    MappingOverrideResolver,
    OverrideResolver,
    OwnerActivity,
    PromptError,
    RenderedBlock,
    ResolvedBlock,
    parse_block_source,
)
from core.prompts._block_types import (
    ToolAvailability as ToolAvailability,
)
from core.prompts._block_types import (
    no_tools_available as no_tools_available,
)
from core.prompts._templates import (
    FILE_READ_HINT as FILE_READ_HINT,
)
from core.prompts._templates import (
    FILE_TOO_LARGE_NOTICE as FILE_TOO_LARGE_NOTICE,
)
from core.prompts._templates import (
    INLINE_FILE_MAX_BYTES as INLINE_FILE_MAX_BYTES,
)
from core.prompts._templates import (
    PromptFileIssue,
    _render_generated_marker,
    apply_replacements,
    expand_block_template,
    expand_workspace_includes,
    validate_workspace_include,
    wrap_include_file,
)
from core.prompts._templates import (
    render_prompt_file as render_prompt_file,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.prompts._types import PromptAgent

_LOGGER = get_logger("prompts")

# Exactly one blank line separates two rendered blocks. The old format padded
# missing pieces with blank lines so an identity agent stayed byte-identical;
# the block model trims each block and joins survivors with this separator
# instead (deliberately abandoned per the handoff "Model in One Picture").
BLOCK_SEPARATOR = "\n\n"


def dedupe_definitions(
    definitions: Sequence[BlockDefinition],
) -> list[BlockDefinition]:
    """Return the definitions with id collisions removed, first-collected wins.

    Mirrors Extension Tool application's first-wins-and-diagnose
    policy: on two definitions sharing an id, the first in iteration order is
    kept and a warning naming **both** sources is logged. Assembly is always
    handed an already-deduplicated list (this is the seam that guarantees it).
    """
    seen: dict[str, BlockDefinition] = {}
    for definition in definitions:
        existing = seen.get(definition.id)
        if existing is None:
            seen[definition.id] = definition
            continue
        # Same id -> same source prefix by construction; name the kept and the
        # skipped owners so a collision between two contributors is diagnosable
        # (mirrors Extension Tool application naming both sides).
        _LOGGER.warning(
            "Block id %r (source %s) already declared by owner %r; "
            "skipping the duplicate from owner %r",
            definition.id,
            existing.source,
            existing.owner,
            definition.owner,
        )
    return list(seen.values())


def resolve_layout(
    definitions: Sequence[BlockDefinition],
    layout: Sequence[LayoutEntry],
) -> list[ResolvedBlock]:
    """Return the effective ordered ``(definition, enabled)`` list for gating (D3).

    Deterministic for the prompt cache:

    - A layout entry whose id has a matching definition keeps the layout's order
      and ``enabled`` flag.
    - A layout entry with **no** matching definition (contributor gone) is inert:
      skipped here, never an error (Phase 2 prunes it on the next write).
    - A definition **absent** from the layout is inserted at its ``default_rank``
      (ties broken by id, lexicographically), with its ``default_enabled`` state —
      this is how a newly added contributor appears (opt-in blocks default in off).

    Defaulted-in blocks are appended after the explicitly-laid-out ones in
    rank/id order, so the user's chosen order is never disturbed by a new block.
    """
    deduped = dedupe_definitions(definitions)
    by_id = {definition.id: definition for definition in deduped}

    resolved: list[ResolvedBlock] = []
    laid_out_ids: set[str] = set()
    for entry in layout:
        if entry.id in laid_out_ids:
            # A layout that lists the same id twice keeps only the first slot;
            # later duplicates are inert, like an id with no definition.
            continue
        laid_out_ids.add(entry.id)
        definition = by_id.get(entry.id)
        if definition is None:
            continue
        resolved.append(ResolvedBlock(definition=definition, enabled=entry.enabled))

    missing = [definition for definition in deduped if definition.id not in laid_out_ids]
    missing.sort(key=lambda definition: (definition.default_rank, definition.id))
    resolved.extend(
        ResolvedBlock(definition=definition, enabled=definition.default_enabled)
        for definition in missing
    )
    return resolved


def passes_gates(
    block: ResolvedBlock,
    agent: PromptAgent,
    owner_activity: OwnerActivity,
    rendered_text: str,
) -> bool:
    """Return whether a block clears all three gates (D5).

    A block renders only when **all three** hold:

    1. user-enabled — its layout entry is ``enabled`` (a defaulted-in block
       follows its definition's ``default_enabled``).
    2. owner-active — the owner is active for this agent/run (delegated to the
       injected :class:`OwnerActivity`, never hardcoded here).
    3. non-empty — the effective rendered text is non-empty after trim.
    """
    if not block.enabled:
        return False
    if not owner_activity.is_owner_active(block.definition.owner, agent):
        return False
    return bool(rendered_text.strip())


def resolve_block_text(
    block: ResolvedBlock,
    context: BlockRenderContext,
    *,
    override_resolver: OverrideResolver,
    producers: Mapping[str, BlockProducer],
    replacements: Mapping[str, str] = MAPPING_PROXY_EMPTY,
) -> str:
    """Resolve one block's effective text before the non-emptiness gate.

    - **dynamic** block (``render`` set): call ``render(context)`` in isolation —
      on any exception, log a warning and return ``""`` so only this block drops,
      never the run. ``data`` blocks that carry a ``render`` go through here too.
    - **static text** block: take the override cascade result (the injected
      resolver, falling back to ``default_text``) and expand its ``{generated:…}``
      (producers), ``{include:…}`` (workspace files) and build-time
      ``replacements`` (runtime variables such as ``{server_hostname}``/``{model}``)
      in one pass over the template via :func:`expand_block_template` — inserted
      text is never re-expanded, and a block without markers is untouched. A
      block with ``requires_generated`` resolves to ``""`` while that producer
      renders nothing.
    - **static data** block: its ``default_text`` is rendered **verbatim** —
      never run through marker/include/replacement expansion (mirrors today's
      "agent body substituted last, literally"). A data block that needs expansion
      uses a ``render`` function instead.
    """
    definition = block.definition
    if definition.render is not None:
        rendered = render_dynamic_block(definition, context)
        return "" if rendered is None else rendered.text

    if definition.kind == BLOCK_KIND_DATA:
        # Data is not editable and may be request-local, with no storage path.
        return definition.default_text or ""

    required = definition.requires_generated
    if required is not None and not _render_generated_marker(required, producers, context).strip():
        return ""
    override = override_resolver(definition, context.scope)
    text = override if override is not None else (definition.default_text or "")
    return expand_block_template(text, context, producers=producers, replacements=replacements)


def render_dynamic_block(
    definition: BlockDefinition, context: BlockRenderContext
) -> RenderedBlock | None:
    """Call a dynamic block's ``render`` in isolation; ``None`` when it fails.

    A failing render, or one that returns neither text nor a
    :class:`RenderedBlock`, is logged and drops only this block, never the run.
    """
    if definition.render is None:
        raise PromptError(f"block {definition.id!r} has no render function")
    try:
        value = definition.render(context)
        if isinstance(value, RenderedBlock):
            return value
        if isinstance(value, str):
            return RenderedBlock(value)
        raise TypeError(f"render returned {type(value).__name__}, not text")
    except Exception as exc:  # noqa: BLE001 - one block drops, never the run
        _LOGGER.warning("Dropping dynamic block %r: render failed: %s", definition.id, exc)
        return None


def normalize_blocks(rendered_blocks: Sequence[str]) -> str:
    """Join rendered block texts into the final prompt, deterministically.

    Trim each block, drop the empties, and join the survivors with exactly one
    blank line (``\\n\\n``). No leading or trailing blank line, no double blank
    line between blocks, and an empty block/marker leaves no trace — this is the
    contract that replaces the old blank-line padding trick.
    """
    trimmed = [text.strip() for text in rendered_blocks]
    survivors = [text for text in trimmed if text]
    return BLOCK_SEPARATOR.join(survivors)


def assemble_system_prompt(
    definitions: Sequence[BlockDefinition],
    layout: Sequence[LayoutEntry],
    context: BlockRenderContext,
    *,
    owner_activity: OwnerActivity,
    override_resolver: OverrideResolver,
    producers: Mapping[str, BlockProducer],
    replacements: Mapping[str, str] = MAPPING_PROXY_EMPTY,
    block_details: list[dict[str, Any]] | None = None,
) -> str:
    """Assemble the final system prompt from blocks (the public entry point).

    Resolves the layout into the effective ordered ``(definition, enabled)`` list,
    renders each surviving block's effective text (applying the build-time
    ``replacements`` to text blocks), applies the three gates, and normalizes the
    survivors into one string. Deterministic for the cache: the same inputs always
    produce the same output.
    """
    resolved = resolve_layout(definitions, layout)
    rendered: list[str] = []
    for block in resolved:
        active = owner_activity.is_owner_active(block.definition.owner, context.agent)
        if not active or (not block.enabled and block_details is None):
            if block_details is not None:
                block_details.append(
                    {
                        "id": block.definition.id,
                        "owner": block.definition.owner,
                        "enabled": block.enabled,
                        "active": active,
                        "text": "",
                        "included": False,
                    }
                )
            continue
        text = resolve_block_text(
            block,
            context,
            override_resolver=override_resolver,
            producers=producers,
            replacements=replacements,
        )
        included = block.enabled and bool(text.strip())
        if block_details is not None:
            block_details.append(
                {
                    "id": block.definition.id,
                    "owner": block.definition.owner,
                    "enabled": block.enabled,
                    "active": active,
                    "text": text.strip(),
                    "included": included,
                }
            )
        if included:
            rendered.append(text)
    return normalize_blocks(rendered)


def load_layout_entries(raw: object) -> list[LayoutEntry]:
    """Parse a ``layout.json`` payload into :class:`LayoutEntry` objects, fail-soft.

    Used for the bundled default layout; persisted layouts validate complete
    entries in Storage before consumption. The payload is the ordered
    ``[{"id", "enabled", "source"}, ...]`` list
    (D3). A non-list payload yields ``[]``; a malformed entry (not an object, no
    string ``id``) is skipped with a warning rather than aborting assembly — a
    broken layout must never take a run down, it just falls back to defaults. A
    missing ``enabled`` defaults to ``True`` (a listed block is on); ``source`` is
    optional metadata used only to rank an entry whose definition is momentarily
    gone.
    """
    if not isinstance(raw, list):
        if raw is not None:
            _LOGGER.warning("Ignoring malformed prompt layout: expected a list")
        return []

    entries: list[LayoutEntry] = []
    for item in raw:
        if not isinstance(item, Mapping):
            _LOGGER.warning("Skipping malformed layout entry (not an object): %r", item)
            continue
        block_id = item.get("id")
        if not isinstance(block_id, str) or not block_id:
            _LOGGER.warning("Skipping layout entry with no string id: %r", item)
            continue
        enabled = item.get("enabled", True)
        source = item.get("source")
        entries.append(
            LayoutEntry(
                id=block_id,
                enabled=bool(enabled),
                source=source if isinstance(source, str) else None,
            )
        )
    return entries


__all__ = [
    "BLOCK_KIND_DATA",
    "BLOCK_KIND_TEXT",
    "BLOCK_SEPARATOR",
    "BlockCatalog",
    "BlockDefinition",
    "BlockKind",
    "BlockProducer",
    "BlockRenderContext",
    "BlockRenderer",
    "BlockStore",
    "CallableOwnerActivity",
    "EmptyBlockStore",
    "LayoutEntry",
    "MappingOverrideResolver",
    "OverrideResolver",
    "OwnerActivity",
    "PromptError",
    "PromptFileIssue",
    "RenderedBlock",
    "ResolvedBlock",
    "apply_replacements",
    "assemble_system_prompt",
    "dedupe_definitions",
    "expand_block_template",
    "expand_workspace_includes",
    "load_layout_entries",
    "normalize_blocks",
    "parse_block_source",
    "passes_gates",
    "render_dynamic_block",
    "resolve_block_text",
    "resolve_layout",
    "validate_workspace_include",
    "wrap_include_file",
]
