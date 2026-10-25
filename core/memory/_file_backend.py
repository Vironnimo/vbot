"""Workspace Memory file persistence, matching, history and bounded rendering."""

from __future__ import annotations

import os
import threading
import unicodedata
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import ClassVar

from core.memory._definitions import (
    _BULLET_PREFIX,
    _EMPTY_SCOPE_TEXT,
    _INTERNAL_WRITER,
    _LOGGER,
    _MAX_ENTRY_LENGTH,
    _MAX_FILE_BYTES,
    _MAX_SCOPE_BUDGET,
    _PROMPT_CUT_CONDITIONS,
    _PROMPT_CUT_LIST_HINT,
    _PROMPT_CUT_NOTICE,
    _WHITESPACE_PATTERN,
    MEMORY_FILES,
    MEMORY_PROMPT_MODE_SCOPES,
    MEMORY_SCOPE_LABELS,
    PERSONAL_MEMORY_SCOPES,
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
    _RevertPlan,
    validate_memory_prompt_mode,
    validate_memory_scope,
)
from core.memory._history import (
    MemoryChange,
    MemoryHistory,
    MemoryRevision,
    MemoryRevisionKind,
    diff_entries,
    later_touches,
    revert_revisions,
    state_at,
)
from core.tools.file_state import FileRead
from core.utils.atomic import atomic_write_bytes
from core.utils.paths import model_path


