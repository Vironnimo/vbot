"""Skill package archival, restoration, purge and absorbed-instruction lookup."""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from core.skills._authoring_documents import _change_text, _declared_name, _reject_redirect
from core.skills._authoring_history import _check_writer
from core.skills._authoring_types import (
    ArchivedSkill,
    SkillAuthoringError,
    SkillFileChange,
    SkillWriter,
    SkillWriteResult,
)
from core.skills._history import (
    ARCHIVE_REASONS,
    SkillArchiveInfo,
    SkillArchiveReason,
    SkillHistory,
    SkillReference,
    declared_origin,
    document_fields,
)
from core.skills._packages import is_redirect
from core.skills._restore_journal import RestoreIntent, intent_path, read_intent, write_intent
from core.skills._revert_journal import require_revert_idle
from core.skills.skills import SKILL_FILENAME, SkillRegistry
from core.utils.atomic import sync_directory
from core.utils.file_status import exists_strict
from core.utils.ids import has_id_entry, new_id
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp

if TYPE_CHECKING:
    from core.skills.authoring import SkillAuthoringService


class SkillArchives:
    def __init__(self, owner: SkillAuthoringService) -> None:
        self._owner = owner

    def delete(
        self,
        target_root: Path,
        skill_name: str,
        *,
        writer: SkillWriter,
        reason: SkillArchiveReason | None = None,
        absorbed_into: str | None = None,
        followed: Sequence[SkillReference] = (),
    ) -> SkillWriteResult:
        """Move a Skill package into the home's archive.

        ``reason`` is ``deleted`` by default and ``absorbed`` with
        ``absorbed_into``, another Skill in the same home that now holds this
        Skill's instructions; ``inactive`` marks a Skill retired for disuse.
        ``followed`` names what is planned to move to ``absorbed_into`` (its
        shares, the automations that trigger it); the history records it and the
        caller moves it. The reported changes delete every package file; the
        archived package keeps them for ``restore`` until ``purge``.
        """
        with self._owner._write_lock:
            _check_writer(writer)
            skill_dir = self._owner._resolved_skill_dir(target_root, skill_name)
            root = skill_dir.parent
            reason, absorbed_into = self._archive_reason(root, skill_dir, reason, absorbed_into)
            if followed and absorbed_into is None:
                raise SkillAuthoringError("followed is only valid with absorbed_into.")
            history = SkillHistory(root)
            self._owner._revisions._observe(history, skill_name, skill_dir, writer)
            # Path.walk never enters a link, Windows junctions included, so files
            # behind one are neither reported nor read; the move keeps the link.
            entries = (base / name for base, _, names in skill_dir.walk() for name in names)
            files = sorted(
                (path for path in entries if path.is_file() and not path.is_symlink()),
                key=lambda path: (path.name != SKILL_FILENAME or path.parent != skill_dir, path),
            )
            changes = tuple(
                SkillFileChange(
                    path.relative_to(skill_dir).as_posix(), "deleted", _change_text(path), None
                )
                for path in files
            )
            archive_root = self._archive_root(history)
            archive_id = new_id(
                skill_name, claim=lambda candidate: not (archive_root / candidate).exists()
            )
            destination = archive_root / archive_id
            skill_dir.rename(destination)
            revision = self._owner._revisions._commit(
                history,
                skill_name,
                None,
                writer,
                "archive",
                live=False,
                reason=reason,
                absorbed_into=absorbed_into,
                archive_id=archive_id,
                followed=tuple(followed),
            )
            return SkillWriteResult(
                name=skill_name,
                operation="delete",
                path=destination,
                changes=changes,
                revision=revision,
                archive_id=archive_id,
            )

    def archived(self, target_root: Path) -> list[ArchivedSkill]:
        """Return the packages in a home's archive, newest first."""
        with self._owner._write_lock:
            root = self._owner._resolve(target_root)
            history = SkillHistory(root)
            infos = self._archive_infos(history)
            packages = self._archived(history, infos)
            newest = _newest_archives([*packages, *self._purged_absorbed(history, infos)])
            return [self._with_holder(root, newest, entry) for entry in packages]

    def archived_skill(self, target_root: Path, skill_name: str) -> ArchivedSkill | None:
        """Return the newest archive entry of *skill_name* in a home, if any.

        Includes an absorbed Skill whose archived files were purged
        (``available`` false).
        """
        with self._owner._write_lock:
            root = self._owner._resolve(target_root)
            history = SkillHistory(root)
            infos = self._archive_infos(history)
            newest = _newest_archives(
                [*self._archived(history, infos), *self._purged_absorbed(history, infos)]
            )
            entry = newest.get(skill_name)
            return None if entry is None else self._with_holder(root, newest, entry)

    def restore(
        self, target_root: Path, archive_id: str, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Move an archived package back into the home under its Skill name.

        Repeating the same restore finishes an interrupted operation.
        """
        with self._owner._write_lock:
            _check_writer(writer)
            root = self._owner._writable_root(target_root)
            require_revert_idle(root)
            history = SkillHistory(root)
            intent = read_intent(root)
            if intent is not None and intent.archive_id != archive_id:
                raise SkillAuthoringError(
                    f"Retry restoring archive '{intent.archive_id}' before restoring another Skill."
                )
            try:
                if intent is None:
                    intent = self._begin_restore(history, archive_id, writer)
                destination, revision = self._finish_restore(history, intent)
            except OSError as error:
                raise SkillAuthoringError(
                    f"Could not complete the Skill restore. Retry restoring archive "
                    f"'{archive_id}' when storage is available: {error}"
                ) from error
            files = sorted(
                base / file_name
                for base, _, file_names in destination.walk()
                for file_name in file_names
            )
            return SkillWriteResult(
                name=intent.name,
                operation="restore",
                path=destination,
                changes=tuple(
                    SkillFileChange(
                        path.relative_to(destination).as_posix(),
                        "created",
                        None,
                        _change_text(path),
                    )
                    for path in files
                    if path.is_file() and not path.is_symlink()
                ),
                revision=revision,
                archive_id=archive_id,
            )

    def _begin_restore(
        self, history: SkillHistory, archive_id: str, writer: SkillWriter
    ) -> RestoreIntent:
        package = self._archived_package(history, archive_id)
        try:
            info = history.archive(archive_id)
        except OSError as error:
            raise SkillAuthoringError(f"The Skill history cannot be read: {error}") from error
        name = info.skill if info is not None else _declared_name(package)
        destination = self._owner._skill_dir(history.home, name)
        self._check_restore_destination(destination)
        history.observe(name, None)
        status = package.lstat()
        intent = RestoreIntent(
            transaction=uuid4().hex,
            archive_id=archive_id,
            name=name,
            device=status.st_dev,
            inode=status.st_ino,
            origin=info.origin if info is not None else declared_origin(package),
            created_at=info.created_at if info is not None else None,
            pinned=info.pinned if info is not None else False,
            writer=writer,
        )
        write_intent(history.home, intent)
        return intent

    def _finish_restore(self, history: SkillHistory, intent: RestoreIntent) -> tuple[Path, int]:
        package = self._archive_path(history, intent.archive_id)
        destination = self._owner._skill_dir(history.home, intent.name)
        source_exists = exists_strict(package, follow_symlinks=False)
        if source_exists:
            self._check_restore_destination(destination, archive_id=intent.archive_id)
        current = package if source_exists else destination
        if not intent.matches(current):
            raise SkillAuthoringError(
                f"The package for archive '{intent.archive_id}' changed or is missing. "
                "Inspect the archive and the Skill folder before retrying the restore."
            )

        # The intent and the restore revision must be durable before the package
        # becomes visible in the home. A retry flushes a previously written line.
        journal = intent_path(history.home)
        sync_directory(journal.parent)
        files, texts = history.changes(intent.name, current)
        revision = history.append(
            skill=intent.name,
            kind="restore",
            actor=intent.writer.actor,
            session_id=intent.writer.session_id,
            run_id=intent.writer.run_id,
            run_kind=intent.writer.run_kind,
            subject_created_at=intent.writer.subject_created_at,
            files=files,
            texts=texts,
            live=True,
            archive_id=intent.archive_id,
            origin=intent.origin,
            created_at=intent.created_at,
            pinned=intent.pinned,
            restore_transaction=intent.transaction,
        )
        sync_directory(journal.parent)
        if source_exists:
            destination.parent.mkdir(exist_ok=True)
            self._check_restore_destination(destination, archive_id=intent.archive_id)
            if not intent.matches(package):
                raise SkillAuthoringError(
                    f"The package for archive '{intent.archive_id}' changed while restoring it."
                )
            package.rename(destination)
        sync_directory(destination.parent)
        sync_directory(package.parent)
        sync_directory(journal.parent)
        journal.unlink()
        return destination, revision.id

    @staticmethod
    def _check_restore_destination(destination: Path, *, archive_id: str | None = None) -> None:
        name = destination.name
        if exists_strict(destination, follow_symlinks=False) or any(
            skill.name == name for skill in SkillRegistry.load(destination.parent).list_all()
        ):
            if archive_id is not None:
                raise SkillAuthoringError(
                    f"A Skill named '{name}' appeared during the restore. Move the conflicting "
                    f"package out of this Skill home before retrying archive '{archive_id}'."
                )
            raise SkillAuthoringError(
                f"A Skill named '{name}' already exists. Delete or rename it first, "
                "then restore this one."
            )

    def purge(self, target_root: Path, archive_id: str) -> ArchivedSkill:
        """Permanently delete one archived package; return what it was."""
        with self._owner._write_lock:
            root = self._owner._writable_root(target_root)
            require_revert_idle(root)
            history = SkillHistory(root)
            intent = read_intent(root)
            if intent is not None and intent.archive_id == archive_id:
                raise SkillAuthoringError(
                    f"Retry restoring archive '{archive_id}' before purging it."
                )
            package = self._archived_package(history, archive_id)
            entry = _archived_entry(package, self._archive_infos(history).get(archive_id))
            shutil.rmtree(package)
            _LOGGER.info("Purged archived Skill '%s' (%s) from %s", entry.name, archive_id, root)
            return entry

    def _archive_reason(
        self,
        root: Path,
        skill_dir: Path,
        reason: SkillArchiveReason | None,
        absorbed_into: str | None,
    ) -> tuple[SkillArchiveReason, str | None]:
        if absorbed_into is None:
            if reason == "absorbed":
                raise SkillAuthoringError("An absorbed Skill needs the Skill that absorbed it.")
            if reason is not None and reason not in ARCHIVE_REASONS:
                raise SkillAuthoringError(f"Unknown archive reason: {reason!r}")
            return reason or "deleted", None
        if reason not in (None, "absorbed"):
            raise SkillAuthoringError("absorbed_into is only valid for the reason 'absorbed'.")
        target = self._owner._existing_skill_dir(root, absorbed_into)
        if target == skill_dir:
            raise SkillAuthoringError(f"Skill '{skill_dir.name}' cannot absorb itself.")
        return "absorbed", target.name

    def _archive_root(self, history: SkillHistory) -> Path:
        archive_root = history.archive_root
        _reject_redirect(archive_root)
        archive_root.mkdir(exist_ok=True)
        return archive_root

    @staticmethod
    def _archive_path(history: SkillHistory, archive_id: str) -> Path:
        if (
            not isinstance(archive_id, str)
            or not archive_id
            or archive_id in {".", ".."}
            or any(separator in archive_id for separator in "/\\\x00")
        ):
            raise SkillAuthoringError(f"Illegal archive id: {archive_id!r}")
        archive_root = history.archive_root
        _reject_redirect(archive_root)
        package = archive_root / archive_id
        if package.name != archive_id or package.parent != archive_root:
            raise SkillAuthoringError(f"Archived Skill '{archive_id}' not found.")
        return package

    def _archived_package(self, history: SkillHistory, archive_id: str) -> Path:
        package = self._archive_path(history, archive_id)
        archive_root = history.archive_root
        # Only an archive entry spelled exactly like the id names a package. On
        # Windows a drive-relative id ("C:..") joins as the archive's parent, and
        # a case variant opens the real entry under a spelling no history knows.
        if not has_id_entry(archive_root, archive_id):
            raise SkillAuthoringError(f"Archived Skill '{archive_id}' not found.")
        _reject_redirect(package)
        if not package.is_dir():
            raise SkillAuthoringError(f"Archived Skill '{archive_id}' not found.")
        return package

    def _archive_infos(self, history: SkillHistory) -> dict[str, SkillArchiveInfo]:
        try:
            return {info.archive_id: info for info in history.archives()}
        except OSError as error:
            _LOGGER.warning("Skill history of %s cannot be read: %s", history.home, error)
            return {}

    def _archived(
        self, history: SkillHistory, infos: dict[str, SkillArchiveInfo]
    ) -> list[ArchivedSkill]:
        archive_root = history.archive_root
        try:
            if is_redirect(archive_root) or not archive_root.is_dir():
                return []
            packages = [
                package
                for package in archive_root.iterdir()
                if not is_redirect(package) and package.is_dir()
            ]
        except OSError:
            return []
        entries = [_archived_entry(package, infos.get(package.name)) for package in packages]
        intent = read_intent(history.home)
        if intent is not None and all(entry.archive_id != intent.archive_id for entry in entries):
            destination = self._owner._skill_dir(history.home, intent.name)
            if intent.matches(destination):
                entry = _archived_entry(destination, infos.get(intent.archive_id))
                entries.append(replace(entry, archive_id=intent.archive_id))
        return sorted(entries, key=lambda entry: entry.archived_at, reverse=True)

    def _purged_absorbed(
        self, history: SkillHistory, infos: dict[str, SkillArchiveInfo]
    ) -> list[ArchivedSkill]:
        """Absorbed Skills whose archived files were purged; the history keeps their target."""
        return [
            _archived_from_info(info, available=False)
            for info in infos.values()
            if info.absorbed_into
            and not info.restored
            and not (history.archive_root / info.archive_id).exists()
        ]

    def _with_holder(
        self, root: Path, newest: dict[str, ArchivedSkill], entry: ArchivedSkill
    ) -> ArchivedSkill:
        """Follow the merges from an absorbed *entry* to the Skill that holds it now."""
        target, since = entry.absorbed_into, entry.archived_at
        passed = {entry.name}
        while target is not None and target not in passed:
            if self._is_live(root, target):
                return replace(entry, holder=target, holder_since=since)
            passed.add(target)
            later = newest.get(target)
            if later is None:
                break
            target, since = later.absorbed_into, later.archived_at
        return entry

    def _is_live(self, root: Path, skill_name: str) -> bool:
        try:
            self._owner._existing_skill_dir(root, skill_name)
        except SkillAuthoringError:
            return False
        return True


def _archived_entry(package: Path, info: SkillArchiveInfo | None) -> ArchivedSkill:
    fields = document_fields(package / SKILL_FILENAME)
    description = fields.get("description")
    description = description.strip() if isinstance(description, str) else ""
    if info is not None:
        return replace(_archived_from_info(info, available=True), description=description)
    name = fields.get("name")
    try:
        modified = package.stat().st_mtime
    except OSError:
        modified = 0.0
    return ArchivedSkill(
        archive_id=package.name,
        name=name.strip() if isinstance(name, str) and name.strip() else package.name,
        archived_at=format_canonical_timestamp(datetime.fromtimestamp(modified, tz=UTC)),
        reason=None,
        absorbed_into=None,
        archived_by=None,
        origin=declared_origin(package),
        description=description,
    )


def _newest_archives(entries: Iterable[ArchivedSkill]) -> dict[str, ArchivedSkill]:
    """The newest archive entry of each Skill name."""
    newest: dict[str, ArchivedSkill] = {}
    for entry in entries:
        known = newest.get(entry.name)
        if known is None or entry.archived_at > known.archived_at:
            newest[entry.name] = entry
    return newest


def _archived_from_info(info: SkillArchiveInfo, *, available: bool) -> ArchivedSkill:
    return ArchivedSkill(
        archive_id=info.archive_id,
        name=info.skill,
        archived_at=info.at,
        reason=info.reason,
        absorbed_into=info.absorbed_into,
        archived_by=info.actor,
        origin=info.origin,
        description="",
        available=available,
    )


_LOGGER = get_logger("skills.authoring")
