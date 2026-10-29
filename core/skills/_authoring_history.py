"""Skill revision reads, pins, background protection and coordinated revert."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.skills._authoring_documents import _validate_skill_name
from core.skills._authoring_types import (
    SkillAuthoringError,
    SkillProtectedError,
    SkillProtection,
    SkillRevertConflictError,
    SkillRevertIncompleteError,
    SkillWriter,
    SkillWriteResult,
)
from core.skills._history import (
    SKILL_ACTORS,
    SkillHistory,
    SkillRecord,
    SkillRevision,
    SkillRevisionKind,
)
from core.skills._packages import is_redirect
from core.skills._revert import (
    RevertActor,
    RevertConflictError,
    RevertError,
    RevertIncompleteError,
    check_revert,
    revert_revisions,
)
from core.skills.skills import SKILL_FILENAME
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.skills.authoring import SkillAuthoringService


class SkillRevisions:
    def __init__(self, owner: SkillAuthoringService) -> None:
        self._owner = owner

    def set_pinned(
        self, target_root: Path, skill_name: str, pinned: bool, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Pin or unpin a Skill; background writers never change a pinned Skill.

        Only a person pins. The pin lives in the Skill history, so a history that
        cannot be written fails the call. Setting the current state records nothing.
        """
        with self._owner._write_lock:
            _check_writer(writer)
            if writer.actor != "human":
                raise SkillAuthoringError("Only a person can pin or unpin a Skill.")
            skill_dir = self._owner._existing_skill_dir(target_root, skill_name)
            history = SkillHistory(skill_dir.parent)
            revision: int | None = None
            try:
                record = history.observe(skill_name, skill_dir)
                if record is None or record.pinned != pinned:
                    revision = history.append(
                        skill=skill_name,
                        kind="pin" if pinned else "unpin",
                        actor=writer.actor,
                        session_id=writer.session_id,
                        run_id=writer.run_id,
                        run_kind=writer.run_kind,
                        subject_created_at=writer.subject_created_at,
                        pinned=pinned,
                    ).id
            except OSError as error:
                raise SkillAuthoringError(
                    f"The Skill history cannot be written: {error}"
                ) from error
            return SkillWriteResult(
                name=skill_name,
                operation="pin" if pinned else "unpin",
                path=skill_dir,
                revision=revision,
            )

    def records(self, target_root: Path) -> dict[str, SkillRecord]:
        """Return origin, pin and change records of every Skill in a writable home.

        A Skill the history does not know yet gets its ``baseline`` revision. An
        unreadable history yields no records.
        """
        with self._owner._write_lock:
            root = self._owner._resolve(target_root)
            history = SkillHistory(root)
            records: dict[str, SkillRecord] = {}
            try:
                known = history.records()
                for package in _live_packages(root):
                    record = known.get(package.name) or history.observe(package.name, package)
                    if record is not None:
                        records[package.name] = record
            except OSError as error:
                _LOGGER.warning("Skill history of %s cannot be read: %s", root, error)
                return {}
            return records

    def background_protection(self, target_root: Path) -> dict[str, SkillProtection]:
        """Return why background writers may not change each protected Skill of a home.

        A pinned Skill is ``pinned``; Skills a background writer may change are
        absent. When the history cannot be read, every Skill of the home is
        ``unknown``, as a write would be.
        """
        records = self.records(target_root)
        with self._owner._write_lock:
            names = [package.name for package in _live_packages(self._owner._resolve(target_root))]
        protection: dict[str, SkillProtection] = {}
        for name in names:
            record = records.get(name)
            if record is None:
                protection[name] = "unknown"
            elif record.pinned:
                protection[name] = "pinned"
        return protection

    def record(self, target_root: Path, skill_name: str) -> SkillRecord | None:
        """Return the record of one Skill in a writable home, if it exists there."""
        with self._owner._write_lock:
            root = self._owner._resolve(target_root)
            package = _live_package(root, skill_name)
            if package is None:
                return None
            history = SkillHistory(root)
            try:
                return history.record(skill_name) or history.observe(skill_name, package)
            except OSError as error:
                _LOGGER.warning("Skill history of %s cannot be read: %s", root, error)
                return None

    def history(
        self, target_root: Path, skill_name: str | None = None, *, limit: int = 50
    ) -> list[SkillRevision]:
        """Return a home's revisions, or one Skill's, newest first.

        Changes made outside vBot since the last write are recorded first.
        """
        with self._owner._write_lock:
            root = self._owner._resolve(target_root)
            history = SkillHistory(root)
            if skill_name is not None:
                _validate_skill_name(skill_name)
            try:
                if skill_name is not None:
                    names = {skill_name}
                else:
                    names = {package.name for package in _live_packages(root)}
                    names |= set(history.records())
                for name in sorted(names):
                    history.observe(name, _live_package(root, name))
                revisions = history.revisions(skill_name)
            except OSError as error:
                raise SkillAuthoringError(f"The Skill history cannot be read: {error}") from error
            return list(reversed(revisions))[: max(limit, 0)]

    def recorded_revisions(self, target_root: Path) -> list[SkillRevision]:
        """Return the revisions a home's history holds, oldest first.

        Unlike :meth:`history`, this reads only the history: outside changes of
        the packages not noticed yet are not recorded first.
        """
        history = SkillHistory(self._owner._resolve(target_root))
        try:
            return history.revisions()
        except OSError as error:
            raise SkillAuthoringError(f"The Skill history cannot be read: {error}") from error

    def check_revert(
        self,
        target_root: Path,
        revision_ids: Sequence[int],
        *,
        writer: SkillWriter,
        related: Collection[int] = (),
    ) -> None:
        """Check that :meth:`revert` would succeed now; change no package.

        Raises what the revert raises before changing anything. Outside changes
        of the named Skills are recorded first, as every history read does.
        """
        with self._owner._write_lock:
            history = self._revert_history(target_root, writer)
            with _revert_errors():
                check_revert(history, history.home, revision_ids, related=related)

    def revert(
        self,
        target_root: Path,
        revision_ids: Sequence[int],
        *,
        writer: SkillWriter,
        related: Collection[int] = (),
    ) -> list[SkillRevision]:
        """Undo the named revisions, all or none; return the recorded revisions.

        Refused when a later revision outside the request changed the same file,
        pin or presence of that Skill (``SkillRevertConflictError`` names it), when
        an earlier text is not stored, or when an archived package was purged. A
        later revision in *related* (an earlier revert of the same change) never
        blocks. A failure while changing packages takes the steps back and
        raises :class:`SkillAuthoringError`, or :class:`SkillRevertIncompleteError`
        when a step cannot be taken back. Once every package changed, each named
        revision records one ``revert`` revision; the list is shorter when the
        history cannot record them all.
        """
        with self._owner._write_lock:
            history = self._revert_history(target_root, writer)
            with _revert_errors():
                return revert_revisions(
                    history,
                    history.home,
                    revision_ids,
                    RevertActor(
                        writer.actor,
                        writer.session_id,
                        writer.run_id,
                        writer.run_kind,
                        writer.subject_created_at,
                    ),
                    related=related,
                )

    def _revert_history(self, target_root: Path, writer: SkillWriter) -> SkillHistory:
        _check_writer(writer)
        if writer.background:
            raise SkillAuthoringError("Background writers cannot revert Skill revisions.")
        return SkillHistory(self._owner._writable_root(target_root))

    def _observe(
        self,
        history: SkillHistory,
        skill_name: str,
        skill_dir: Path | None,
        writer: SkillWriter,
    ) -> None:
        """Record outside changes before a write; enforce the background rules.

        A history failure only logs, except for a background writer changing an
        existing Skill: it cannot know whether the user pinned the Skill, and the
        change could not be recorded for undo, so it is refused.
        """
        try:
            record = history.observe(skill_name, skill_dir)
            pinned = record is not None and record.pinned
            if writer.background and skill_dir is not None and skill_dir.name != skill_name:
                # A hand-edited frontmatter name still denotes the pinned package
                # whose history the manager addresses by its folder name.
                folder_record = history.record(skill_dir.name)
                pinned = pinned or (folder_record is not None and folder_record.pinned)
        except Exception as error:
            if writer.background and skill_dir is not None:
                raise SkillProtectedError(skill_name, "unknown") from error
            _LOGGER.warning("Skill history of '%s' not updated: %s", skill_name, error)
            return
        if writer.background and pinned:
            raise SkillProtectedError(skill_name, "pinned")

    def _commit(
        self,
        history: SkillHistory,
        skill_name: str,
        skill_dir: Path | None,
        writer: SkillWriter,
        kind: SkillRevisionKind,
        *,
        paths: Iterable[str] | None = None,
        **fields: Any,
    ) -> int | None:
        """Record one revision of a completed write; a failure only logs."""
        try:
            files, texts = history.changes(skill_name, skill_dir, paths)
            if kind == "change" and not files:
                return None
            return history.append(
                skill=skill_name,
                kind=kind,
                actor=writer.actor,
                files=files,
                texts=texts,
                session_id=writer.session_id,
                run_id=writer.run_id,
                run_kind=writer.run_kind,
                subject_created_at=writer.subject_created_at,
                **fields,
            ).id
        except Exception as error:
            _LOGGER.warning("Skill history of '%s' not recorded: %s", skill_name, error)
            return None


