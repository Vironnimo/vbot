"""Pinned memory service backed by agent workspace Markdown files."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from core.memory._definitions import (
    _INTERNAL_WRITER,
    _MAX_SCOPE_BUDGET,
    _MEMORY_BLOCK_TEMPLATE,
    _MEMORY_GUIDANCE,
    DEFAULT_MEMORY_PROMPT_MODE,
    MEMORY_BLOCK_ID,
    MEMORY_BLOCK_OWNER,
    MEMORY_FILES,
    MEMORY_FILES_PRODUCER_NAME,
    MEMORY_PROMPT_MODE_AGENT,
    MEMORY_PROMPT_MODE_AGENT_USER,
    MEMORY_PROMPT_MODE_OFF,
    MEMORY_PROMPT_MODE_SCOPES,
    MEMORY_PROMPT_MODES,
    MEMORY_SCOPE_LABELS,
    MEMORY_SCOPES,
    MemoryBudgetError,
    MemoryEntry,
    MemoryError,
    MemoryMatchError,
    MemoryPromptMode,
    MemoryRevertError,
    MemoryRevertIncompleteError,
    MemoryRevertResult,
    MemoryScope,
    MemoryTextChange,
    MemoryWriter,
    validate_memory_prompt_mode,
    validate_memory_scope,
)
from core.memory._file_backend import (
    FilePinnedMemoryBackend,
    _read_entries,
    _total,
    match_memory_entries,
)
from core.memory._history import MemoryChange, MemoryHistory, MemoryRevertConflict, MemoryRevision

if TYPE_CHECKING:
    from core.prompts.blocks import BlockDefinition


class MemoryService:
    """Small facade for pinned memory operations.

    Every mutation that changes a file logs one INFO line naming the Agent, the
    scope, who caused it (the writer's ``actor``) and the resulting entry count
    and character change - never entry text. With a ``history_root`` (the agents
    directory), each Agent's changes are also kept in its Memory history.
    """

    def __init__(
        self,
        backend: FilePinnedMemoryBackend | None = None,
        *,
        history_root: Path | None = None,
    ) -> None:
        history = MemoryHistory(history_root) if history_root is not None else None
        self._backend = backend or FilePinnedMemoryBackend(history)

    def list_entries(self, workspace: Path, scope: MemoryScope) -> list[MemoryEntry]:
        return self._backend.list_entries(workspace, scope)

    def add_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        content: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryEntry:
        return self._backend.add_entry(workspace, scope, content, writer=writer)

    def replace_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        entry_id: int,
        content: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryEntry:
        return self._backend.replace_entry(workspace, scope, entry_id, content, writer=writer)

    def remove_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        entry_id: int,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryEntry:
        return self._backend.remove_entry(workspace, scope, entry_id, writer=writer)

    def find_matches(self, workspace: Path, scope: MemoryScope, old_text: str) -> list[str]:
        return self._backend.find_matches(workspace, scope, old_text)

    def replace_matching(
        self,
        workspace: Path,
        scope: MemoryScope,
        old_text: str,
        content: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryTextChange:
        return self._backend.replace_matching(workspace, scope, old_text, content, writer=writer)

    def remove_matching(
        self,
        workspace: Path,
        scope: MemoryScope,
        old_text: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryTextChange:
        return self._backend.remove_matching(workspace, scope, old_text, writer=writer)

    def history(self, workspace: Path, agent_id: str) -> list[MemoryRevision]:
        return self._backend.history(workspace, agent_id)

    def entries_at(
        self, workspace: Path, agent_id: str, revision_id: int | None
    ) -> dict[MemoryScope, list[str]]:
        return self._backend.entries_at(workspace, agent_id, revision_id)

    def compare(
        self, workspace: Path, agent_id: str, from_id: int, to_id: int | None = None
    ) -> dict[MemoryScope, tuple[MemoryChange, ...]]:
        return self._backend.compare(workspace, agent_id, from_id, to_id)

    def recorded_revisions(self, agent_id: str) -> list[MemoryRevision]:
        return self._backend.recorded_revisions(agent_id)

    def check_revert(
        self,
        workspace: Path,
        revision_ids: Sequence[int],
        *,
        writer: MemoryWriter,
        strict: bool = False,
        related: Collection[int] = (),
    ) -> tuple[MemoryScope, ...]:
        return self._backend.check_revert(
            workspace, revision_ids, writer=writer, strict=strict, related=related
        )

    def revert(
        self,
        workspace: Path,
        revision_ids: Sequence[int],
        *,
        writer: MemoryWriter,
        strict: bool = False,
        related: Collection[int] = (),
    ) -> MemoryRevertResult:
        return self._backend.revert(
            workspace, revision_ids, writer=writer, strict=strict, related=related
        )

    def scope_usage(self, workspace: Path, scope: MemoryScope) -> tuple[int, int]:
        return self._backend.scope_usage(workspace, scope)

    def render_scopes(self, workspace: Path, scopes: Sequence[MemoryScope]) -> str:
        return self._backend.render_scopes(workspace, scopes)

    def read_prompt_files(
        self, workspace: Path, mode: MemoryPromptMode, *, memory_tool: str | None
    ) -> str:
        return self._backend.read_prompt_files(workspace, mode, memory_tool=memory_tool)


class _MemoryFileReader(Protocol):
    """The one method :func:`read_memory_files` needs from a memory provider."""

    def read_prompt_files(
        self, workspace: Path, mode: MemoryPromptMode, *, memory_tool: str | None
    ) -> str:
        """Return the rendered pinned-memory entries for a mode."""
        ...


def read_memory_files(
    workspace: Path,
    mode: MemoryPromptMode,
    *,
    provider: _MemoryFileReader,
    memory_tool: str | None,
) -> str:
    """Render the embedded ``{generated:memory_files}`` text for one agent/mode.

    The single entry point the ``memory_files`` producer wraps: only the rendered
    pinned-memory entries (each scope's heading label plus its ``- `` bullets, no
    guidance, no ``<memory>`` wrapper), ``""`` only when the mode is ``off`` — a mode
    that selects scopes always renders them, an empty scope via its placeholder. Kept
    in the memory domain so the producer the manager registers stays a thin closure;
    *provider* is the manager's memory provider (a :class:`MemoryService` or any stub
    exposing ``read_prompt_files``). *memory_tool* is the name under which the Agent
    can call the memory Tool, or ``None``; see
    :meth:`FilePinnedMemoryBackend.read_prompt_files`.
    """
    return provider.read_prompt_files(Path(workspace), mode, memory_tool=memory_tool)


def memory_prompt_file_paths(workspace: Path, mode: MemoryPromptMode) -> list[Path]:
    """Resolved absolute paths of the pinned-memory files the prompt shows whole.

    The prompt renders every scope a mode selects — an empty one via its placeholder
    — but only an on-disk file can be stamped as read-before-write: a still-absent
    file has nothing whose ``(mtime, size)`` to record, and a later Add to it is
    a new-file write (exempt) anyway. A file above its scope's budget shows only
    part of its entries, so stamping it would let the Agent overwrite entries it
    never saw. So this returns just the existing selected files within their budget,
    resolved the same way the backend reads them (``workspace / the scope's file``),
    for the chat loop to stamp. ``off`` mode selects no scope, so it returns ``[]``.
    The caller must not pass an empty-string workspace (a config agent) — that would
    resolve against ``Path(".")``; config agents are ``off`` mode and gated out
    upstream regardless.
    """
    workspace_path = Path(workspace)
    paths: list[Path] = []
    for scope in MEMORY_PROMPT_MODE_SCOPES[validate_memory_prompt_mode(mode)]:
        path = (workspace_path / MEMORY_FILES[scope]).resolve()
        if not path.is_file():
            continue
        try:
            entries = _read_entries(path)
        except MemoryError:
            continue
        if _total(entries) <= _MAX_SCOPE_BUDGET[scope]:
            paths.append(path)
    return paths


def memory_block_definition() -> BlockDefinition:
    """Return the ``memory:guidance`` block declaration (D6).

    The memory domain ships its own block: a static, editable ``text`` block whose
    default text is the guidance prose wrapped in ``<memory>`` with an embedded
    ``{generated:memory_files}`` marker (one sortable unit, D2). Owner ``memory``
    drives gate 2, so the block — guidance included — renders whenever the memory
    tool is enabled, even with empty/absent files (the D5 empty-memory fix). The
    file contents are supplied by the ``memory_files`` producer (see
    :func:`read_memory_files`), which the runtime registers on the manager.

    Imported lazily so the memory domain carries no import-time dependency on the
    prompts package (which depends back on memory through the tool-availability
    seam); the factory runs only at block collection, never at module import.
    """
    from core.prompts.blocks import BLOCK_KIND_TEXT, BlockDefinition

    default_text = _MEMORY_BLOCK_TEMPLATE.format(
        guidance=_MEMORY_GUIDANCE,
        producer=MEMORY_FILES_PRODUCER_NAME,
    )
    return BlockDefinition(
        id=MEMORY_BLOCK_ID,
        owner=MEMORY_BLOCK_OWNER,
        kind=BLOCK_KIND_TEXT,
        default_text=default_text,
    )


__all__ = [
    "DEFAULT_MEMORY_PROMPT_MODE",
    "FilePinnedMemoryBackend",
    "MEMORY_BLOCK_ID",
    "MEMORY_BLOCK_OWNER",
    "MEMORY_FILES",
    "MEMORY_FILES_PRODUCER_NAME",
    "MEMORY_PROMPT_MODES",
    "MEMORY_PROMPT_MODE_AGENT",
    "MEMORY_PROMPT_MODE_AGENT_USER",
    "MEMORY_PROMPT_MODE_OFF",
    "MEMORY_PROMPT_MODE_SCOPES",
    "MEMORY_SCOPES",
    "MEMORY_SCOPE_LABELS",
    "MemoryBudgetError",
    "MemoryEntry",
    "MemoryError",
    "MemoryMatchError",
    "MemoryPromptMode",
    "MemoryRevertConflict",
    "MemoryRevertError",
    "MemoryRevertIncompleteError",
    "MemoryRevertResult",
    "MemoryScope",
    "MemoryTextChange",
    "MemoryWriter",
    "match_memory_entries",
    "validate_memory_prompt_mode",
    "validate_memory_scope",
]