class FilePinnedMemoryBackend:
    """Store pinned memory entries in USER.md and MEMORY.md workspace files.

    With a :class:`MemoryHistory`, every change of an Agent's entries is also
    recorded there: mutations by their writer, and changes made to the files
    outside this backend as ``external`` revisions noticed on the next Memory
    operation for that Agent. History failures never block a mutation; a change
    that could not be recorded is noticed as external later.
    """

    # Tool calls in one assistant turn run concurrently (and the memory handler runs
    # in a worker thread), so two mutations to the same file would otherwise read the
    # same starting list and the last writer would clobber the other's entry — a silent
    # lost update that os.replace's atomicity does not prevent. Serialize the
    # read-modify-write per file, process-wide and keyed by canonical path so every
    # backend instance shares the same lock for a given file.
    _file_locks: ClassVar[dict[str, threading.Lock]] = {}
    _file_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        history: MemoryHistory | None = None,
        *,
        project: bool = False,
    ) -> None:
        self._history = history
        self._project = project
        self._scopes: tuple[MemoryScope, ...] = ("project",) if project else PERSONAL_MEMORY_SCOPES

    def _writer_owner(self, writer: MemoryWriter) -> str | None:
        return writer.project_id if self._project else writer.agent_id

    @classmethod
    def _file_lock(cls, path: Path) -> threading.Lock:
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        with cls._file_locks_guard:
            lock = cls._file_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._file_locks[key] = lock
            return lock

    def list_entries(self, workspace: Path, scope: MemoryScope) -> list[MemoryEntry]:
        validated_scope = validate_memory_scope(scope)
        entries = _read_entries(self._path(workspace, validated_scope))
        return _memory_entries(validated_scope, entries)

    def add_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        content: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryEntry:
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        with self._file_lock(path):
            entries = self._read_synced(path, validated_scope, writer)
            normalized = _normalize_entry_content(content)
            if normalized in entries:
                existing_index = entries.index(normalized) + 1
                return MemoryEntry(id=existing_index, scope=validated_scope, content=normalized)

            before = list(entries)
            entries.append(normalized)
            _enforce_scope_budget(validated_scope, entries, _total(before))
            revision = self._commit(path, validated_scope, "added", before, entries, writer)
            return MemoryEntry(len(entries), validated_scope, normalized, revision)

    def replace_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        entry_id: int,
        content: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryEntry:
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        with self._file_lock(path):
            entries = self._read_synced(path, validated_scope, writer)
            index = _entry_index(entry_id, entries)
            normalized = _normalize_entry_content(content)
            before = list(entries)
            entries[index] = normalized
            _enforce_scope_budget(validated_scope, entries, _total(before))
            revision = self._commit(path, validated_scope, "replaced", before, entries, writer)
            return MemoryEntry(entry_id, validated_scope, normalized, revision)

    def remove_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        entry_id: int,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryEntry:
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        with self._file_lock(path):
            entries = self._read_synced(path, validated_scope, writer)
            before = list(entries)
            index = _entry_index(entry_id, entries)
            removed = entries.pop(index)
            revision = self._commit(path, validated_scope, "removed", before, entries, writer)
            return MemoryEntry(entry_id, validated_scope, removed, revision)

    def find_matches(self, workspace: Path, scope: MemoryScope, old_text: str) -> list[str]:
        """Return the entries of one scope that ``old_text`` identifies, changing nothing."""
        validated_scope = validate_memory_scope(scope)
        entries = _read_entries(self._path(workspace, validated_scope))
        return [entries[index] for index in match_memory_entries(entries, old_text)]

    def replace_matching(
        self,
        workspace: Path,
        scope: MemoryScope,
        old_text: str,
        content: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryTextChange:
        """Replace the single entry ``old_text`` identifies (see :func:`match_memory_entries`).

        Matching happens under the file lock, so a concurrent change cannot redirect
        the write. New text equal to another existing entry folds the replaced entry
        into it instead of leaving two identical entries.
        """
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        normalized = _normalize_entry_content(content)
        with self._file_lock(path):
            entries = self._read_synced(path, validated_scope, writer)
            index = _single_match(validated_scope, entries, old_text)
            previous = entries[index]
            revision = None
            if normalized != previous:
                before = list(entries)
                if normalized in entries:
                    entries.pop(index)
                else:
                    entries[index] = normalized
                _enforce_scope_budget(validated_scope, entries, _total(before))
                revision = self._commit(path, validated_scope, "replaced", before, entries, writer)
            return MemoryTextChange(validated_scope, previous, normalized, revision)

    def remove_matching(
        self,
        workspace: Path,
        scope: MemoryScope,
        old_text: str,
        *,
        writer: MemoryWriter = _INTERNAL_WRITER,
    ) -> MemoryTextChange:
        """Remove the single entry ``old_text`` identifies."""
        validated_scope = validate_memory_scope(scope)
        path = self._path(workspace, validated_scope)
        with self._file_lock(path):
            entries = self._read_synced(path, validated_scope, writer)
            before = list(entries)
            removed = entries.pop(_single_match(validated_scope, entries, old_text))
            revision = self._commit(path, validated_scope, "removed", before, entries, writer)
            return MemoryTextChange(validated_scope, removed, None, revision)

    def history(self, workspace: Path, agent_id: str) -> list[MemoryRevision]:
        """Return every revision of the Agent's Memory, oldest first.

        Changes made to the files since the last Memory operation are recorded
        first, so the list ends with the current state.
        """
        history = self._require_history(agent_id)
        with self._scope_locks(workspace):
            for scope in self._scopes:
                path = self._path(workspace, scope)
                history.sync(agent_id, scope, _read_entries(path), path)
            return history.revisions(agent_id)

    def entries_at(
        self, workspace: Path, agent_id: str, revision_id: int | None
    ) -> dict[MemoryScope, list[str]]:
        """Return each scope's entries after *revision_id*, or the current ones for ``None``."""
        revisions = self.history(workspace, agent_id)
        if revision_id is not None:
            _require_revisions(revisions, [revision_id])
        replayed = state_at(revisions, revision_id)
        return {scope: replayed[scope] for scope in self._scopes}

    def compare(
        self, workspace: Path, agent_id: str, from_id: int, to_id: int | None = None
    ) -> dict[MemoryScope, tuple[MemoryChange, ...]]:
        """Describe how each scope changed from *from_id* to *to_id* (current for ``None``)."""
        revisions = self.history(workspace, agent_id)
        _require_revisions(revisions, [from_id] if to_id is None else [from_id, to_id])
        before = state_at(revisions, from_id)
        after = state_at(revisions, to_id)
        return {scope: diff_entries(before[scope], after[scope]) for scope in self._scopes}

    def recorded_revisions(self, agent_id: str) -> list[MemoryRevision]:
        """Return the revisions the Agent's Memory history holds, oldest first.

        Unlike :meth:`history`, this reads only the history: outside changes of
        the files not noticed yet are not recorded first. An Agent without a
        history has no revisions.
        """
        if self._history is None:
            return []
        return self._history.revisions(agent_id)

    def check_revert(
        self,
        workspace: Path,
        revision_ids: Sequence[int],
        *,
        writer: MemoryWriter,
        strict: bool = False,
        related: Collection[int] = (),
    ) -> tuple[MemoryScope, ...]:
        """Check that :meth:`revert` of *revision_ids* would succeed now; change no entry.

        Returns the scopes the revert would change and raises what it would
        raise before writing: :class:`MemoryRevertError`,
        :class:`MemoryBudgetError` or :class:`MemoryError`. Outside changes of
        the files are recorded first, as every history read does. *strict* and
        *related* work as for :meth:`revert`.
        """
        agent_id = self._writer_owner(writer) or ""
        history = self._require_history(agent_id)
        if not revision_ids:
            raise MemoryError("name at least one revision to revert")
        with self._scope_locks(workspace):
            plan = self._plan_revert(
                history, agent_id, workspace, revision_ids, strict=strict, related=related
            )
            return tuple(plan.changed)

    def revert(
        self,
        workspace: Path,
        revision_ids: Sequence[int],
        *,
        writer: MemoryWriter,
        strict: bool = False,
        related: Collection[int] = (),
    ) -> MemoryRevertResult:
        """Take back the changes of *revision_ids*, all of them or none.

        Returns the scopes it changed, none when the entries already match, and
        the ``revert`` revisions that recorded them (fewer when the history could
        not record a change). Raises :class:`MemoryRevertError` when later changes built
        on a reverted one and :class:`MemoryBudgetError` when restored entries
        would exceed a budget, both before writing anything. With *strict*, any
        later revision outside *revision_ids* and *related* that added, removed or
        replaced an entry text one of them changed refuses the revert too, even
        when the change could still be taken back. Every changed scope is
        written before any is recorded: when a write fails, the scopes already
        written get their earlier content back and the failure is raised, or
        :class:`MemoryRevertIncompleteError` when that restore fails too; the
        scopes it names are recorded as reverted.
        """
        agent_id = self._writer_owner(writer) or ""
        history = self._require_history(agent_id)
        if not revision_ids:
            raise MemoryError("name at least one revision to revert")
        with self._scope_locks(workspace):
            plan = self._plan_revert(
                history, agent_id, workspace, revision_ids, strict=strict, related=related
            )
            changed = plan.changed
            try:
                _write_scopes(
                    {scope: plan.paths[scope] for scope in changed}, plan.reverted, plan.originals
                )
            except MemoryRevertIncompleteError as error:
                _LOGGER.warning(
                    "Memory revert failed part-way (agent=%s project=%s scopes=%s actor=%s)",
                    writer.agent_id or "unknown",
                    writer.project_id or "none",
                    ",".join(error.changed),
                    writer.actor,
                )
                # The scopes left reverted keep their record, so a later revert
                # of the same revisions only takes back the rest.
                error.revisions = self._record_reverts(plan, error.changed, writer)
                raise
            recorded = self._record_reverts(plan, changed, writer)
            return MemoryRevertResult(changed=tuple(changed), revisions=recorded)

    def _record_reverts(
        self, plan: _RevertPlan, scopes: Sequence[MemoryScope], writer: MemoryWriter
    ) -> tuple[MemoryRevision, ...]:
        """Log and record the reverted entries of *scopes*; return the recorded revisions."""
        recorded: list[MemoryRevision] = []
        for scope in scopes:
            _log_mutation("reverted", scope, plan.current[scope], plan.reverted[scope], writer)
            revision = self._record(
                scope,
                "revert",
                plan.current[scope],
                plan.reverted[scope],
                writer,
                reverts=[target.id for target in plan.targets if target.scope == scope],
            )
            if revision is not None:
                recorded.append(revision)
        return tuple(recorded)

    def _plan_revert(
        self,
        history: MemoryHistory,
        agent_id: str,
        workspace: Path,
        revision_ids: Sequence[int],
        *,
        strict: bool,
        related: Collection[int],
    ) -> _RevertPlan:
        """Plan a revert under the scope locks: what each scope would read afterwards.

        Raises :class:`MemoryRevertError`, :class:`MemoryBudgetError` or
        :class:`MemoryError` when the revert cannot run.
        """
        paths = {scope: self._path(workspace, scope) for scope in self._scopes}
        originals = {scope: _read_file(path) for scope, path in paths.items()}
        current: dict[MemoryScope, list[str]] = {}
        for scope, path in paths.items():
            current[scope] = _decode_entries(path, originals[scope])
            history.sync(agent_id, scope, current[scope], path)
        revisions = history.revisions(agent_id)
        targets = _require_revisions(revisions, revision_ids)
        baselines = [target.id for target in targets if target.kind == "baseline"]
        if baselines:
            raise MemoryError(
                f"revision {baselines[0]} is where the history starts; it records the "
                "entries that already existed and cannot be reverted"
            )
        if strict:
            touched = later_touches(targets, revisions, exempt={*revision_ids, *related})
            if touched:
                raise MemoryRevertError(touched)
        reverted, conflicts = revert_revisions(current, targets, revisions)
        if conflicts:
            raise MemoryRevertError(conflicts)
        changed = [scope for scope in self._scopes if reverted[scope] != current[scope]]
        for scope in changed:
            _enforce_scope_budget(scope, reverted[scope], _total(current[scope]))
        return _RevertPlan(paths, originals, current, reverted, targets, changed)

    def scope_usage(self, workspace: Path, scope: MemoryScope) -> tuple[int, int]:
        """Return a scope's used characters and its budget."""
        validated_scope = validate_memory_scope(scope)
        entries = _read_entries(self._path(workspace, validated_scope))
        return _total(entries), _MAX_SCOPE_BUDGET[validated_scope]

    def render_scopes(self, workspace: Path, scopes: Sequence[MemoryScope]) -> str:
        """Render scopes exactly as the memory block shows them: heading, usage, bullets."""
        return "\n\n".join(
            self._render_scope_block(Path(workspace), validate_memory_scope(scope))
            for scope in scopes
        )

    def read_prompt_files(
        self,
        workspace: Path,
        mode: MemoryPromptMode,
        *,
        memory_tool: str | None,
        on_read: Callable[[FileRead], None] | None = None,
    ) -> str:
        """Return the rendered pinned-memory entries for a mode.

        The data half of the memory block — only the per-scope entry lists, with no
        ``<memory>`` wrapper and no guidance (those live in the ``memory:guidance``
        block itself). Each selected scope renders under its heading label (see
        :data:`MEMORY_SCOPE_LABELS`) followed by its ``- `` bullet entries, or the
        empty-scope placeholder when it has none. The ``memory_files`` producer calls
        this so the file reading stays in the memory domain. ``off`` mode reads nothing
        (``""``); a not-yet-created or emptied file renders as its label plus the
        placeholder, and an unreadable file raises :class:`MemoryError`.

        A scope above its budget (a hand edit) shows the entries that fit, in file
        order, and a notice counting the rest. *memory_tool* is the name under which
        the Agent can call the memory Tool, or ``None`` when it cannot; the notice
        names the Tool only when it is given.
        """
        validated_mode = validate_memory_prompt_mode(mode)
        return self.read_prompt_scopes(
            workspace,
            MEMORY_PROMPT_MODE_SCOPES[validated_mode],
            memory_tool=memory_tool,
            on_read=on_read,
        )

    def read_prompt_scopes(
        self,
        workspace: Path,
        scopes: Sequence[MemoryScope],
        *,
        memory_tool: str | None,
        on_read: Callable[[FileRead], None] | None = None,
    ) -> str:
        """Render the selected scopes with the same bounded prompt projection."""
        return "\n\n".join(
            self._render_prompt_scope(
                Path(workspace), validate_memory_scope(scope), memory_tool, on_read
            )
            for scope in scopes
        )

    def _render_scope_block(self, workspace: Path, scope: MemoryScope) -> str:
        """Render one scope's memory block: heading, character usage, and entries.

        Reads the scope's entries (never creating the file — a missing file reads as
        no entries), then renders the label with usage against the mutation budget,
        followed by the ``- `` bullet list or the empty-scope placeholder.
        """
        entries = _read_entries(self._path(workspace, scope))
        return _format_scope(scope, _total(entries), entries)

    def _render_prompt_scope(
        self,
        workspace: Path,
        scope: MemoryScope,
        memory_tool: str | None,
        on_read: Callable[[FileRead], None] | None,
    ) -> str:
        """Render one scope for the System Prompt, cut to the scope's budget."""
        path = self._path(workspace, scope)
        reads: list[FileRead] = []
        entries = _decode_entries(path, _read_file(path, on_read=reads.append))
        used = _total(entries)
        budget = _MAX_SCOPE_BUDGET[scope]
        shown, omitted = _fit_budget(entries, budget)
        condition = str(path)
        if not omitted:
            if on_read is not None:
                for read in reads:
                    on_read(read)
            if _PROMPT_CUT_CONDITIONS.ended(condition):
                _LOGGER.info("Showed all Memory entries in the prompt again (path=%s)", path)
            return _format_scope(scope, used, entries)
        if _PROMPT_CUT_CONDITIONS.started(condition):
            _LOGGER.warning(
                "Left Memory entries above the budget out of the prompt "
                "(path=%s chars=%d budget=%d omitted=%d)",
                path,
                used,
                budget,
                len(omitted),
            )
        list_hint = (
            _PROMPT_CUT_LIST_HINT.format(memory_tool=memory_tool, scope=scope)
            if memory_tool is not None
            else ""
        )
        notice = _PROMPT_CUT_NOTICE.format(
            count=len(omitted),
            noun="entry" if len(omitted) == 1 else "entries",
            chars=_total(omitted),
            budget=budget,
            list_hint=list_hint,
        )
        return f"{_format_scope(scope, used, shown, placeholder=False)}\n{notice}"

    def _path(self, workspace: Path, scope: MemoryScope) -> Path:
        workspace_path = Path(workspace)
        return workspace_path / MEMORY_FILES[scope]

    @contextmanager
    def _scope_locks(self, workspace: Path) -> Iterator[None]:
        # Always in the owner's scope order; single-scope mutations hold one lock.
        with ExitStack() as stack:
            for scope in self._scopes:
                stack.enter_context(self._file_lock(self._path(workspace, scope)))
            yield

    def _require_history(self, agent_id: str) -> MemoryHistory:
        if self._history is None or not self._history.available(agent_id):
            owner = "Project" if self._project else "Agent"
            raise MemoryError(f"{owner} '{agent_id}' has no Memory history")
        return self._history

    def _read_synced(self, path: Path, scope: MemoryScope, writer: MemoryWriter) -> list[str]:
        entries = _read_entries(path)
        owner_id = self._writer_owner(writer)
        if self._history is not None and owner_id:
            try:
                self._history.sync(owner_id, scope, entries, path)
            except Exception as exc:  # the change goes ahead without its history
                _log_history_failure("not updated before a change", exc)
        return entries

    def _commit(
        self,
        path: Path,
        scope: MemoryScope,
        event: str,
        before: list[str],
        after: list[str],
        writer: MemoryWriter,
    ) -> int | None:
        """Write *after*, log it and record it; return the recorded revision's id."""
        _write_entries(path, after)
        _log_mutation(event, scope, before, after, writer)
        revision = self._record(scope, "edit", before, after, writer)
        return revision.id if revision is not None else None

    def _record(
        self,
        scope: MemoryScope,
        kind: MemoryRevisionKind,
        before: list[str],
        after: list[str],
        writer: MemoryWriter,
        *,
        reverts: Sequence[int] = (),
    ) -> MemoryRevision | None:
        """Record a written change in the writer's Agent history, when it has one.

        The change is already on disk, so a history failure only logs; the
        unrecorded change is noticed as ``external`` by a later operation.
        """
        owner_id = self._writer_owner(writer)
        if self._history is None or not owner_id:
            return None
        try:
            return self._history.record(
                owner_id,
                scope,
                kind=kind,
                actor=writer.actor,
                changes=diff_entries(before, after),
                entries=after,
                session_id=writer.session_id,
                run_id=writer.run_id,
                run_kind=writer.run_kind,
                agent_id=writer.agent_id if self._project else None,
                reverts=reverts,
            )
        except Exception as exc:  # never fail the change the history describes
            _log_history_failure("did not record a change", exc)
            return None


def _read_entries(path: Path) -> list[str]:
    """Read a memory file's entries — its optionally indented bullet lines, in order.

    The file holds only entries now (no preamble, no section heading), so every
    line whose left-stripped content starts with ``- `` is an entry and anything
    else is ignored. A not-yet-created file reads as no entries — lazy ownership:
    the file is created only on the first write. A file that cannot be read, or
    whose content is not UTF-8 text, raises :class:`MemoryError`.
    """
    return _decode_entries(path, _read_file(path))


def _read_file(path: Path, *, on_read: Callable[[FileRead], None] | None = None) -> bytes | None:
    """Return a memory file's content, or ``None`` when it does not exist yet."""
    try:
        with path.open("rb") as handle:
            status = os.fstat(handle.fileno())
            stamp = (status.st_mtime, status.st_size)
            content = handle.read(_MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MemoryError(
            f"failed to read memory file {model_path(path)}: {_os_error_text(exc)}"
        ) from exc
    if len(content) > _MAX_FILE_BYTES:
        raise MemoryError(
            f"Memory file {model_path(path)} exceeds the {_MAX_FILE_BYTES // 1024} KiB limit. "
            "Nothing was changed. Inspect and shorten this file with file tools or a text "
            "editor before using Memory again."
        )
    if on_read is not None:
        on_read((path.resolve(), stamp))
    return content


def _decode_entries(path: Path, content: bytes | None) -> list[str]:
    """Return the entries in a memory file's *content*; a missing file has none."""
    if content is None:
        return []
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryError(f"failed to read memory file {model_path(path)}: {exc}") from exc
    return _parse_entries(text)


def _parse_entries(text: str) -> list[str]:
    entries = [
        _strip_entry_bullet(line)
        for line in text.splitlines()
        if line.lstrip().startswith(_BULLET_PREFIX)
    ]
    return [entry for entry in entries if entry]


def _strip_entry_bullet(line: str) -> str:
    # Entries are normalized to a single line and written behind a "- " prefix, so a
    # leading-dash entry round-trips by stripping the prefix exactly once. No "\-"
    # unescaping: nothing writes it, and replacing it would corrupt literal "\-" content.
    return line.lstrip().removeprefix(_BULLET_PREFIX).strip()


def _write_entries(path: Path, entries: list[str]) -> None:
    # LF on every platform: the file is vBot-owned data, and reading accepts any line ending.
    text = _render_entries_file(entries)
    try:
        atomic_write_bytes(path, text.encode("utf-8"))
    except OSError as exc:
        raise MemoryError(
            f"failed to write memory file {model_path(path)}: {_os_error_text(exc)}"
        ) from exc


def _os_error_text(error: OSError) -> str:
    """Return the OS reason without the path, which the error spells natively."""
    return error.strerror or str(error)


def _write_scopes(
    paths: dict[MemoryScope, Path],
    entries: dict[MemoryScope, list[str]],
    originals: dict[MemoryScope, bytes | None],
) -> None:
    """Write the *entries* of every scope in *paths*, all of them or none.

    When a write fails, each scope already written gets its *originals* content
    back (a file that did not exist is removed again) and the failure is raised.
    When a restore fails too, :class:`MemoryRevertIncompleteError` names the
    scopes left with their new entries.
    """
    written: list[MemoryScope] = []
    try:
        for scope, path in paths.items():
            _write_entries(path, entries[scope])
            written.append(scope)
    except Exception as failure:
        unrestored: list[MemoryScope] = []
        for scope in written:
            try:
                _restore_file(paths[scope], originals[scope])
            except MemoryError:
                unrestored.append(scope)
        if unrestored:
            raise MemoryRevertIncompleteError(unrestored, failure) from failure
        raise


def _restore_file(path: Path, content: bytes | None) -> None:
    """Put back a memory file's earlier *content*, or remove a file that did not exist."""
    try:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(path, content)
    except OSError as exc:
        raise MemoryError(f"failed to restore memory file {path}: {exc}") from exc


def _render_entries_file(entries: list[str]) -> str:
    """Render the on-disk memory file: one ``- `` bullet per entry, nothing else.

    An empty entry list renders an empty file — the last ``remove`` leaves the file
    present but empty, which reads back as no entries.
    """
    if not entries:
        return ""
    lines = [f"{_BULLET_PREFIX}{_escape_entry_content(entry)}" for entry in entries]
    return "\n".join(lines) + "\n"


def _escape_entry_content(content: str) -> str:
    return content.replace("\n", " ").replace("\r", " ").strip()


def _normalize_entry_content(content: object) -> str:
    if not isinstance(content, str):
        raise MemoryError("content must be a string")
    normalized = _WHITESPACE_PATTERN.sub(" ", content).strip()
    if not normalized:
        raise MemoryError("content must be a non-empty string")
    if len(normalized) > _MAX_ENTRY_LENGTH:
        raise MemoryError(f"content must be at most {_MAX_ENTRY_LENGTH} characters")
    return normalized


def _log_mutation(
    event: str,
    scope: MemoryScope,
    before: list[str],
    after: list[str],
    writer: MemoryWriter,
) -> None:
    """Log one changed Memory file: counts and sizes only, never entry text."""
    if before == after:
        return
    _LOGGER.info(
        "Memory entry %s (agent=%s project=%s scope=%s actor=%s entries=%d chars_delta=%+d)",
        event,
        writer.agent_id or "unknown",
        writer.project_id or "none",
        scope,
        writer.actor,
        len(after),
        _total(after) - _total(before),
    )


def _log_history_failure(event: str, error: Exception) -> None:
    """Log a history failure that a Memory change went ahead without."""
    if isinstance(error, MemoryError):
        _LOGGER.warning("Memory history %s: %s", event, error)
    else:
        _LOGGER.error("Memory history %s", event, exc_info=error)


def _total(entries: Sequence[str]) -> int:
    return sum(len(entry) for entry in entries)


def _fit_budget(entries: Sequence[str], budget: int) -> tuple[list[str], list[str]]:
    """Split *entries* into those that fit *budget* in file order and the rest.

    An entry that does not fit is skipped and later, shorter ones still fit, so one
    oversized entry cannot hide the others.
    """
    shown: list[str] = []
    omitted: list[str] = []
    used = 0
    for entry in entries:
        if used + len(entry) <= budget:
            shown.append(entry)
            used += len(entry)
        else:
            omitted.append(entry)
    return shown, omitted


def _format_scope(
    scope: MemoryScope, used: int, entries: Sequence[str], *, placeholder: bool = True
) -> str:
    """Return a scope's label with its usage, then *entries* as ``- `` bullets.

    With no entries, *placeholder* adds the empty-scope text below the label.
    """
    # Chat's quoting imports prompt state, which itself imports Memory.
    from core.chat.wire_shaping import quote_external_value

    label = f"{MEMORY_SCOPE_LABELS[scope]} ({used}/{_MAX_SCOPE_BUDGET[scope]} chars used)"
    if not entries:
        return f"{label}\n{_EMPTY_SCOPE_TEXT}" if placeholder else label
    body = "\n".join(f"{_BULLET_PREFIX}{quote_external_value(entry)}" for entry in entries)
    return f"{label}\n{body}"


def _require_revisions(
    revisions: Sequence[MemoryRevision], revision_ids: Sequence[int]
) -> list[MemoryRevision]:
    by_id = {revision.id: revision for revision in revisions}
    missing = [revision_id for revision_id in revision_ids if revision_id not in by_id]
    if missing:
        last = revisions[-1].id if revisions else 0
        known = f"revisions run from 1 to {last}" if last else "the history is empty"
        raise MemoryError(f"revision {missing[0]} does not exist; {known}")
    return [by_id[revision_id] for revision_id in dict.fromkeys(revision_ids)]


def _enforce_scope_budget(scope: MemoryScope, entries: list[str], previous_total: int) -> None:
    """Reject a mutation that pushes a scope's tool-managed entries past its budget.

    A non-increasing change (shrink or remove) is always allowed so the model can
    always dig out of an over-budget state — e.g. after the budget is lowered or
    pre-existing entries already exceed it.
    """
    budget = _MAX_SCOPE_BUDGET[scope]
    total = sum(len(entry) for entry in entries)
    if total > budget and total > previous_total:
        raise MemoryBudgetError(scope, total, budget)


def _entry_index(entry_id: int, entries: list[str]) -> int:
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise MemoryError("entry_id must be an integer")
    if entry_id < 1 or entry_id > len(entries):
        raise MemoryError(f"entry_id must be between 1 and {len(entries)}")
    return entry_id - 1


def match_memory_entries(entries: Sequence[str], old_text: str) -> list[int]:
    """Return the indexes of the entries ``old_text`` identifies.

    ``old_text`` is part of one entry's text as the Agent saw it. Whitespace runs
    and a copied leading ``- `` bullet are ignored. An exact whole-entry match wins,
    then exact containment. Only when neither exists are letter case, typographic
    quotes and dashes, and Unicode compatibility forms ignored, again whole entry
    first. More than one index means the text is ambiguous; none means no match.
    """
    locator = _WHITESPACE_PATTERN.sub(" ", old_text).strip()
    locator = locator.removeprefix(_BULLET_PREFIX).strip()
    if not locator:
        raise MemoryError("old_text must contain part of an entry's text")
    for key in (_exact_entry_key, _loose_entry_key):
        needle = key(locator)
        whole = [index for index, entry in enumerate(entries) if key(entry) == needle]
        if whole:
            identical = all(entries[index] == entries[whole[0]] for index in whole)
            return whole[:1] if identical else whole
        partial = [index for index, entry in enumerate(entries) if needle in key(entry)]
        if partial:
            return partial
    # A copied complete bullet uses the same lossless encoding as prompt/list
    # rendering. Prefer the existing literal matches above when both could apply.
    from core.chat.wire_shaping import quote_external_value

    quoted_matches = [
        index for index, entry in enumerate(entries) if quote_external_value(entry) == locator
    ]
    return quoted_matches[:1]


def _exact_entry_key(text: str) -> str:
    return text


def _loose_entry_key(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHIC_EQUIVALENTS).casefold()
    return _WHITESPACE_PATTERN.sub(" ", folded).strip()


def _single_match(scope: MemoryScope, entries: list[str], old_text: str) -> int:
    matches = match_memory_entries(entries, old_text)
    if len(matches) != 1:
        raise MemoryMatchError(
            scope,
            old_text,
            matches=[entries[index] for index in matches],
            entries=entries,
        )
    return matches[0]


def _memory_entries(scope: MemoryScope, entries: list[str]) -> list[MemoryEntry]:
    return [
        MemoryEntry(id=index, scope=scope, content=entry) for index, entry in enumerate(entries, 1)
    ]


_TYPOGRAPHIC_EQUIVALENTS = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "″": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)
