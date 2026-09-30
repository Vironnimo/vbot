"""Append-only change history of an Identity Agent's pinned Memory.

Each Agent has one JSON Lines file, ``<agents_root>/<agent_id>/memory-history.jsonl``,
outside its Workspace: file Tools do not see it, and it moves and archives with
the Agent. Every line is one revision of one scope with a sequential id. A
revision records its changed entries, not the whole file; replaying them gives
any earlier state. Two kinds carry the complete state instead: ``baseline``, the
entries found when a scope's history starts, and ``external``, a change made to
the file outside the Memory service (a file Tool, a shell, an editor), which is
noticed on the next Memory operation and dated by the file's modification time.

Callers hold the scope file's lock; the history file has its own lock, always
taken last.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp, utc_now_timestamp

if TYPE_CHECKING:
    from core.memory.memory import MemoryScope

_LOGGER = get_logger("memory.history")

HISTORY_FILE_NAME = "memory-history.jsonl"
_FORMAT_VERSION = 1

MemoryChangeOp = Literal["added", "removed", "replaced"]
MemoryRevisionKind = Literal["baseline", "edit", "external", "revert"]
_CHANGE_OPS = frozenset({"added", "removed", "replaced"})
_REVISION_KINDS = frozenset({"baseline", "edit", "external", "revert"})
_SCOPES = frozenset({"agent", "user"})


@dataclass(frozen=True)
class MemoryChange:
    """One changed entry: ``text`` is the added, removed or new text.

    ``previous`` is a replaced entry's old text. ``index`` is the entry's
    position: before the change for removed and replaced entries, after it for
    added ones.
    """

    op: MemoryChangeOp
    text: str
    index: int
    previous: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"op": self.op, "text": self.text, "index": self.index}
        if self.previous is not None:
            data["previous"] = self.previous
        return data


@dataclass(frozen=True)
class MemoryRevision:
    """One recorded change of one Memory scope.

    ``actor`` names who changed it (``tool``, ``rpc``, ``internal``, or
    ``external`` for a change outside the service); ``session_id``/``run_id``
    name the Run of a Tool change. ``entries`` holds the complete state of a
    ``baseline`` or ``external`` revision; ``reverts`` the revisions a
    ``revert`` took back.
    """

    id: int
    at: str
    scope: MemoryScope
    kind: MemoryRevisionKind
    actor: str
    state: str
    changes: tuple[MemoryChange, ...] = ()
    entries: tuple[str, ...] | None = None
    session_id: str | None = None
    run_id: str | None = None
    reverts: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "at": self.at,
            "scope": self.scope,
            "kind": self.kind,
            "actor": self.actor,
            "changes": [change.to_dict() for change in self.changes],
        }
        if self.entries is not None:
            data["entries"] = list(self.entries)
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if self.reverts:
            data["reverts"] = list(self.reverts)
        return data


@dataclass
class _Log:
    """One history file as read: its revisions and each scope's current state."""

    signature: tuple[int, int] | None
    revisions: list[MemoryRevision] = field(default_factory=list)
    states: dict[str, list[str]] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)


