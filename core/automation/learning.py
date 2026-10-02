"""The Memory and Skill changes one Run made, and their undo.

A Run changes an Identity Agent's pinned Memory through the ``memory`` Tool and
the Agent's own Skills through ``skill_manage``. Both owners record every change
in their history together with the id of the Run that made it, so the changes of
one Run are derived from those two histories and nothing else is stored.
Background learning (Reflection reviews now, the Librarian later) shows them to
the user, and an undo takes back all of them or none.

Only the Agent's own histories are read: its Memory and its private Skill home,
the only places a background Run writes. A Memory change is the Run's net
effect on one entry, so an entry the Run added and removed again is no change.
A change counts as undone while a ``revert`` revision that took it back is
itself not undone.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from core.memory import (
    MemoryError,
    MemoryRevertError,
    MemoryRevertIncompleteError,
    MemoryRevision,
    MemoryScope,
    MemoryService,
    MemoryWriter,
)
from core.settings import is_valid_agent_id
from core.skills import (
    HUMAN_WRITER,
    SkillAuthoringError,
    SkillAuthoringService,
    SkillRevertConflictError,
    SkillRevertIncompleteError,
    SkillRevision,
)
from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("automation.learning")

LearningStore = Literal["memory", "skill"]
# A Memory change shows at most this much of its entry text.
PREVIEW_CHARACTERS = 160
_SKILL_DOCUMENT = "SKILL.md"
_MEMORY_SCOPE_NAMES: dict[str, str] = {"user": "User Memory", "agent": "Agent Memory"}


@dataclass(frozen=True)
class LearningChange:
    """One change a Run made, as the user sees it.

    A Memory change is the Run's net effect on one entry: ``kind`` is
    ``added``, ``replaced`` or ``removed``, ``scope`` names the Memory and
    ``text`` holds the entry's (new) text, cut to :data:`PREVIEW_CHARACTERS`.
    An entry the Run added and removed again, or changed back, is no change. A
    Skill change sums up what
    the Run did to one Skill: ``kind`` is ``created``, ``changed`` (its
    ``SKILL.md``), ``archived`` (``absorbed_into`` names the Skill that took
    over its instructions), ``file_written`` or ``file_removed``; ``files`` lists
    the changed package files. ``revisions`` are the history revisions behind
    the change; ``undone`` says they were taken back.
    """

    store: LearningStore
    kind: str
    revisions: tuple[int, ...]
    undone: bool
    scope: MemoryScope | None = None
    text: str | None = None
    skill: str | None = None
    files: tuple[str, ...] = ()
    absorbed_into: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "store": self.store,
            "kind": self.kind,
            "revisions": list(self.revisions),
            "undone": self.undone,
        }
        if self.store == "memory":
            data["scope"] = self.scope
            data["text"] = self.text
        else:
            data["skill"] = self.skill
            data["files"] = list(self.files)
            if self.absorbed_into is not None:
                data["absorbed_into"] = self.absorbed_into
        return data


@dataclass(frozen=True)
class LearningSummary:
    """Counts of one Run's changes: Memory entries, distinct Skills, all undone."""

    memory: int
    skills: int
    undone: bool

    def to_dict(self) -> dict[str, Any]:
        return {"memory": self.memory, "skills": self.skills, "undone": self.undone}


@dataclass(frozen=True)
class RunLearningChanges:
    """Every Memory and Skill change of one Run, Memory first, in history order."""

    agent_id: str
    run_id: str
    changes: tuple[LearningChange, ...]

    @property
    def summary(self) -> LearningSummary:
        return LearningSummary(
            memory=sum(1 for change in self.changes if change.store == "memory"),
            skills=sum(1 for change in self.changes if change.store == "skill"),
            undone=bool(self.changes) and all(change.undone for change in self.changes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "summary": self.summary.to_dict(),
            "changes": [change.to_dict() for change in self.changes],
        }


@dataclass(frozen=True)
class LearningUndoResult:
    """A finished undo: the Run's changes afterwards and which stores it wrote."""

    changes: RunLearningChanges
    memory_changed: bool
    skills_changed: bool


@dataclass(frozen=True)
class LaterChange:
    """The later revision that blocks an undo: who made it and when.

    ``actor`` uses its store's vocabulary: Memory ``tool``, ``rpc``,
    ``internal`` or ``external``; Skills ``human``, ``agent``, ``reflection``,
    ``librarian`` or ``external``.
    """

    revision: int
    at: str
    actor: str
    run_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"revision": self.revision, "at": self.at, "actor": self.actor}
        if self.run_kind is not None:
            data["run_kind"] = self.run_kind
        return data


