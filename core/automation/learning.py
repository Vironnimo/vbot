"""The Memory and Skill changes one Run made, and their undo.

A Run changes personal and Project Memory through the ``memory`` Tool and
own or shared Skills through ``skill_manage``. Both owners record every change
in their history together with the id of the Run that made it, so the changes of
one Run are derived from those two histories and nothing else is stored.
Background learning (Reflection reviews now, the Librarian later) shows them to
the user, and an undo takes back all of them or none.

The Agent's Memory, registered Agents' Skill histories and shared Project Memory
histories are read. A Memory change is the Run's net effect on one entry, so an
entry the Run added and removed again is no change.
A change counts as undone while a ``revert`` revision that took it back is
itself not undone.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from core.memory import (
    MemoryError,
    MemoryRevertError,
    MemoryRevertIncompleteError,
    MemoryRevertResult,
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
    SkillReference,
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
_MEMORY_SCOPE_NAMES: dict[str, str] = {
    "user": "User Memory",
    "agent": "Agent Memory",
    "project": "Project Memory",
}


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
    over its instructions, ``followed`` the planned reference moves),
    ``file_written`` or ``file_removed``; ``files`` lists the changed package
    files. ``revisions`` are the history revisions behind the change; ``undone``
    says they were taken back. Undoing a merge restores the Skill but moves
    nothing in ``followed`` back.
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
    followed: tuple[SkillReference, ...] = ()
    project_id: str | None = None
    skill_owner_id: str | None = None

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
            if self.project_id is not None:
                data["project_id"] = self.project_id
        else:
            data["skill"] = self.skill
            data["files"] = list(self.files)
            if self.skill_owner_id is not None:
                data["skill_owner_id"] = self.skill_owner_id
            if self.absorbed_into is not None:
                data["absorbed_into"] = self.absorbed_into
            if self.followed:
                data["followed"] = [reference.to_dict() for reference in self.followed]
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
        project_id: str | None = None,
        skill_owner_id: str | None = None,
    ) -> None:
        self.store: LearningStore = store
        self.revision = revision
        self.later = later
        self.scope: MemoryScope | None = scope
        self.text = text
        self.skill = skill
        self.project_id = project_id
        self.skill_owner_id = skill_owner_id
        if store == "memory":
            target = f'the {_MEMORY_SCOPE_NAMES.get(scope or "", "Memory")} entry "{text}"'
        else:
            target = f"Skill '{skill}'"
            if skill_owner_id is not None:
                target += f" of Agent '{skill_owner_id}'"
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
            if self.project_id is not None:
                data["project_id"] = self.project_id
        else:
            data["skill"] = self.skill
            if self.skill_owner_id is not None:
                data["skill_owner_id"] = self.skill_owner_id
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
    """Personal and shared histories read under the Agent and Project guards."""

    memory: list[MemoryRevision]
    skills: Mapping[str, tuple[Path, list[SkillRevision]]]
    projects: Mapping[str, tuple[Path, list[MemoryRevision]]]

    def changes(self, agent_id: str, run_id: str) -> RunLearningChanges:
        memory_undone = _undone(self.memory)
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
        for project_id, (_, memory_revisions) in self.projects.items():
            undone = _undone(memory_revisions)
            changes.extend(
                LearningChange(
                    store="memory",
                    kind=entry.kind,
                    revisions=tuple(entry.revisions),
                    undone=all(revision in undone for revision in entry.revisions),
                    scope=entry.scope,
                    text=_preview(entry.shown),
                    project_id=project_id,
                )
                for entry in _net_memory_changes(memory_revisions, run_id, agent_id=agent_id)
            )
        for owner_id, (_, history) in self.skills.items():
            undone = _undone(history)
            for skill, revisions in _skill_revisions(history, run_id).items():
                changes.append(
                    replace(
                        _skill_change(skill, revisions, undone),
                        skill_owner_id=owner_id if owner_id != agent_id else None,
                    )
                )
        return RunLearningChanges(agent_id, run_id, tuple(changes))

    def pending_memory(self, run_id: str) -> list[int]:
        """Return the Run's personal Memory revisions not undone yet, oldest first.

        Memory revisions whose changes cancel out within the Run have nothing to
        take back and are left out.
        """
        memory_undone = _undone(self.memory)
        memory = {
            revision
            for entry in _net_memory_changes(self.memory, run_id)
            for revision in entry.revisions
            if revision not in memory_undone
        }
        return sorted(memory)

    def memory_undos(self, agent_id: str, run_id: str, workspace: Path) -> list[_MemoryUndo]:
        personal = self.pending_memory(run_id)
        plans = (
            [_MemoryUndo(None, workspace, personal, _lineage(self.memory, run_id))]
            if personal
            else []
        )
        for project_id, (anchor, revisions) in self.projects.items():
            undone = _undone(revisions)
            pending = sorted(
                {
                    revision
                    for entry in _net_memory_changes(revisions, run_id, agent_id=agent_id)
                    for revision in entry.revisions
                    if revision not in undone
                }
            )
            if pending:
                plans.append(
                    _MemoryUndo(
                        project_id,
                        anchor,
                        pending,
                        _lineage(revisions, run_id, agent_id=agent_id),
                    )
                )
        return plans

    def skill_undos(self, run_id: str, extra: Mapping[str, Sequence[int]]) -> list[_SkillUndo]:
        missing = set(extra).difference(self.skills)
        if missing:
            owners = ", ".join(sorted(missing))
            raise LearningUndoError(
                f"Nothing was undone: the Skill owners are unavailable: {owners}."
            )
        plans = []
        for owner_id, (home, history) in self.skills.items():
            undone = _undone(history)
            named = set(extra.get(owner_id, ())) | {
                revision.id
                for revisions in _skill_revisions(history, run_id).values()
                for revision in revisions
            }
            pending = sorted(named - undone)
            if pending:
                plans.append(
                    _SkillUndo(
                        owner_id, home, pending, _lineage(history, run_id, revision_ids=named)
                    )
                )
        return plans


def _skill_revisions(
    history: Sequence[SkillRevision], run_id: str
) -> dict[str, list[SkillRevision]]:
    by_skill: dict[str, list[SkillRevision]] = {}
    for revision in history:
        if revision.run_id == run_id and revision.kind != "revert":
            by_skill.setdefault(revision.skill, []).append(revision)
    return by_skill


@dataclass(frozen=True)
class _SkillUndo:
    owner_id: str
    home: Path
    revisions: list[int]
    related: set[int]


@dataclass(frozen=True)
class _MemoryUndo:
    project_id: str | None
    workspace: Path
    revisions: list[int]
    related: set[int]

    def writer(self, agent_id: str, actor: str) -> MemoryWriter:
        return MemoryWriter(agent_id=agent_id, actor=actor, project_id=self.project_id)


class LearningChanges:
    """Read and undo the Memory and Skill changes of a Run.

    Reads are side-effect free and touch only the histories. An undo
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
        project_memories: Callable[[], Mapping[str, Path]] | None = None,
        project_guard: Callable[[], AbstractContextManager[Any]] | None = None,
        skill_homes: Callable[[], Mapping[str, Path]] | None = None,
        agent_guard: Callable[[], AbstractContextManager[Any]] | None = None,
        subject_created_at: Callable[[str], str | None] | None = None,
    ) -> None:
        self._memory = memory
        self._skills = skills
        self._skill_home = skill_home
        self._run_active = run_active
        self._project_memories = project_memories or (lambda: {})
        self._project_guard = project_guard or nullcontext
        self._skill_homes = skill_homes or (lambda: {})
        self._agent_guard = agent_guard or nullcontext
        self._subject_created_at = subject_created_at
        self._undo_lock = threading.Lock()

    def of_run(self, agent_id: str, run_id: str) -> RunLearningChanges:
        """Return the Run's personal/shared Memory and Skill changes."""
        _check_ids(agent_id, run_id)
        with self._agent_guard(), self._project_guard():
            histories = self._read(agent_id)
            self._check_skill_subject(histories, agent_id, run_id)
            return histories.changes(agent_id, run_id)

    def summaries(self, agent_id: str, run_ids: Iterable[str]) -> dict[str, LearningSummary]:
        """Return the change counts of several Runs of one Agent, reading each history once."""
        wanted = list(dict.fromkeys(run_ids))
        _check_ids(agent_id, *wanted)
        if not wanted:
            return {}
        with self._agent_guard(), self._project_guard():
            histories = self._read(agent_id)
            for run_id in wanted:
                self._check_skill_subject(histories, agent_id, run_id)
            return {run_id: histories.changes(agent_id, run_id).summary for run_id in wanted}

    def undo(
        self,
        agent_id: str,
        run_id: str,
        *,
        workspace: Path,
        actor: str = "internal",
        skill_revisions: Mapping[str, Sequence[int]] | None = None,
        validate: Callable[[], None] | None = None,
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

        Internal callers can include explicit Skill revisions per owner, such as
        a Librarian pass's aging archives. They share the Run's preflight and
        compensation, but do not become changes attributed to that Run.
        ``validate`` rechecks an internal caller's admission while the lifecycle
        guards are held, before reading histories or changing either store.
        """
        _check_ids(agent_id, run_id)
        with (
            self._undo_lock,
            self._agent_guard(),
            self._project_guard(),
            self._skills.exclusive(),
        ):
            if validate is not None:
                validate()
            home = self._skill_home(agent_id)
            if self._run_active(run_id):
                raise LearningRunActiveError(run_id)
            histories = self._read(agent_id)
            self._check_skill_subject(histories, agent_id, run_id, validated=validate is not None)
            skill_plans = histories.skill_undos(run_id, skill_revisions or {})
            memory_plans = histories.memory_undos(agent_id, run_id, workspace)
            if not memory_plans and not skill_plans:
                return LearningUndoResult(histories.changes(agent_id, run_id), False, False)
            for plan in memory_plans:
                with self._refusals(agent_id, home, project_id=plan.project_id):
                    self._memory.check_revert(
                        plan.workspace,
                        plan.revisions,
                        writer=plan.writer(agent_id, actor),
                        strict=True,
                        related=plan.related,
                    )
            for skill_plan in skill_plans:
                with self._refusals(agent_id, skill_plan.home, skill_owner_id=skill_plan.owner_id):
                    self._skills.check_revert(
                        skill_plan.home,
                        skill_plan.revisions,
                        writer=HUMAN_WRITER,
                        related=skill_plan.related,
                    )
            applied_skills: list[tuple[_SkillUndo, list[SkillRevision]]] = []
            for skill_plan in skill_plans:
                try:
                    with self._refusals(
                        agent_id, skill_plan.home, skill_owner_id=skill_plan.owner_id
                    ):
                        skill_reverts = self._skills.revert(
                            skill_plan.home,
                            skill_plan.revisions,
                            writer=HUMAN_WRITER,
                            related=skill_plan.related,
                        )
                except Exception as error:
                    if self._restore_skills(agent_id, run_id, applied_skills):
                        raise LearningUndoFailedError(
                            "The undo stopped part-way; some Skill changes could not be "
                            "restored. Inspect the remaining Run changes and undo again to finish."
                        ) from error
                    raise
                applied_skills.append((skill_plan, skill_reverts))
                if len(skill_reverts) < len(skill_plan.revisions):
                    _LOGGER.error(
                        "Learning undo changed Skills the history did not record "
                        "(agent=%s owner=%s run=%s recorded=%d of %d)",
                        agent_id,
                        skill_plan.owner_id,
                        run_id,
                        len(skill_reverts),
                        len(skill_plan.revisions),
                    )
            memory_changed = False
            applied: list[tuple[_MemoryUndo, MemoryRevertResult]] = []
            for plan in memory_plans:
                try:
                    reverted = self._memory.revert(
                        plan.workspace,
                        plan.revisions,
                        writer=plan.writer(agent_id, actor),
                        strict=True,
                        related=plan.related,
                    )
                except Exception as error:
                    restored = self._restore_memory(agent_id, actor, applied)
                    failure = self._fail_after_skills(
                        agent_id,
                        run_id,
                        applied_skills,
                        error,
                        project_id=plan.project_id,
                    )
                    if not restored:
                        raise LearningUndoFailedError(
                            "The undo stopped part-way; some Memory changes could not be "
                            "restored. Inspect the remaining Run changes and undo again to finish."
                        ) from error
                    raise failure from error
                applied.append((plan, reverted))
                memory_changed = memory_changed or bool(reverted.changed)
            _LOGGER.info(
                "Learning changes undone (agent=%s run=%s memory_revisions=%d "
                "skill_revisions=%d actor=%s)",
                agent_id,
                run_id,
                sum(len(plan.revisions) for plan in memory_plans),
                sum(len(plan.revisions) for plan in skill_plans),
                actor,
            )
            after = self._read(agent_id).changes(agent_id, run_id)
            # Every Skill step ran once the revert returned, recorded or not.
            return LearningUndoResult(after, memory_changed, bool(skill_plans))

    def _check_skill_subject(
        self,
        histories: _Histories,
        agent_id: str,
        run_id: str,
        *,
        validated: bool = False,
    ) -> None:
        """Require evidence before a Run reaches another Agent's Skill history.

        Own-home legacy revisions keep their existing behavior. Foreign legacy
        revisions need a stamped revision of the same globally unique Run, a
        personal Memory write, or an internally validated Librarian pass.
        """
        if self._subject_created_at is None:
            return
        identity = self._subject_created_at(agent_id)
        if identity is None:
            raise LearningError(f"Agent '{agent_id}' is unavailable.")
        revisions = [
            (owner_id, revision)
            for owner_id, (_, history) in histories.skills.items()
            for revision in history
            if revision.run_id == run_id and revision.kind != "revert"
        ]
        stamped = {
            revision.subject_created_at
            for _, revision in revisions
            if revision.subject_created_at is not None
        }
        if stamped and stamped != {identity}:
            raise LearningError(f"Run '{run_id}' does not belong to this Agent '{agent_id}'.")
        if (
            stamped
            or validated
            or any(
                revision.run_id == run_id and revision.kind == "edit"
                for revision in histories.memory
            )
        ):
            return
        if any(owner_id != agent_id for owner_id, _ in revisions):
            raise LearningError(
                "This Run changed shared Skills before its writing Agent was recorded. "
                "Those changes cannot be assigned safely. Inspect and revert them in each "
                "owner's Skill history instead."
            )

    def _read(self, agent_id: str) -> _Histories:
        try:
            memory = self._memory.recorded_revisions(agent_id)
            homes = {agent_id: self._skill_home(agent_id)}
            homes.update(
                (owner_id, home)
                for owner_id, home in sorted(self._skill_homes().items())
                if owner_id != agent_id
            )
            skills = {
                owner_id: (home, self._skills.recorded_revisions(home))
                for owner_id, home in homes.items()
            }
            projects = {
                project_id: (path, self._memory.recorded_revisions(project_id=project_id))
                for project_id, path in sorted(self._project_memories().items())
            }
        except VBotError as error:
            raise LearningError(str(error)) from error
        return _Histories(memory, skills, projects)

    def _restore_memory(
        self,
        agent_id: str,
        actor: str,
        applied: Sequence[tuple[_MemoryUndo, MemoryRevertResult]],
    ) -> bool:
        """Compensate completed Memory owners when a later owner's undo fails."""
        restored = True
        for plan, result in reversed(applied):
            if len(result.revisions) < len(result.changed):
                restored = False
            if not result.revisions:
                continue
            try:
                self._memory.revert(
                    plan.workspace,
                    [revision.id for revision in result.revisions],
                    writer=plan.writer(agent_id, actor),
                )
            except Exception:
                restored = False
                _LOGGER.error(
                    "Learning undo could not restore Memory (agent=%s project=%s)",
                    agent_id,
                    plan.project_id,
                )
        return restored

    @contextmanager
    def _refusals(
        self,
        agent_id: str,
        home: Path,
        *,
        project_id: str | None = None,
        skill_owner_id: str | None = None,
    ) -> Iterator[None]:
        """Turn a refused revert into the undo's refusal, naming the blocking change.

        A Skill revert that could not take its own steps back changed packages,
        so it is a failure, not a refusal.
        """
        try:
            yield
        except MemoryRevertError as error:
            raise self._memory_conflict(agent_id, error, project_id=project_id) from error
        except SkillRevertConflictError as error:
            raise self._skill_conflict(
                home, error, owner_id=skill_owner_id if skill_owner_id != agent_id else None
            ) from error
        except SkillRevertIncompleteError as error:
            raise LearningUndoFailedError(
                f"The undo failed part-way. {error} The Memory changes were not undone."
            ) from error
        except (MemoryError, SkillAuthoringError) as error:
            raise LearningUndoError(f"Nothing was undone: {error}") from error

    def _memory_conflict(
        self, agent_id: str, error: MemoryRevertError, *, project_id: str | None = None
    ) -> LearningUndoError:
        conflict = error.conflicts[0]
        try:
            revisions = {
                revision.id: revision
                for revision in self._memory.recorded_revisions(
                    None if project_id is not None else agent_id, project_id=project_id
                )
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
            project_id=project_id,
        )

    def _skill_conflict(
        self, home: Path, error: SkillRevertConflictError, *, owner_id: str | None = None
    ) -> LearningUndoError:
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
            skill_owner_id=owner_id,
        )

    def _restore_skills(
        self,
        agent_id: str,
        run_id: str,
        applied: Sequence[tuple[_SkillUndo, Sequence[SkillRevision]]],
    ) -> int:
        """Compensate completed Skill homes, returning the number left undone."""
        left = 0
        for plan, revisions in reversed(applied):
            restored = False
            if revisions:
                try:
                    self._skills.revert(
                        plan.home, [revision.id for revision in revisions], writer=HUMAN_WRITER
                    )
                    restored = True
                except Exception:
                    _LOGGER.error(
                        "Learning undo could not restore Skills (agent=%s owner=%s run=%s)",
                        agent_id,
                        plan.owner_id,
                        run_id,
                        exc_info=True,
                    )
            left += len(plan.revisions) - (len(revisions) if restored else 0)
        return left

    def _fail_after_skills(
        self,
        agent_id: str,
        run_id: str,
        applied: Sequence[tuple[_SkillUndo, Sequence[SkillRevision]]],
        failure: Exception,
        *,
        project_id: str | None = None,
    ) -> LearningError:
        """Take the Skill reverts back after the Memory revert failed; describe the outcome.

        A Memory revert refused by a change that came after the check yields
        the undo's conflict once the Skills are back. A Skill step the history
        did not record cannot be taken back and stays undone.
        """
        skill_count = sum(len(plan.revisions) for plan, _ in applied)
        skills_left = self._restore_skills(agent_id, run_id, applied)
        if skill_count and not skills_left:
            _LOGGER.warning(
                "Learning undo failed; Skills restored (agent=%s run=%s)", agent_id, run_id
            )
        incomplete = failure if isinstance(failure, MemoryRevertIncompleteError) else None
        if incomplete is None and not skills_left:
            if isinstance(failure, MemoryRevertError):
                return self._memory_conflict(agent_id, failure, project_id=project_id)
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