@contextmanager
def _revert_errors() -> Iterator[None]:
    """Report a refused or failed revert as the authoring service's errors."""
    try:
        yield
    except RevertConflictError as conflict:
        raise SkillRevertConflictError(
            str(conflict),
            revision=conflict.revision,
            later=conflict.later,
            skill_name=conflict.skill,
        ) from conflict
    except RevertIncompleteError as incomplete:
        raise SkillRevertIncompleteError(
            str(incomplete), skill_names=incomplete.skills
        ) from incomplete
    except RevertError as error:
        raise SkillAuthoringError(str(error)) from error
    except OSError as error:
        raise SkillAuthoringError(f"The Skill history cannot be read: {error}") from error


def _check_writer(writer: SkillWriter) -> None:
    if not isinstance(writer, SkillWriter) or writer.actor not in SKILL_ACTORS:
        raise SkillAuthoringError(f"Unknown Skill writer: {writer!r}")


def _live_package(root: Path, skill_name: str) -> Path | None:
    """Return the package of *skill_name* in *root* when it is a loadable Skill directory."""
    try:
        _validate_skill_name(skill_name)
    except SkillAuthoringError:
        return None
    package = root / skill_name
    try:
        if is_redirect(package) or not package.is_dir():
            return None
        document = package / SKILL_FILENAME
        if is_redirect(document) or not document.is_file():
            return None
    except OSError:
        return None
    return package


def _live_packages(root: Path) -> list[Path]:
    try:
        names = sorted(entry.name for entry in root.iterdir())
    except OSError:
        return []
    return [package for name in names if (package := _live_package(root, name)) is not None]


_LOGGER = get_logger("skills.authoring")