class LearningError(VBotError):
    """A Run's changes cannot be read or undone."""


class LearningUndoError(LearningError):
    """The undo was refused before anything changed."""


class LearningUndoConflictError(LearningUndoError):
    """A later change touched what the undo would take back; nothing changed.

    ``store`` and ``revision`` name the Run's revision that cannot be taken
    back; ``scope`` and ``text`` its Memory entry or ``skill`` its Skill.
    ``later`` is the change that came after, when its revision is known.
    """

    def __init__(
        self,
        *,
        store: LearningStore,
        revision: int,
        later: LaterChange | None,
        scope: MemoryScope | None = None,
        text: str | None = None,
        skill: str | None = None,
    ) -> None:
        self.store: LearningStore = store
        self.revision = revision
        self.later = later
        self.scope: MemoryScope | None = scope
        self.text = text
        self.skill = skill
        if store == "memory":
            target = f'the {_MEMORY_SCOPE_NAMES.get(scope or "", "Memory")} entry "{text}"'
        else:
            target = f"Skill '{skill}'"
        when = f" (revision {later.revision}, {later.at})" if later is not None else ""
        super().__init__(
            f"Nothing was undone: {target} was changed again later{when}. Change it directly "
            "instead."
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "store": self.store,
            "revision": self.revision,
            "later": self.later.to_dict() if self.later is not None else None,
        }
        if self.store == "memory":
            data["scope"] = self.scope
            data["text"] = self.text
        else:
            data["skill"] = self.skill
        return data


class LearningRunActiveError(LearningUndoError):
    """The Run is still running and may change more; nothing changed."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(
            "Nothing was undone: the Run is still running. Undo it once it has finished."
        )


class LearningUndoFailedError(LearningError):
    """The undo failed while writing; the message says what changed."""


class _Revision(Protocol):
    """What both histories' revisions share."""

    @property
    def id(self) -> int: ...

    @property
    def kind(self) -> str: ...

    @property
    def run_id(self) -> str | None: ...

    @property
    def reverts(self) -> tuple[int, ...]: ...


@dataclass(frozen=True)
class _Histories:
    """Both histories of one Agent as read at one moment."""

    memory: list[MemoryRevision]
    skills: list[SkillRevision]

    def changes(self, agent_id: str, run_id: str) -> RunLearningChanges:
        memory_undone = _undone(self.memory)
        skill_undone = _undone(self.skills)
        changes = [
            LearningChange(
                store="memory",
                kind=entry.kind,
                revisions=tuple(entry.revisions),
                undone=all(revision in memory_undone for revision in entry.revisions),
                scope=entry.scope,
                text=_preview(entry.shown),
            )
            for entry in _net_memory_changes(self.memory, run_id)
        ]
        for skill, revisions in self._skill_revisions(run_id).items():
            changes.append(_skill_change(skill, revisions, skill_undone))
        return RunLearningChanges(agent_id, run_id, tuple(changes))

    def pending(self, run_id: str) -> tuple[list[int], list[int]]:
        """Return the Run's Memory and Skill revisions not undone yet, oldest first.

        Memory revisions whose changes cancel out within the Run have nothing to
        take back and are left out.
        """
        memory_undone = _undone(self.memory)
        skill_undone = _undone(self.skills)
        memory = {
            revision
            for entry in _net_memory_changes(self.memory, run_id)
            for revision in entry.revisions
            if revision not in memory_undone
        }
        skills = [
            revision.id
            for revisions in self._skill_revisions(run_id).values()
            for revision in revisions
            if revision.id not in skill_undone
        ]
        return sorted(memory), sorted(skills)

    def _skill_revisions(self, run_id: str) -> dict[str, list[SkillRevision]]:
        by_skill: dict[str, list[SkillRevision]] = {}
        for revision in self.skills:
            if revision.run_id == run_id and revision.kind != "revert":
                by_skill.setdefault(revision.skill, []).append(revision)
        return by_skill


