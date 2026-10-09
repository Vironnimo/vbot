"""Revert of recorded Skill revisions inside the Skill authoring owner.

A revert is all or none: every named revision is checked and its inverse planned
before anything changes, the inverses run newest first, and a failure undoes the
steps already taken. A revision cannot be reverted while a later revision outside
the request changed the same part of that Skill: one of its files, its pin, or
its presence in the home. Revisions the caller names as related (such as earlier
reverts of the same change) never block. Each reverted revision records one
``revert`` revision.

A ``rename`` concerns both of its names: its revert gives the Skill its former
name back, with the ``SKILL.md`` text from before, and records the ``revert``
under that name with ``renamed_from``. A revision recorded under the former name
before the rename conflicts with the rename, so it reverts together with it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Literal

from core.skills._history import (
    SKILL_DOCUMENT,
    FileState,
    SkillFileRecord,
    SkillHistory,
    SkillRevision,
    declared_origin,
    diff_states,
    file_state,
    package_state,
)
from core.skills._packages import excluded, is_redirect
from core.utils.atomic import atomic_write_bytes
from core.utils.ids import new_id
from core.utils.logging import get_logger

_LOGGER = get_logger("skills.history")


class RevertError(ValueError):
    """A revert that cannot run; nothing was changed."""


class RevertConflictError(RevertError):
    """A later revision changed the same part of the Skill."""

    def __init__(self, revision: int, later: int, skill: str) -> None:
        super().__init__(
            f"Revision {revision} cannot be reverted: revision {later} later changed the "
            f"same part of Skill '{skill}'. Revert revision {later} together with it."
        )
        self.revision = revision
        self.later = later
        self.skill = skill


class RevertIncompleteError(RevertError):
    """A revert failed while changing packages and could not undo every step.

    ``skills`` names the Skills whose packages may be left part-way.
    """

    def __init__(self, message: str, skills: Sequence[str]) -> None:
        super().__init__(message)
        self.skills = tuple(skills)


@dataclass(frozen=True)
class RevertActor:
    actor: str
    session_id: str | None = None
    run_id: str | None = None
    run_kind: str | None = None


@dataclass
class _Step:
    target: SkillRevision
    action: Literal["archive", "restore", "content", "pin", "rename"]
    archive_id: str | None = None
    reason: str | None = None
    absorbed_into: str | None = None
    writes: dict[str, str | None] = field(default_factory=dict)
    pinned: bool | None = None
    # Filled while the step runs, recorded once every step succeeded.
    records: list[SkillFileRecord] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)
    origin: str | None = None
    created_at: str | None = None

    @property
    def skill(self) -> str:
        return self.target.skill

    @property
    def renamed_to(self) -> str | None:
        """For a rename step, the name the Skill gets back."""
        return self.target.renamed_from if self.action == "rename" else None

    @property
    def recorded_skill(self) -> str:
        """The Skill name the step's ``revert`` revision is recorded under."""
        return self.renamed_to or self.skill


def check_revert(
    history: SkillHistory,
    root: Path,
    revision_ids: Sequence[int],
    *,
    related: Collection[int] = (),
) -> None:
    """Check that :func:`revert_revisions` would run now; change no package.

    Raises what the revert raises before changing anything. Outside changes of
    the named Skills are recorded first. The caller holds the authoring write lock.
    """
    _plan(history, root, revision_ids, related)


