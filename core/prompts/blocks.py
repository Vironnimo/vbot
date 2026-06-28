"""System Prompt block contract and the pure assembly engine.

This module is the keystone of the block-model System Prompt (D1–D6 in
``stuff/HANDOFF-system-prompt-architecture.md``). It owns, with **no** storage,
RPC, or runtime wiring:

- the block contract types (:class:`BlockDefinition`, :class:`BlockRenderContext`,
  the kind/source/owner vocabulary and the layout entry shape),
- the producer registry for ``{generated:NAME}`` markers,
- the dedupe / ordering / three-gate filter pure functions, and
- the deterministic assembly entry point (:func:`assemble_system_prompt`).

Everything here takes its inputs **injected** — the definition list, the layout,
an override-text resolver, and the owner-active inputs — so the engine is fully
unit-testable in isolation. Phases 2–3 supply the real storage-backed overrides,
layout, and contributor declarations; this module never reaches for them itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol

from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.prompts.prompts import ProjectPromptContext, PromptAgent

_LOGGER = get_logger("prompts")

# Exactly one blank line separates two rendered blocks. The old format padded
# missing pieces with blank lines so an identity agent stayed byte-identical;
# the block model trims each block and joins survivors with this separator
# instead (deliberately abandoned per the handoff "Model in One Picture").
BLOCK_SEPARATOR = "\n\n"

# ``{generated:NAME}`` injects a producer's output into an editable text block.
# An unknown NAME renders to "" and warns (fail-soft, like a missing include).
GENERATED_PATTERN = re.compile(r"\{generated:([^{}]+)\}")
# ``{include:filename}`` pulls a flat, safe workspace file into a text block.
INCLUDE_PATTERN = re.compile(r"\{include:([^{}]+)\}")
# Shared immutable empty mapping for the optional build-time replacements default,
# so the keyword default is not a mutable object and every "no replacements" caller
# shares one instance.
MAPPING_PROXY_EMPTY: Mapping[str, str] = MappingProxyType({})


class PromptError(VBotError, ValueError):
    """Raised when a public prompt-domain request is invalid.

    Defined here, at the engine layer, and re-exported from ``core.prompts.prompts``
    so the public name stays ``PromptError`` while the assembly engine (which the
    manager imports, not the reverse) can raise it without a circular import. A
    malformed block-template directive (an unsafe ``{include:…}`` path) raises this
    — unchanged behavior. A failed dynamic render or an unknown
    ``{generated:…}``/``{include:…}`` marker is **never** this error: those are
    fail-soft warn-and-drop.
    """


# A block's content kind. ``text`` blocks flow through the override cascade and
# may carry ``{include:…}``/``{generated:…}`` markers; ``data`` blocks are not
# editable (auto-lists, SOUL, project files, agent body, dynamic render blocks).
BlockKind = Literal["text", "data"]
BLOCK_KIND_TEXT: Literal["text"] = "text"
BLOCK_KIND_DATA: Literal["data"] = "data"


class BlockSource(StrEnum):
    """The canonical source namespaces, named for typed comparison.

    A block id's source is the text before its first ``:`` and is kept as a plain
    string (see :func:`parse_block_source`), because contributors may legitimately
    use a domain prefix beyond the common four — the memory block ships as
    ``memory:guidance`` (D6 lists ``core``/``tool``/``extension``/``user`` as the
    common ones, while the phase examples also use ``memory:``). These members
    exist so code can compare against a well-known source without a string
    literal; they do **not** form a closed allow-list.

    The source is distinct from the block's :class:`owner <BlockDefinition.owner>`,
    even though both vocabularies reuse ``tool:``/``extension:``. The source says
    *who shipped* the block; the owner drives gate 2 (is the owner active). The
    layout persists the source per entry (D3) so an unknown-but-remembered entry
    can still be ranked.
    """

    CORE = "core"
    TOOL = "tool"
    EXTENSION = "extension"
    USER = "user"
    MEMORY = "memory"


def parse_block_source(block_id: str) -> str:
    """Return the source namespace prefix of a fully-qualified block id.

    The namespace is the text before the first ``:`` (``core:intro`` -> ``core``,
    ``memory:guidance`` -> ``memory``). Any non-empty prefix is valid: the prefix
    is an open namespace, not a closed enum. An id with **no** ``:`` is a contract
    error, since every contributor ships a prefixed id (D6).
    """
    prefix, separator, _ = block_id.partition(":")
    if not separator or not prefix:
        raise PromptError(f"block id is missing a source prefix: {block_id!r}")
    return prefix


# A producer turns build-time inputs into the text a ``{generated:NAME}`` marker
# expands to. Today's tool/skill/channel list formatters become producers; the
# new ``memory_files`` producer renders the memory file contents.
BlockProducer = Callable[["BlockRenderContext"], str]
# A dynamic block's build-time render function. Exactly one of ``default_text`` /
# ``render`` is set on a definition. A raising render drops only that block.
BlockRenderer = Callable[["BlockRenderContext"], str]


@dataclass(frozen=True)
class BlockRenderContext:
    """Build-time inputs a dynamic block or a producer may read.

    Boundary (D6): this context carries **no conversation messages** —
    message-dependent content belongs to the ``context`` extension hook, at the
    message level, not in the System Prompt. The tool/skill/channel registries
    are injected into the manager (not here); a producer reaches them through the
    manager-built closures, while this context carries the per-agent/run state.
    """

    agent: PromptAgent
    project_context: ProjectPromptContext | None = None
    scope: str = "default"


@dataclass(frozen=True)
class BlockDefinition:
    """One block a contributor hands to the prompt domain (D6).

    A definition is the immutable unit collected from a source (core resources, a
    tool, an extension, or the user's own custom blocks). Its effective text is
    resolved per agent/run, gated by the three gates, and emitted in layout order.
    """

    id: str
    owner: str
    kind: BlockKind = BLOCK_KIND_TEXT
    default_text: str | None = None
    render: BlockRenderer | None = None
    default_rank: int = 0

    def __post_init__(self) -> None:
        # Exactly one of default_text / render: a static block carries text, a
        # dynamic block carries a render function. The source prefix must parse,
        # so a malformed id fails at construction, not deep inside assembly.
        parse_block_source(self.id)
        has_text = self.default_text is not None
        has_render = self.render is not None
        if has_text == has_render:
            raise PromptError(f"block {self.id!r} must set exactly one of default_text / render")

    @property
    def source(self) -> str:
        """The block's source namespace prefix, parsed from its id."""
        return parse_block_source(self.id)

    @property
    def editable(self) -> bool:
        """Whether this block is user-editable through the override cascade.

        A convenience for the metadata surface: a static text block is editable;
        a dynamic block (``render`` set) and any ``data`` block are not.
        """
        return self.kind == BLOCK_KIND_TEXT and self.default_text is not None


@dataclass(frozen=True)
class LayoutEntry:
    """One ordered entry in a scope's layout (D3): a block id + on/off + source.

    The source is persisted so an entry whose definition is momentarily gone
    (a contributor removed between writes) can still be ranked by its namespace's
    default rank. An entry is ``enabled`` by default — a defaulted-in block is on.
    """

    id: str
    enabled: bool = True
    source: str | None = None


@dataclass(frozen=True)
class ResolvedBlock:
    """A definition paired with its user-enabled flag, in final layout order.

    The output of :func:`resolve_layout` — the deterministic input to gating and
    assembly. ``enabled`` is gate 1 (user-enabled); the definition still has to
    clear gate 2 (owner-active) and gate 3 (non-empty) before it renders.
    """

    definition: BlockDefinition
    enabled: bool


class OwnerActivity(Protocol):
    """The owner-active inputs gate 2 reads, injected into the assembly.

    The manager already holds these seams (tool allowlist, memory mode, channel
    state, the loaded-extension set); gate 2 reads them here rather than
    re-implementing or hardcoding any of them (D5).
    """

    def is_owner_active(self, owner: str, agent: PromptAgent) -> bool:
        """Return whether *owner* is active for *agent* on this run."""
        ...


# Resolves a static text block's effective override text. Phase 1 injects a plain
# map / callable so the engine stays storage-free; Phase 2 supplies the real
# cascade (agent override ← default override ← owner default). ``None`` means "no
# override — use the definition's default_text".
OverrideResolver = Callable[[BlockDefinition, str], str | None]


def dedupe_definitions(
    definitions: Sequence[BlockDefinition],
) -> list[BlockDefinition]:
    """Return the definitions with id collisions removed, first-collected wins.

    Mirrors ``ExtensionRegistry._apply_one_tool``'s first-wins-and-diagnose
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
        # (mirrors ExtensionRegistry._apply_one_tool naming both sides).
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
      (ties broken by id, lexicographically), defaulting to ``enabled=True`` —
      this is how a newly added contributor appears.

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
    resolved.extend(ResolvedBlock(definition=definition, enabled=True) for definition in missing)
    return resolved


def passes_gates(
    block: ResolvedBlock,
    agent: PromptAgent,
    owner_activity: OwnerActivity,
    rendered_text: str,
) -> bool:
    """Return whether a block clears all three gates (D5).

    A block renders only when **all three** hold:

    1. user-enabled — its layout entry is ``enabled`` (a defaulted-in block is on).
    2. owner-active — the owner is active for this agent/run (delegated to the
       injected :class:`OwnerActivity`, never hardcoded here).
    3. non-empty — the effective rendered text is non-empty after trim.
    """
    if not block.enabled:
        return False
    if not owner_activity.is_owner_active(block.definition.owner, agent):
        return False
    return bool(rendered_text.strip())


def expand_generated_markers(
    text: str,
    producers: Mapping[str, BlockProducer],
    context: BlockRenderContext,
) -> str:
    """Replace every ``{generated:NAME}`` marker with its producer's output.

    A known marker is replaced by ``producer(context)`` (which may be empty — no
    skills renders ``""`` and the marker leaves no trace after normalization). An
    **unknown** marker renders to ``""`` and logs a warning — fail-soft, mirroring
    a missing ``{include:…}``; it is never a :class:`PromptError`.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        producer = producers.get(name)
        if producer is None:
            _LOGGER.warning("Skipping unknown generated marker: {generated:%s}", name)
            return ""
        return producer(context)

    return GENERATED_PATTERN.sub(replace, text)


def expand_workspace_includes(text: str, workspace: str) -> str:
    """Replace every ``{include:filename}`` with the workspace file, fail-soft.

    The single include-expansion path, reused by the manager. An empty workspace
    means "no includes": every marker is dropped with no read and no warning —
    it must never resolve against ``Path("")`` (= ``Path(".")``), which would read
    SOUL.md/USER.md from the server's process CWD. A safe flat filename resolves
    under the workspace and is ``<file>``-wrapped; a missing **or** unreadable file
    is dropped with a warning (a prompt file never aborts a run — user decision).
    An **unsafe** include path raises :class:`PromptError` — that is a
    malformed directive, not a readability issue.
    """
    if not workspace:
        return INCLUDE_PATTERN.sub("", text)

    workspace_path = Path(workspace)

    def replace(match: re.Match[str]) -> str:
        filename = match.group(1).strip()
        validate_workspace_include(filename)
        include_path = workspace_path / filename
        try:
            content = include_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            _LOGGER.warning("Skipping missing workspace include: %s", include_path)
            return ""
        except (OSError, ValueError) as exc:
            # Present but unreadable for ANY reason (locked, no permission, a
            # directory, binary/non-UTF-8, …): log and drop the block, like a
            # missing include. A prompt file must never abort the run.
            _LOGGER.warning("Skipping unreadable workspace include %s: %s", include_path, exc)
            return ""
        return wrap_include_file(filename, content)

    return INCLUDE_PATTERN.sub(replace, text)


def wrap_include_file(filename: str, content: str) -> str:
    """Wrap a file's content in the canonical ``<file name="…">`` block.

    The single wrap used by workspace includes, project files, and the
    ``memory_files`` producer, so the rendering paths cannot drift in framing.
    """
    return f'<file name="{filename}">\n{content}\n</file>'


def validate_workspace_include(filename: str) -> None:
    """Raise :class:`PromptError` unless *filename* is a safe flat name.

    Checks both POSIX and Windows semantics so separators and drive prefixes of
    either platform are rejected on any host (deployment is Linux, development is
    Windows). Only a bare filename in the workspace root is accepted.
    """
    posix_path = PurePosixPath(filename)
    windows_path = PureWindowsPath(filename)
    if (
        posix_path.name != filename
        or posix_path.is_absolute()
        or windows_path.name != filename
        or windows_path.is_absolute()
    ):
        raise PromptError(f"Unsafe workspace include: {filename}")


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
      resolver, falling back to ``default_text``), expand ``{generated:…}``
      (producers) then ``{include:…}`` (workspace files) — both fail-soft — and
      finally apply the build-time ``replacements`` (the runtime variables
      ``{host}``/``{model}``/… that stay literal placeholders, not ``{generated:…}``,
      and are filled here at build). Replacements are plain text substitution, so a
      block that does not contain a placeholder is untouched.
    - **static data** block: its ``default_text`` is rendered **verbatim** —
      never run through marker/include/replacement expansion (mirrors today's
      "agent body substituted last, literally"). A data block that needs expansion
      uses a ``render`` function instead.
    """
    definition = block.definition
    if definition.render is not None:
        try:
            return definition.render(context)
        except Exception as exc:  # noqa: BLE001 - one block drops, never the run
            _LOGGER.warning("Dropping dynamic block %r: render failed: %s", definition.id, exc)
            return ""

    override = override_resolver(definition, context.scope)
    text = override if override is not None else (definition.default_text or "")
    if definition.kind == BLOCK_KIND_DATA:
        # Verbatim data (e.g. a config-agent body): inserted as-is, its "{…}" is
        # never interpreted. It is still positioned by layout order; "verbatim"
        # means only that its content is not run through expansion.
        return text

    expanded = expand_generated_markers(text, producers, context)
    included = expand_workspace_includes(expanded, context.agent.workspace)
    return apply_replacements(included, replacements)


def apply_replacements(text: str, replacements: Mapping[str, str]) -> str:
    """Replace each build-time placeholder in *text* with its value (plain text).

    The single home for the runtime-variable substitution (``{host}``, ``{model}``,
    …). Plain ``str.replace`` per key — never a format engine — so an unrelated
    ``{…}`` in the text is left untouched and only the known runtime placeholders
    are filled.
    """
    if not replacements:
        return text
    for placeholder, value in replacements.items():
        if placeholder in text:
            text = text.replace(placeholder, value)
    return text


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
        text = resolve_block_text(
            block,
            context,
            override_resolver=override_resolver,
            producers=producers,
            replacements=replacements,
        )
        if passes_gates(block, context.agent, owner_activity, text):
            rendered.append(text)
    return normalize_blocks(rendered)


@dataclass(frozen=True)
class CallableOwnerActivity:
    """An :class:`OwnerActivity` backed by a plain ``(owner, agent) -> bool``.

    A small adapter so the manager (and tests) can supply the owner-active gate
    as a closure over the registries it already holds, without a bespoke class.
    """

    predicate: Callable[[str, PromptAgent], bool]

    def is_owner_active(self, owner: str, agent: PromptAgent) -> bool:
        return self.predicate(owner, agent)


@dataclass(frozen=True)
class MappingOverrideResolver:
    """An :class:`OverrideResolver` backed by a ``{(scope, id): text}`` map.

    Phase 1's storage-free override source for tests and the manager's legacy
    path. A missing key resolves to ``None`` (use the definition default). Phase 2
    replaces this with the real per-scope cascade.
    """

    overrides: Mapping[tuple[str, str], str] = field(default_factory=dict)

    def __call__(self, definition: BlockDefinition, scope: str) -> str | None:
        return self.overrides.get((scope, definition.id))


class BlockStore(Protocol):
    """The persisted layout + per-block text override source the manager reads.

    The seam between the assembly engine (definitions, this module) and the β
    persistence (``layout.json`` + ``blocks/<namespace>/<slug>.md``, Phase 2). The
    manager depends on this Protocol, never the concrete ``PromptBlockStore``, so
    its unit tests inject a fake/empty source exactly as Phase 1's tests do.

    Two read operations, both per scope (``"default"`` or an ``"agent:<id>"`` key
    the manager forms from the resolved build scope):

    - :meth:`read_layout` returns the scope's owned ordered entries, or ``[]`` when
      the scope has no saved layout yet (every block then defaults in at its rank).
    - :meth:`read_block_override` returns one block's saved override text, or
      ``None`` when no override exists (fall back to the owner default).

    The agent-scope cascade (agent override ← default override ← owner default) is
    composed by the manager from these two reads, not by the store.
    """

    def read_layout(self, scope: str) -> list[LayoutEntry]:
        """Return the saved ordered layout entries for *scope* (``[]`` if none)."""
        ...

    def read_block_override(self, scope: str, block_id: str) -> str | None:
        """Return *block_id*'s saved override text for *scope* (``None`` if none)."""
        ...