class LearningChanges:
    """Read and undo the Memory and Skill changes of a Run.

    Reads are side-effect free and touch only the two history files. An undo
    reverts the Run's revisions that are not undone yet in both stores, all or
    none: both stores are checked before either is written, and a later change
    by anyone to a Memory entry or Skill part the Run changed refuses the whole
    undo, even when that change already removed the Run's effect. Reverts of
    the Run's own revisions (an earlier undo and its restore) never block. The
    undo is recorded as ``revert`` revisions by a person.
    """

    def __init__(
        self,
        *,
        memory: MemoryService,
        skills: SkillAuthoringService,
        skill_home: Callable[[str], Path],
        run_active: Callable[[str], bool],
    ) -> None:
        self._memory = memory
        self._skills = skills
        self._skill_home = skill_home
        self._run_active = run_active
        self._undo_lock = threading.Lock()

    def of_run(self, agent_id: str, run_id: str) -> RunLearningChanges:
        """Return every change the Run made to the Agent's Memory and own Skills."""
        _check_ids(agent_id, run_id)
        return self._read(agent_id).changes(agent_id, run_id)

    def summaries(self, agent_id: str, run_ids: Iterable[str]) -> dict[str, LearningSummary]:
        """Return the change counts of several Runs of one Agent, reading each history once."""
        wanted = list(dict.fromkeys(run_ids))
        _check_ids(agent_id, *wanted)
        if not wanted:
            return {}
        histories = self._read(agent_id)
        return {run_id: histories.changes(agent_id, run_id).summary for run_id in wanted}

    def undo(
        self, agent_id: str, run_id: str, *, workspace: Path, actor: str = "internal"
    ) -> LearningUndoResult:
        """Take back every change of the Run that is not undone yet, all of them or none.

        *workspace* is the Agent's Workspace, which holds its Memory files;
        *actor* names the person's surface in the Memory history (``rpc`` from
        an RPC). Raises :class:`LearningRunActiveError` while the Run still
        runs, :class:`LearningUndoConflictError` when a later change built on
        one of the Run's changes and :class:`LearningUndoError` for any other
        refusal, all before writing. A failure while writing takes back what
        was already written where it can and raises
        :class:`LearningUndoFailedError`, whose message says what changed.
        """
        _check_ids(agent_id, run_id)
        home = self._skill_home(agent_id)
        writer = MemoryWriter(agent_id=agent_id, actor=actor)
        with self._undo_lock:
            if self._run_active(run_id):
                raise LearningRunActiveError(run_id)
            histories = self._read(agent_id)
            memory_ids, skill_ids = histories.pending(run_id)
            if not memory_ids and not skill_ids:
                return LearningUndoResult(histories.changes(agent_id, run_id), False, False)
            memory_related = _lineage(histories.memory, run_id)
            skill_related = _lineage(histories.skills, run_id)
            with self._refusals(agent_id, home):
                if memory_ids:
                    self._memory.check_revert(
                        workspace, memory_ids, writer=writer, strict=True, related=memory_related
                    )
                if skill_ids:
                    self._skills.check_revert(
                        home, skill_ids, writer=HUMAN_WRITER, related=skill_related
                    )
            skill_reverts: list[SkillRevision] = []
            if skill_ids:
                with self._refusals(agent_id, home):
                    skill_reverts = self._skills.revert(
                        home, skill_ids, writer=HUMAN_WRITER, related=skill_related
                    )
                if len(skill_reverts) < len(skill_ids):
                    _LOGGER.error(
                        "Learning undo changed Skills the history did not record "
                        "(agent=%s run=%s recorded=%d of %d)",
                        agent_id,
                        run_id,
                        len(skill_reverts),
                        len(skill_ids),
                    )
            memory_changed = False
            if memory_ids:
                try:
                    reverted = self._memory.revert(
                        workspace, memory_ids, writer=writer, strict=True, related=memory_related
                    )
                except Exception as error:
                    failure = self._fail_after_skills(
                        agent_id, run_id, home, len(skill_ids), skill_reverts, error
                    )
                    raise failure from error
                memory_changed = bool(reverted.changed)
            _LOGGER.info(
                "Learning changes undone (agent=%s run=%s memory_revisions=%d "
                "skill_revisions=%d actor=%s)",
                agent_id,
                run_id,
                len(memory_ids),
                len(skill_ids),
                actor,
            )
            after = self._read(agent_id).changes(agent_id, run_id)
            # Every Skill step ran once the revert returned, recorded or not.
            return LearningUndoResult(after, memory_changed, bool(skill_ids))

    def _read(self, agent_id: str) -> _Histories:
        try:
            memory = self._memory.recorded_revisions(agent_id)
            skills = self._skills.recorded_revisions(self._skill_home(agent_id))
        except (MemoryError, SkillAuthoringError) as error:
            raise LearningError(str(error)) from error
        return _Histories(memory, skills)

    @contextmanager
    def _refusals(self, agent_id: str, home: Path) -> Iterator[None]:
        """Turn a refused revert into the undo's refusal, naming the blocking change.

        A Skill revert that could not take its own steps back changed packages,
        so it is a failure, not a refusal.
        """
        try:
            yield
        except MemoryRevertError as error:
            raise self._memory_conflict(agent_id, error) from error
        except SkillRevertConflictError as error:
            raise self._skill_conflict(home, error) from error
        except SkillRevertIncompleteError as error:
            raise LearningUndoFailedError(
                f"The undo failed part-way. {error} The Memory changes were not undone."
            ) from error
        except (MemoryError, SkillAuthoringError) as error:
            raise LearningUndoError(f"Nothing was undone: {error}") from error

    def _memory_conflict(self, agent_id: str, error: MemoryRevertError) -> LearningUndoError:
        conflict = error.conflicts[0]
        try:
            revisions = {
                revision.id: revision for revision in self._memory.recorded_revisions(agent_id)
            }
        except MemoryError:
            revisions = {}
        target = revisions.get(conflict.revision)
        later = revisions.get(conflict.later[0]) if conflict.later else None
        return LearningUndoConflictError(
            store="memory",
            revision=conflict.revision,
            later=_later(later),
            scope=target.scope if target is not None else None,
            text=_preview(conflict.text),
        )

    def _skill_conflict(self, home: Path, error: SkillRevertConflictError) -> LearningUndoError:
        try:
            revisions = self._skills.recorded_revisions(home)
        except SkillAuthoringError:
            revisions = []
        later = next((revision for revision in revisions if revision.id == error.later), None)
        return LearningUndoConflictError(
            store="skill",
            revision=error.revision,
            later=_later(later) or LaterChange(error.later, "", "unknown"),
            skill=error.skill_name,
        )

    def _fail_after_skills(
        self,
        agent_id: str,
        run_id: str,
        home: Path,
        skill_count: int,
        skill_reverts: Sequence[SkillRevision],
        failure: Exception,
    ) -> LearningError:
        """Take the Skill reverts back after the Memory revert failed; describe the outcome.

        A Memory revert refused by a change that came after the check yields
        the undo's conflict once the Skills are back. A Skill step the history
        did not record cannot be taken back and stays undone.
        """
        restore_failed = False
        if skill_reverts:
            try:
                self._skills.revert(
                    home, [revision.id for revision in skill_reverts], writer=HUMAN_WRITER
                )
            except Exception:
                restore_failed = True
                _LOGGER.error(
                    "Learning undo left Skills undone without Memory (agent=%s run=%s)",
                    agent_id,
                    run_id,
                    exc_info=True,
                )
        if restore_failed or not skill_reverts:
            skills_left = skill_count
        else:
            skills_left = skill_count - len(skill_reverts)
        if skill_count and not skills_left:
            _LOGGER.warning(
                "Learning undo failed; Skills restored (agent=%s run=%s)", agent_id, run_id
            )
        incomplete = failure if isinstance(failure, MemoryRevertIncompleteError) else None
        if incomplete is None and not skills_left:
            if isinstance(failure, MemoryRevertError):
                return self._memory_conflict(agent_id, failure)
            return LearningUndoFailedError(f"The undo failed and nothing was changed: {failure}")
        if incomplete is not None:
            names = " and ".join(_MEMORY_SCOPE_NAMES[scope] for scope in incomplete.changed)
            memory_part = (
                f"the {names} changes were undone, the other Memory changes were not "
                f"({incomplete.failure})."
            )
        else:
            memory_part = f"the Memory changes were not undone ({failure})."
        if not skill_count:
            skill_part = ""
        elif not skills_left:
            skill_part = " The Skill changes were not undone."
        elif skills_left < skill_count:
            skill_part = " Some Skill changes were undone."
        else:
            skill_part = " The Skill changes were undone."
        return LearningUndoFailedError(
            f"The undo stopped part-way: {memory_part}{skill_part} Undo again to finish."
        )