def revert_revisions(
    history: SkillHistory,
    root: Path,
    revision_ids: Sequence[int],
    actor: RevertActor,
    *,
    related: Collection[int] = (),
) -> list[SkillRevision]:
    """Revert *revision_ids* in the home *root*; return the recorded revisions.

    A later revision in *related* does not block the revert. Each named
    revision records one ``revert`` revision once every package changed; when
    the history cannot record them all, the packages changed all the same and
    the list is shorter. The caller holds the authoring write lock.
    """
    steps = _plan(history, root, revision_ids, related)
    _execute(root, history.archive_root, steps)
    recorded: list[SkillRevision] = []
    for step in steps:
        try:
            recorded.append(
                history.append(
                    skill=step.recorded_skill,
                    kind="revert",
                    actor=actor.actor,
                    files=step.records,
                    texts=step.texts,
                    session_id=actor.session_id,
                    run_id=actor.run_id,
                    run_kind=actor.run_kind,
                    origin=step.origin,
                    created_at=step.created_at,
                    pinned=step.pinned,
                    live=_live_after(step),
                    reason=step.reason if step.action == "archive" else None,
                    absorbed_into=step.absorbed_into if step.action == "archive" else None,
                    archive_id=step.archive_id,
                    reverts=(step.target.id,),
                    renamed_from=step.skill if step.renamed_to is not None else None,
                )
            )
        except OSError as error:
            _LOGGER.warning("Skill revert of %s not recorded: %s", root, error)
            break
    return recorded


def _plan(
    history: SkillHistory,
    root: Path,
    revision_ids: Sequence[int],
    related: Collection[int],
) -> list[_Step]:
    """Check the named revisions and plan their inverses, newest first."""
    if not revision_ids:
        raise RevertError("Name at least one revision to revert.")
    targets: list[SkillRevision] = []
    for revision_id in sorted(set(revision_ids), reverse=True):
        revision = history.revision(revision_id)
        if revision is None:
            raise RevertError(f"Revision {revision_id} does not exist in this Skill home.")
        targets.append(revision)
    for skill in {name for target in targets for name in target.names}:
        _skill_path(root, skill)
        history.observe(skill, live_package(root, skill))
    _check_conflicts(history, targets, related)
    steps = [_inverse(history, root, target) for target in targets]
    _simulate(root, history.archive_root, steps)
    return steps


def live_package(root: Path, skill: str) -> Path | None:
    """Return the live package directory of *skill* in *root*, if any."""
    package = root / skill
    try:
        if is_redirect(package) or not package.is_dir():
            return None
        document = package / SKILL_DOCUMENT
        if is_redirect(document) or not document.is_file():
            return None
    except OSError:
        return None
    return package


def _live_after(step: _Step) -> bool | None:
    if step.action == "archive":
        return False
    if step.action == "restore":
        return True
    return None


def _overlaps(revision: SkillRevision, later: SkillRevision) -> bool:
    if revision.moves or later.moves:
        return True
    if revision.pinned is not None and later.pinned is not None:
        return True
    return bool({item.path for item in revision.files} & {item.path for item in later.files})


def _check_conflicts(
    history: SkillHistory, targets: list[SkillRevision], related: Collection[int]
) -> None:
    requested = {target.id for target in targets} | set(related)
    revisions = history.revisions()
    for target in targets:
        if target.kind == "baseline":
            raise RevertError(
                f"Revision {target.id} is the first recorded state of Skill "
                f"'{target.skill}'; there is nothing earlier to return to."
            )
        for later in revisions:
            if (
                later.id > target.id
                and later.id not in requested
                and target.names & later.names
                and _overlaps(target, later)
            ):
                raise RevertConflictError(target.id, later.id, later.skill)


def _inverse(history: SkillHistory, root: Path, target: SkillRevision) -> _Step:
    if target.live is False:
        if target.archive_id is None:
            raise RevertError(
                f"Revision {target.id} cannot be reverted: Skill '{target.skill}' was "
                "removed outside vBot, so no archived package exists."
            )
        info = history.archive(target.archive_id)
        if info is None:
            return _Step(target, "restore", archive_id=target.archive_id)
        return _Step(
            target,
            "restore",
            archive_id=target.archive_id,
            pinned=info.pinned,
            origin=info.origin,
            created_at=info.created_at,
        )
    if target.live is True:
        if target.archive_id is None:
            raise RevertError(f"Revision {target.id} cannot be reverted.")
        info = history.archive(target.archive_id)
        return _Step(
            target,
            "archive",
            archive_id=target.archive_id,
            reason=info.reason if info is not None and info.reason else "deleted",
            absorbed_into=info.absorbed_into if info is not None else None,
        )
    if target.renamed_from is not None:
        # The Skill gets its former name back, with the files the rename changed.
        _skill_path(root, target.renamed_from)
        return _Step(target, "rename", writes=_earlier_texts(history, root, target))
    if target.kind == "create":
        return _Step(target, "archive", reason="deleted")
    if target.pinned is not None and not target.files:
        return _Step(target, "pin", pinned=not target.pinned)
    writes = _earlier_texts(history, root, target)
    if not writes:
        raise RevertError(f"Revision {target.id} changed nothing that can be reverted.")
    return _Step(target, "content", writes=writes)