@dataclass(frozen=True)
class EmptyBlockStore:
    """A :class:`BlockStore` with no saved layout and no overrides.

    The default the manager uses when no persistence is wired (Phase 1-style unit
    tests, and any path that has not been handed Phase 2's store yet): every scope
    reads an empty layout (so all definitions default in at their rank) and no
    block has an override (so every block uses its owner default text).
    """

    def read_layout(self, scope: str) -> list[LayoutEntry]:
        return []

    def read_block_override(self, scope: str, block_id: str) -> str | None:
        return None


def load_layout_entries(raw: object) -> list[LayoutEntry]:
    """Parse a ``layout.json`` payload into :class:`LayoutEntry` objects, fail-soft.

    The bundled default layout and Phase 2's persisted layouts share this one
    parser. The payload is the ordered ``[{"id", "enabled", "source"}, ...]`` list
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
    "BlockDefinition",
    "BlockKind",
    "BlockProducer",
    "BlockRenderContext",
    "BlockRenderer",
    "BlockSource",
    "BlockStore",
    "CallableOwnerActivity",
    "EmptyBlockStore",
    "LayoutEntry",
    "MappingOverrideResolver",
    "OverrideResolver",
    "OwnerActivity",
    "PromptError",
    "ResolvedBlock",
    "apply_replacements",
    "assemble_system_prompt",
    "dedupe_definitions",
    "expand_generated_markers",
    "expand_workspace_includes",
    "load_layout_entries",
    "normalize_blocks",
    "parse_block_source",
    "passes_gates",
    "resolve_block_text",
    "resolve_layout",
    "validate_workspace_include",
    "wrap_include_file",
]