def _check_ids(agent_id: str, *run_ids: str) -> None:
    if not is_valid_agent_id(agent_id):
        raise LearningError(f"'{agent_id}' is not an Agent id")
    if any(not isinstance(run_id, str) or not run_id for run_id in run_ids):
        raise LearningError("A Run id must be a non-empty string")


def _later(revision: MemoryRevision | SkillRevision | None) -> LaterChange | None:
    if revision is None:
        return None
    return LaterChange(revision.id, revision.at, revision.actor, revision.run_kind)


def _undone(revisions: Sequence[_Revision]) -> set[int]:
    """Return the ids of revisions a ``revert`` took back that is itself not undone.

    A revert always comes after what it takes back, so walking newest first
    decides every revert before the revisions it names.
    """
    reverted_by: dict[int, list[int]] = {}
    for revision in revisions:
        for target in revision.reverts:
            reverted_by.setdefault(target, []).append(revision.id)
    undone: set[int] = set()
    for revision in sorted(revisions, key=lambda item: item.id, reverse=True):
        if any(reverter not in undone for reverter in reverted_by.get(revision.id, ())):
            undone.add(revision.id)
    return undone


def _lineage(revisions: Sequence[_Revision], run_id: str) -> set[int]:
    """Return the Run's revisions and every revert chain that started from one of them.

    An earlier undo (or the restore of a failed one) left those reverts; they
    belong to the Run's own change and never block undoing it again.
    """
    lineage = {revision.id for revision in revisions if revision.run_id == run_id}
    for revision in revisions:
        if any(target in lineage for target in revision.reverts):
            lineage.add(revision.id)
    return lineage


