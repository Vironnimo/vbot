"""Skill package archival, restoration, purge and absorbed-instruction lookup."""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
from core.skills.skills import SKILL_FILENAME, SkillRegistry
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

        Refused while a Skill with that name exists in the home.
        """
        with self._owner._write_lock:
            _check_writer(writer)
            root = self._owner._writable_root(target_root)
            history = SkillHistory(root)
            package = self._archived_package(history, archive_id)
            info = self._archive_infos(history).get(archive_id)
            name = info.skill if info is not None else _declared_name(package)
            destination = self._owner._skill_dir(root, name)
            if destination.exists() or any(
                skill.name == name for skill in SkillRegistry.load(root).list_all()
            ):
                raise SkillAuthoringError(
                    f"A Skill named '{name}' already exists. Delete or rename it first, "
                    "then restore this one."
                )
            self._owner._revisions._observe(history, name, None, writer)
            package.rename(destination)
            revision = self._owner._revisions._commit(
                history,
                name,
                destination,
                writer,
                "restore",
                live=True,
                archive_id=archive_id,
                origin=info.origin if info is not None else declared_origin(destination),
                created_at=info.created_at if info is not None else None,
                pinned=info.pinned if info is not None else False,
            )
            files = sorted(
                base / file_name
                for base, _, file_names in destination.walk()
                for file_name in file_names
            )
            return SkillWriteResult(
                name=name,
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

    def purge(self, target_root: Path, archive_id: str) -> ArchivedSkill:
        """Permanently delete one archived package; return what it was."""
        with self._owner._write_lock:
            root = self._owner._writable_root(target_root)
            history = SkillHistory(root)
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

    def _archived_package(self, history: SkillHistory, archive_id: str) -> Path:
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
        # Only an archive entry spelled exactly like the id names a package. On
        # Windows a drive-relative id ("C:..") joins as the archive's parent, and
        # a case variant opens the real entry under a spelling no history knows.
        if (
            package.name != archive_id
            or package.parent != archive_root
            or not has_id_entry(archive_root, archive_id)
        ):
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