class MemoryHistory:
    """The Memory histories of the Agents under one agents root."""

    _locks: ClassVar[dict[str, threading.Lock]] = {}
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _logs: ClassVar[dict[str, _Log]] = {}

    def __init__(self, agents_root: Path) -> None:
        self._agents_root = Path(agents_root)

    def available(self, agent_id: str | None) -> bool:
        """Whether *agent_id* names an Agent directory that can hold a history."""
        return self._path(agent_id) is not None

    def sync(self, agent_id: str, scope: MemoryScope, entries: Sequence[str], file: Path) -> None:
        """Record the scope's current entries when the history does not know them yet.

        A scope without history starts with a ``baseline`` of its entries; a
        state that differs from the last recorded one was changed outside the
        service and becomes an ``external`` revision.
        """
        path = self._path(agent_id)
        if path is None:
            return
        with self._lock(path):
            log = self._load(path)
            current = list(entries)
            known = log.states.get(scope)
            if known is None:
                if current:
                    self._append(
                        path,
                        log,
                        scope=scope,
                        kind="baseline",
                        actor="external",
                        at=_modified_at(file),
                        entries=current,
                        changes=diff_entries([], current),
                    )
                return
            if log.hashes[scope] == _state_hash(current):
                return
            self._append(
                path,
                log,
                scope=scope,
                kind="external",
                actor="external",
                at=_modified_at(file),
                entries=current,
                changes=diff_entries(known, current),
            )

    def record(
        self,
        agent_id: str,
        scope: MemoryScope,
        *,
        kind: MemoryRevisionKind,
        actor: str,
        changes: Sequence[MemoryChange],
        entries: Sequence[str],
        session_id: str | None = None,
        run_id: str | None = None,
        reverts: Sequence[int] = (),
    ) -> MemoryRevision | None:
        """Append one revision whose ``changes`` led to ``entries``."""
        path = self._path(agent_id)
        if path is None or not changes:
            return None
        with self._lock(path):
            log = self._load(path)
            return self._append(
                path,
                log,
                scope=scope,
                kind=kind,
                actor=actor,
                at=utc_now_timestamp(),
                changes=list(changes),
                state=list(entries),
                session_id=session_id,
                run_id=run_id,
                reverts=list(reverts),
            )

    def revisions(self, agent_id: str) -> list[MemoryRevision]:
        """Return every revision of the Agent's Memory, oldest first."""
        path = self._path(agent_id)
        if path is None:
            return []
        with self._lock(path):
            return list(self._load(path).revisions)

    def _path(self, agent_id: str | None) -> Path | None:
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or agent_id in {".", ".."}
            or any(separator in agent_id for separator in ("/", "\\", "\0"))
        ):
            return None
        agent_dir = self._agents_root / agent_id
        return agent_dir / HISTORY_FILE_NAME if agent_dir.is_dir() else None

    @classmethod
    def _lock(cls, path: Path) -> threading.Lock:
        key = os.path.normcase(os.path.abspath(path))
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.Lock())

    @classmethod
    def _load(cls, path: Path) -> _Log:
        key = os.path.normcase(os.path.abspath(path))
        signature = _signature(path)
        cached = cls._logs.get(key)
        if cached is not None and cached.signature == signature:
            return cached
        log = _Log(signature=signature)
        if signature is not None:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                from core.memory.memory import MemoryError

                raise MemoryError(f"failed to read Memory history {path}: {exc}") from exc
            skipped = 0
            for line in lines:
                revision = _parse_revision(line)
                if revision is None:
                    skipped += bool(line.strip())
                    continue
                log.revisions.append(revision)
                _advance(log, revision)
            if skipped:
                _LOGGER.warning("Skipped %d unreadable Memory history lines in %s", skipped, path)
        cls._logs[key] = log
        return log

    @classmethod
    def _append(
        cls,
        path: Path,
        log: _Log,
        *,
        scope: MemoryScope,
        kind: MemoryRevisionKind,
        actor: str,
        at: str,
        entries: Sequence[str] | None = None,
        changes: Sequence[MemoryChange] = (),
        state: Sequence[str] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        reverts: Sequence[int] = (),
    ) -> MemoryRevision:
        after = list(entries if entries is not None else state or ())
        revision = MemoryRevision(
            id=(log.revisions[-1].id + 1) if log.revisions else 1,
            at=at,
            scope=scope,
            kind=kind,
            actor=actor,
            state=_state_hash(after),
            changes=tuple(changes),
            entries=tuple(entries) if entries is not None else None,
            session_id=session_id,
            run_id=run_id,
            reverts=tuple(reverts),
        )
        line = json.dumps(
            {"v": _FORMAT_VERSION, **revision.to_dict(), "state": revision.state},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            from core.memory.memory import MemoryError

            raise MemoryError(f"failed to write Memory history {path}: {exc}") from exc
        log.revisions.append(revision)
        log.states[scope] = after
        log.hashes[scope] = revision.state
        log.signature = _signature(path)
        return revision


def state_at(
    revisions: Iterable[MemoryRevision], revision_id: int | None
) -> dict[MemoryScope, list[str]]:
    """Replay *revisions* up to and including *revision_id* (all when ``None``)."""
    log = _Log(signature=None)
    for revision in revisions:
        if revision_id is not None and revision.id > revision_id:
            break
        _advance(log, revision)
    return {scope: list(log.states.get(scope, [])) for scope in ("agent", "user")}


def diff_entries(before: Sequence[str], after: Sequence[str]) -> tuple[MemoryChange, ...]:
    """Describe how the entry list *before* became *after*, entry by entry."""
    changes: list[MemoryChange] = []
    matcher = difflib.SequenceMatcher(a=list(before), b=list(after), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        paired = min(i2 - i1, j2 - j1) if tag == "replace" else 0
        for offset in range(paired):
            changes.append(
                MemoryChange(
                    "replaced", after[j1 + offset], i1 + offset, previous=before[i1 + offset]
                )
            )
        for index in range(i1 + paired, i2):
            changes.append(MemoryChange("removed", before[index], index))
        for index in range(j1 + paired, j2):
            changes.append(MemoryChange("added", after[index], index))
    return tuple(changes)


def apply_changes(entries: list[str], changes: Iterable[MemoryChange]) -> None:
    """Apply recorded *changes* in order to *entries*, in place."""
    for change in changes:
        if change.op == "added":
            entries.insert(min(max(change.index, 0), len(entries)), change.text)
            continue
        target = change.previous if change.op == "replaced" else change.text
        index = _position(entries, target, change.index)
        if index is None:
            continue
        if change.op == "removed":
            entries.pop(index)
        else:
            entries[index] = change.text


@dataclass(frozen=True)
class MemoryRevertConflict:
    """A change that cannot be taken back because a later change built on it.

    ``text`` is the entry text the revert needs but no longer finds; ``later``
    lists the revisions that changed or removed that text since.
    """

    revision: int
    change: MemoryChange
    text: str
    later: tuple[int, ...]


def revert_revisions(
    states: dict[MemoryScope, list[str]],
    targets: Sequence[MemoryRevision],
    revisions: Sequence[MemoryRevision],
) -> tuple[dict[MemoryScope, list[str]], list[MemoryRevertConflict]]:
    """Take the *targets* back from the current *states*, newest first.

    An added entry is removed, a removed entry is restored at its old position
    and a replaced entry gets its previous text back. A change whose effect is
    already gone (its added entry was removed, its removed entry exists again)
    needs nothing. A change that later revisions built on - the entry was
    replaced since, or a replacement was changed or removed - is a conflict;
    *revisions* (the whole history) names those later revisions.
    """
    result = {scope: list(entries) for scope, entries in states.items()}
    conflicts: list[MemoryRevertConflict] = []
    for target in sorted(targets, key=lambda revision: revision.id, reverse=True):
        entries = result.setdefault(target.scope, [])
        for change in reversed(target.changes):
            if change.op == "removed":
                if change.text not in entries:
                    entries.insert(min(max(change.index, 0), len(entries)), change.text)
                continue
            index = _position(entries, change.text, change.index)
            if index is None:
                later = _later_changes(revisions, target, change.text)
                if change.op == "replaced" or later.replaced:
                    conflicts.append(
                        MemoryRevertConflict(target.id, change, change.text, later.ids)
                    )
                continue
            if change.op == "added" or change.previous in entries:
                entries.pop(index)
            else:
                entries[index] = change.previous or change.text
    return result, conflicts


@dataclass(frozen=True)
class _LaterChanges:
    ids: tuple[int, ...]
    replaced: bool


def _later_changes(
    revisions: Sequence[MemoryRevision], target: MemoryRevision, text: str
) -> _LaterChanges:
    ids: list[int] = []
    replaced = False
    for revision in revisions:
        if revision.id <= target.id or revision.scope != target.scope:
            continue
        for change in revision.changes:
            if change.op == "replaced" and change.previous == text:
                replaced = True
            elif not (change.op == "removed" and change.text == text):
                continue
            if revision.id not in ids:
                ids.append(revision.id)
    return _LaterChanges(tuple(ids), replaced)


def _position(entries: Sequence[str], text: str | None, hint: int) -> int | None:
    if 0 <= hint < len(entries) and entries[hint] == text:
        return hint
    try:
        return entries.index(text) if text is not None else None
    except ValueError:
        return None


def _advance(log: _Log, revision: MemoryRevision) -> None:
    if revision.entries is not None:
        state = list(revision.entries)
    else:
        state = list(log.states.get(revision.scope, []))
        apply_changes(state, revision.changes)
    log.states[revision.scope] = state
    log.hashes[revision.scope] = revision.state


def _state_hash(entries: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()[:16]


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _modified_at(file: Path) -> str:
    try:
        moment = datetime.fromtimestamp(file.stat().st_mtime, UTC)
    except OSError:
        return utc_now_timestamp()
    return format_canonical_timestamp(moment)


def _parse_revision(line: str) -> MemoryRevision | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("v") != _FORMAT_VERSION:
        return None
    try:
        changes = tuple(_parse_change(item) for item in data.get("changes", []))
        entries = data.get("entries")
        reverts = data.get("reverts", [])
        revision = MemoryRevision(
            id=_integer(data["id"]),
            at=_text(data["at"]),
            scope=data["scope"],
            kind=data["kind"],
            actor=_text(data["actor"]),
            state=_text(data["state"]),
            changes=changes,
            entries=tuple(_text(item) for item in entries) if entries is not None else None,
            session_id=_optional_text(data.get("session_id")),
            run_id=_optional_text(data.get("run_id")),
            reverts=tuple(_integer(item) for item in reverts),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if revision.scope not in _SCOPES or revision.kind not in _REVISION_KINDS:
        return None
    return revision


def _parse_change(data: Any) -> MemoryChange:
    if not isinstance(data, dict) or data.get("op") not in _CHANGE_OPS:
        raise ValueError("malformed Memory change")
    return MemoryChange(
        op=data["op"],
        text=_text(data["text"]),
        index=_integer(data["index"]),
        previous=_optional_text(data.get("previous")),
    )


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)