@dataclass
class _NetEntry:
    """One entry as the Run left it: its text before the Run and after it.

    ``original`` is ``None`` for an entry the Run added and ``text`` is
    ``None`` for one it removed; ``revisions`` are the Run's revisions that
    changed the entry.
    """

    scope: MemoryScope
    original: str | None
    text: str | None
    revisions: list[int]

    @property
    def kind(self) -> str:
        """``added``, ``removed`` or ``replaced``; empty when it ends where it started."""
        if self.original is None:
            return "added" if self.text is not None else ""
        if self.text is None:
            return "removed"
        return "replaced" if self.text != self.original else ""

    @property
    def shown(self) -> str:
        return self.text if self.text is not None else self.original or ""


def _net_memory_changes(revisions: Sequence[MemoryRevision], run_id: str) -> list[_NetEntry]:
    """Return the entries the Run's Memory edits changed for good, in the order it touched them.

    Each edit continues the entry it matches by text: a removal or replacement
    the entry that reads its old text, an addition an entry the Run removed
    with that same text. Entries that end where they started are left out.
    """
    entries: list[_NetEntry] = []
    for revision in revisions:
        if revision.run_id != run_id or revision.kind != "edit":
            continue
        for change in revision.changes:
            if change.op == "added":
                before, after = None, change.text
            elif change.op == "removed":
                before, after = change.text, None
            else:
                before, after = change.previous or change.text, change.text
            entry = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.scope == revision.scope
                    and (
                        entry.text == before
                        if before is not None
                        else entry.text is None and entry.original == after
                    )
                ),
                None,
            )
            if entry is None:
                entries.append(_NetEntry(revision.scope, before, after, [revision.id]))
                continue
            entry.text = after
            if revision.id not in entry.revisions:
                entry.revisions.append(revision.id)
    return [entry for entry in entries if entry.kind]


def _skill_change(
    skill: str, revisions: Sequence[SkillRevision], undone: set[int]
) -> LearningChange:
    files = tuple(dict.fromkeys(record.path for revision in revisions for record in revision.files))
    last = revisions[-1]
    absorbed_into = None
    if last.live is False:
        kind = "archived"
        absorbed_into = last.absorbed_into
        files = ()
    elif any(revision.kind == "create" for revision in revisions):
        kind = "created"
    elif _SKILL_DOCUMENT in files:
        kind = "changed"
    elif any(record.change != "deleted" for revision in revisions for record in revision.files):
        kind = "file_written"
    else:
        kind = "file_removed"
    return LearningChange(
        store="skill",
        kind=kind,
        revisions=tuple(revision.id for revision in revisions),
        undone=all(revision.id in undone for revision in revisions),
        skill=skill,
        files=files,
        absorbed_into=absorbed_into,
    )


def _preview(text: str) -> str:
    if len(text) <= PREVIEW_CHARACTERS:
        return text
    return text[: PREVIEW_CHARACTERS - 1].rstrip() + "…"