def _earlier_texts(
    history: SkillHistory, root: Path, target: SkillRevision
) -> dict[str, str | None]:
    """The text each file *target* changed had before it; ``None`` for a created file."""
    writes: dict[str, str | None] = {}
    for record in target.files:
        _package_path(root / target.skill, record.path, target.id)
        if record.before is None:
            writes[record.path] = None
            continue
        text = history.text(target.skill, record.path, record.before)
        if text is None:
            raise RevertError(
                f"Revision {target.id} cannot be reverted: the history holds no earlier "
                f"text of {record.path} (it is not UTF-8 text or is larger than 1 MiB)."
            )
        writes[record.path] = text
    return writes


def _simulate(root: Path, archive_root: Path, steps: list[_Step]) -> None:
    """Check every step against the state the earlier steps leave."""
    names = {name for step in steps for name in (step.skill, step.renamed_to) if name}
    live = {name: live_package(root, name) is not None for name in names}
    occupied = {name: (root / name).exists() for name in names}
    archived: dict[str, bool] = {}

    def available(archive_id: str) -> bool:
        if archive_id not in archived:
            package = archive_root / archive_id
            try:
                archived[archive_id] = (
                    not is_redirect(archive_root) and not is_redirect(package) and package.is_dir()
                )
            except OSError:
                archived[archive_id] = False
        return archived[archive_id]

    for step in steps:
        target = step.target
        if step.action == "restore":
            assert step.archive_id is not None
            if not available(step.archive_id):
                raise RevertError(
                    f"Revision {target.id} cannot be reverted: the archived package of "
                    f"Skill '{step.skill}' was permanently deleted."
                )
            if occupied[step.skill]:
                raise RevertError(
                    f"Revision {target.id} cannot be reverted: a Skill named "
                    f"'{step.skill}' exists. Delete or rename it first."
                )
            archived[step.archive_id] = False
            live[step.skill] = occupied[step.skill] = True
            continue
        if not live[step.skill]:
            raise RevertError(
                f"Revision {target.id} cannot be reverted: Skill '{step.skill}' is not "
                "in this home."
            )
        if step.renamed_to is not None:
            if occupied[step.renamed_to]:
                raise RevertError(
                    f"Revision {target.id} cannot be reverted: a Skill named "
                    f"'{step.renamed_to}' exists. Delete or rename it first."
                )
            live[step.skill] = occupied[step.skill] = False
            live[step.renamed_to] = occupied[step.renamed_to] = True
            continue
        if step.action == "archive":
            if step.archive_id is not None and available(step.archive_id):
                raise RevertError(
                    f"Revision {target.id} cannot be reverted: archive {step.archive_id} is in use."
                )
            if step.archive_id is not None:
                archived[step.archive_id] = True
            live[step.skill] = occupied[step.skill] = False


def _execute(root: Path, archive_root: Path, steps: list[_Step]) -> None:
    undo: list[tuple[str, Callable[[], object]]] = []
    try:
        for step in steps:
            if step.action == "archive":
                _archive(root, archive_root, step, undo)
            elif step.action == "restore":
                _restore(root, archive_root, step, undo)
            elif step.action == "content":
                _content(root, step, undo)
            elif step.action == "rename":
                _rename(root, step, undo)
    except OSError as error:
        failed: list[str] = []
        for skill, action in reversed(undo):
            try:
                action()
            except OSError:
                failed.append(skill)
        if failed:
            skills = sorted(set(failed))
            raise RevertIncompleteError(
                f"The revert failed and could not be undone completely ({error}). "
                f"Check Skill {', '.join(skills)}.",
                skills,
            ) from error
        raise RevertError(f"The revert failed and nothing was changed: {error}") from error


