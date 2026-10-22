"""Block definitions, render inputs, and persisted-layout contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol

from core.tools.file_state import FileRead
from core.utils.errors import VBotError

if TYPE_CHECKING:
    from core.prompts._types import ProjectPromptContext, PromptAgent

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
# editable (auto-lists, SOUL, Working Project data, agent body, dynamic render blocks).
BlockKind = Literal["text", "data"]
BLOCK_KIND_TEXT: Literal["text"] = "text"
BLOCK_KIND_DATA: Literal["data"] = "data"


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
# ``render`` is set on a definition. A raising render drops only that block. A
# render may return a :class:`RenderedBlock` instead of plain text to name the
# catalog its text lists.
BlockRenderer = Callable[["BlockRenderContext"], "str | RenderedBlock"]
# Whether the Agent that receives the rendered text can call a Tool, asked by the
# Tool's registry name. Text names a Tool only when this answers ``True``.
ToolAvailability = Callable[[str], bool]


def no_tools_available(_tool_name: str) -> bool:
    """The :data:`ToolAvailability` of an Agent that can call no Tool."""
    return False


@dataclass(frozen=True)
class BlockCatalog:
    """The keyed list a dynamic block shows, so a change can be told entry by entry.

    ``entries`` pairs each entry's key with the line the block shows for it, in
    display order. ``title`` names the list for the Model (``"Registered
    Projects"``). ``frame`` is everything else the block's text depends on: while
    it stays the same, a changed block differs from its earlier text only in its
    entries.
    """

    title: str
    entries: tuple[tuple[str, str], ...]
    frame: str = ""


@dataclass(frozen=True)
class RenderedBlock:
    """A dynamic block's rendered text, and the catalog it lists when it has one."""

    text: str
    catalog: BlockCatalog | None = None


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
    # Session-pinned Working Project Context. When present, the Working Project
    # block emits this text verbatim instead of reading ``project_context`` again.
    working_project_context: str | None = None
    # Session-pinned rendered SOUL block text. When present, the SOUL block emits
    # this text verbatim instead of re-reading the workspace ``SOUL.md``.
    soul_context: str | None = None
    # Session-pinned rendered pinned-memory text. When present, the
    # ``memory_files`` producer emits this text instead of re-reading
    # ``USER.md``/``MEMORY.md`` from the workspace.
    memory_files_context: str | None = None
    # Addressing scope of the Agent itself. This is deliberately separate from
    # ``project_context``: an Identity Session working in a Project receives
    # Working Project context while remaining identity-scoped for Sub-Agent routing.
    agent_project_id: str | None = None
    # The prompt is built for a Session linked to a Parent Agent (a Sub-Agent
    # Session); Tool-owned guidance can depend on that role.
    subagent_session: bool = False
    scope: str = "default"
    # Optional side channel for build-time file reads. When set, every prompt file
    # whose content actually reaches the prompt (a workspace ``{include:…}``, SOUL,
    # the project auto-load files, the pinned-memory files) reports its resolved
    # absolute path and pre-read metadata here. Chat records these captured stamps in the
    # read-before-write guard, so an agent can edit a file it was auto-shown without
    # a separate read call. ``None`` = no observer — the assembly output is byte-for-
    # byte identical either way, so preview/tests and the prompt cache are unaffected.
    read_observer: Callable[[FileRead], None] | None = None
    # Which Tools the Agent receiving this prompt can call. A notice that stands in
    # for an oversized file names ``read`` only when it answers ``True`` for it; the
    # default names no Tool.
    tool_available: ToolAvailability = no_tools_available


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
    # The enabled state a block defaults in with when a scope's layout does not
    # list it. ``False`` lets a contributor ship an opt-in block that stays off
    # even for scopes with an older persisted layout (a layout entry, once
    # written, always wins over this default).
    default_enabled: bool = True
    # A static text block that only makes sense around one producer's output
    # names that ``{generated:NAME}`` producer here: the block renders nothing
    # while the producer renders nothing, whatever its (edited) text says.
    requires_generated: str | None = None

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
    default rank. A listed entry with no explicit flag is ``enabled``; a block
    with no entry at all defaults in with its definition's ``default_enabled``.
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
    """The persisted layout + per-block text override source the manager drives.

    The seam between the assembly engine (definitions, this module) and the β
    persistence (``layout.json`` + ``blocks/<namespace>/<slug>.md``, Phase 2). The
    manager depends on this Protocol, never the concrete ``PromptBlockStore``, so
    its unit tests inject a fake/empty source exactly as Phase 1's tests do.

    Read side, both per scope (``"default"`` or an ``"agent:<id>"`` key the manager
    forms from the resolved build scope):

    - :meth:`read_layout` returns the scope's owned ordered entries, or ``[]`` when
      the scope has no saved layout yet (every block then defaults in at its rank).
    - :meth:`read_block_override` returns one block's saved override text, or
      ``None`` when no override exists (fall back to the owner default).

    Write side (Phase 4 — the block-edit facade calls these): the manager is the
    only writer, so the store stays a dumb persistence sink. Every write is atomic
    in the concrete store; ids and slugs are validated there before any path is
    built (defense in depth on top of the manager's edge validation).

    - :meth:`write_layout` persists a scope's ordered entries verbatim; ``reset``
      replaces a saved layout that fails to load, which a plain write refuses.
    - :meth:`prune_layout` persists only the entries whose id is still in
      *known_ids*, dropping a contributor-gone id on the next write (tolerate, D4).
    - :meth:`seed_agent_layout` seeds an agent scope's layout from a default layout,
      preserving an existing one unless *overwrite* (returns ``None`` when preserved).
    - :meth:`write_block_override` writes one block's override text.
    - :meth:`remove_block_override` removes one block's override (``True`` when one
      existed; removing a missing override is a no-op).

    The agent-scope cascade (agent override ← default override ← owner default) is
    composed by the manager from the two reads, not by the store.
    """

    def read_layout(self, scope: str) -> list[LayoutEntry]:
        """Return the saved ordered layout entries for *scope* (``[]`` if none)."""
        ...

    def read_block_override(self, scope: str, block_id: str) -> str | None:
        """Return *block_id*'s saved override text for *scope* (``None`` if none)."""
        ...

    def write_layout(
        self, scope: str, entries: Sequence[LayoutEntry], *, reset: bool = False
    ) -> None:
        """Persist *scope*'s ordered layout entries verbatim (``reset``: replace a broken one)."""
        ...

    def prune_layout(
        self, scope: str, entries: Sequence[LayoutEntry], known_ids: frozenset[str]
    ) -> None:
        """Persist *scope*'s layout keeping only entries whose id is in *known_ids*."""
        ...

    def seed_agent_layout(
        self, scope: str, default_layout: Sequence[LayoutEntry], *, overwrite: bool = False
    ) -> None:
        """Seed an agent *scope*'s layout from *default_layout* (preserve unless overwrite)."""
        ...

    def write_block_override(self, scope: str, block_id: str, content: str) -> None:
        """Persist *block_id*'s override text in *scope*."""
        ...

    def remove_block_override(self, scope: str, block_id: str) -> bool:
        """Remove *block_id*'s override in *scope* (``True`` when one existed)."""
        ...


@dataclass(frozen=True)
class EmptyBlockStore:
    """A :class:`BlockStore` with no saved layout and no overrides.

    The default the manager uses when no persistence is wired (Phase 1-style unit
    tests, and any path that has not been handed Phase 2's store yet): every scope
    reads an empty layout (so all definitions default in at their rank) and no
    block has an override (so every block uses its owner default text). The write
    side is a no-op sink — a manager wired without persistence accepts edits but
    drops them, so an edit path never crashes for the lack of a real store.
    """

    def read_layout(self, scope: str) -> list[LayoutEntry]:
        return []

    def read_block_override(self, scope: str, block_id: str) -> str | None:
        return None

    def write_layout(
        self, scope: str, entries: Sequence[LayoutEntry], *, reset: bool = False
    ) -> None:
        return None

    def prune_layout(
        self, scope: str, entries: Sequence[LayoutEntry], known_ids: frozenset[str]
    ) -> None:
        return None

    def seed_agent_layout(
        self, scope: str, default_layout: Sequence[LayoutEntry], *, overwrite: bool = False
    ) -> None:
        return None

    def write_block_override(self, scope: str, block_id: str, content: str) -> None:
        return None

    def remove_block_override(self, scope: str, block_id: str) -> bool:
        return False