def _lineage(
    revisions: Sequence[_Revision],
    run_id: str,
    *,
    agent_id: str | None = None,
    revision_ids: Iterable[int] = (),
) -> set[int]:
    """Return the Run's revisions and every revert chain that started from one of them.

    An earlier undo (or the restore of a failed one) left those reverts; they
    belong to the Run's own change and never block undoing it again.
    """
    lineage = set(revision_ids) | {
        revision.id
        for revision in revisions
        if revision.run_id == run_id
        and (agent_id is None or getattr(revision, "agent_id", None) == agent_id)
    }
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


def _net_memory_changes(
    revisions: Sequence[MemoryRevision], run_id: str, *, agent_id: str | None = None
) -> list[_NetEntry]:
    """Return the entries the Run's Memory edits changed for good, in the order it touched them.

    Each edit continues the entry it matches by text: a removal or replacement
    the entry that reads its old text, an addition an entry the Run removed
    with that same text. Entries that end where they started are left out.
    """
    entries: list[_NetEntry] = []
    for revision in revisions:
        if (
            revision.run_id != run_id
            or revision.kind != "edit"
            or (agent_id is not None and revision.agent_id != agent_id)
        ):
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
    followed: tuple[SkillReference, ...] = ()
    if last.live is False:
        kind = "archived"
        absorbed_into = last.absorbed_into
        followed = last.followed
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
        followed=followed,
    )


def _preview(text: str) -> str:
    if len(text) <= PREVIEW_CHARACTERS:
        return text
    return text[: PREVIEW_CHARACTERS - 1].rstrip() + "…"