def _archive(
    root: Path, archive_root: Path, step: _Step, undo: list[tuple[str, Callable[[], object]]]
) -> None:
    package = root / step.skill
    before = package_state(package)
    archive_root.mkdir(exist_ok=True)
    if is_redirect(archive_root):
        raise OSError("the Skill archive is a link")
    archive_id = step.archive_id or new_id(
        step.skill, claim=lambda candidate: not (archive_root / candidate).exists()
    )
    destination = archive_root / archive_id
    package.rename(destination)
    undo.append((step.skill, lambda: destination.rename(package)))
    step.archive_id = archive_id
    step.records, step.texts = diff_states(_digests(before), {})


def _restore(
    root: Path, archive_root: Path, step: _Step, undo: list[tuple[str, Callable[[], object]]]
) -> None:
    assert step.archive_id is not None
    source = archive_root / step.archive_id
    package = root / step.skill
    source.rename(package)
    undo.append((step.skill, lambda: package.rename(source)))
    if step.origin is None:
        step.origin = declared_origin(package)
        step.pinned = False
    step.records, step.texts = diff_states({}, package_state(package))


def _rename(root: Path, step: _Step, undo: list[tuple[str, Callable[[], object]]]) -> None:
    """Give the Skill its former name back, then the texts the rename changed."""
    assert step.renamed_to is not None
    package, destination = root / step.skill, root / step.renamed_to
    package.rename(destination)
    undo.append((step.skill, lambda: destination.rename(package)))
    _content(root, step, undo, package=destination)


def _content(
    root: Path,
    step: _Step,
    undo: list[tuple[str, Callable[[], object]]],
    *,
    package: Path | None = None,
) -> None:
    """Write the step's texts into *package*, the Skill's package by default."""
    package = package if package is not None else root / step.skill
    before: dict[str, FileState] = {}
    after: dict[str, FileState] = {}
    for path, text in step.writes.items():
        target = _package_path(package, path, step.target.id)
        previous = target.read_bytes() if target.is_file() else None
        state = file_state(target)
        if state is not None:
            before[path] = state
        undo.append((step.skill, partial(_put, target, previous)))
        if text is None:
            target.unlink(missing_ok=True)
            _remove_empty_parents(target.parent, package)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, text.encode("utf-8"))
            written = file_state(target)
            if written is not None:
                after[path] = written
    step.records, step.texts = diff_states(_digests(before), after)


def _put(target: Path, data: bytes | None) -> None:
    if data is None:
        target.unlink(missing_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, data)


def _remove_empty_parents(directory: Path, package: Path) -> None:
    while directory != package and package in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def _digests(states: dict[str, FileState]) -> dict[str, str]:
    return {path: state.digest for path, state in states.items()}


def _skill_path(root: Path, skill: str) -> Path:
    if (
        not skill
        or skill in {".", ".."}
        or any(separator in skill for separator in "/\\\x00")
        or skill != skill.strip()
    ):
        raise RevertError(f"The history names an unsafe Skill name: {skill!r}")
    return root / skill


def _package_path(package: Path, path: str, revision: int) -> Path:
    parts = PurePosixPath(path).parts
    if (
        not parts
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in parts)
        or excluded(path)
    ):
        raise RevertError(f"Revision {revision} names an unsafe file path: {path!r}")
    candidate = package
    for part in parts:
        candidate /= part
        try:
            redirect = is_redirect(candidate)
        except FileNotFoundError:
            redirect = False
        if redirect:
            raise RevertError(f"Revision {revision} names a file behind a link: {path!r}")
    return candidate
