"""Validated, path-safe direct writes for local vBot Skills, with their history.

Every Skill-authoring surface resolves its writable scope to a target root (a
writable Skill home) before calling this service. The service validates Skill
documents, confines every path to that root, protects bundled roots, stamps
provenance, and performs each text write atomically. It never resolves scopes or
writes Project Skills.

Every write records one revision in the home's Skill history (``_history``),
naming its ``SkillWriter``. Deleting moves the package into the home's archive,
from which it can be restored or purged. Background writers (``reflection``,
``librarian``) change only Skills that an Agent or a background writer created
and the user has not pinned.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Literal

import yaml

from core.skills._history import (
    ARCHIVE_REASONS,
    BACKGROUND_ACTORS,
    BACKGROUND_WRITABLE_ORIGINS,
    PROVENANCE_AUTHOR_KEY,
    SKILL_ACTORS,
    SkillActor,
    SkillArchiveInfo,
    SkillArchiveReason,
    SkillHistory,
    SkillRecord,
    SkillRevision,
    SkillRevisionKind,
    declared_origin,
    document_fields,
)
from core.skills._installation import SkillInstallResult, install_package
from core.skills._packages import MAX_DOWNLOAD_BYTES, PackageError, is_redirect
from core.skills._revert import (
    RevertActor,
    RevertConflictError,
    RevertError,
    check_revert,
    revert_revisions,
)
from core.skills.requirements import (
    REQUIREMENTS_METADATA_KEY,
    RequirementParseError,
    parse_vbot_requirements,
)
from core.skills.skill_validator import (
    FRONT_MATTER_DELIMITER,
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_TRIGGER_PATTERN,
    ValidationResult,
    normalize_and_validate_skill_metadata,
    parse_skill_front_matter,
    split_skill_document,
)
from core.skills.skills import RESOURCE_DIRECTORIES, SKILL_FILENAME
from core.utils.atomic import atomic_write_bytes
from core.utils.errors import VBotError
from core.utils.ids import is_reserved_name, new_id, reserved_name_message
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp

_LOGGER = get_logger("skills.authoring")

PROVENANCE_SOURCE_KEY = "source"
SKILL_ARCHIVE_MAX_BYTES = MAX_DOWNLOAD_BYTES

# ``metadata.vbot.author`` stamped into a written ``SKILL.md``.
SkillAuthor = Literal["agent", "human"]
SkillFileChangeKind = Literal["created", "updated", "deleted"]
SkillProtection = Literal["pinned", "user", "unknown"]


class SkillAuthoringError(VBotError):
    """Raised when a Skill write fails validation or path confinement."""

    def __init__(self, message: str, *, diagnostics: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.diagnostics: list[str] = list(diagnostics) if diagnostics else [message]


class SkillProtectedError(SkillAuthoringError):
    """A background writer tried to change a Skill it may not change.

    ``reason`` is ``pinned`` (the user pinned it), ``user`` (a person created
    it) or ``unknown`` (its history cannot be read, so its origin is unknown).
    """

    def __init__(self, skill_name: str, reason: SkillProtection) -> None:
        explanation = {
            "pinned": "the user pinned it",
            "user": "a person created it",
            "unknown": "its history cannot be read",
        }[reason]
        super().__init__(
            f"Skill '{skill_name}' is protected from background changes: {explanation}."
        )
        self.skill_name = skill_name
        self.reason = reason


class SkillRevertConflictError(SkillAuthoringError):
    """A later revision changed the same part of the Skill as a reverted one."""

    def __init__(self, message: str, *, revision: int, later: int, skill_name: str) -> None:
        super().__init__(message)
        self.revision = revision
        self.later = later
        self.skill_name = skill_name


@dataclass(frozen=True)
class SkillWriter:
    """Who performs a Skill write; recorded with its revision.

    ``actor`` is ``human`` (a person through the WebUI, CLI or RPC), ``agent``
    (an attended Agent's Tool call), ``reflection`` or ``librarian`` (background
    Runs). A written ``SKILL.md`` is stamped ``metadata.vbot.author: human`` for
    a person and ``agent`` otherwise.
    """

    actor: SkillActor = "human"
    session_id: str | None = None
    run_id: str | None = None
    run_kind: str | None = None

    @property
    def background(self) -> bool:
        return self.actor in BACKGROUND_ACTORS

    @property
    def author(self) -> SkillAuthor:
        return "human" if self.actor == "human" else "agent"


HUMAN_WRITER = SkillWriter()


@dataclass(frozen=True)
class ArchivedSkill:
    """One package in a home's archive.

    ``available`` is false for an absorbed Skill whose archived files were
    purged: the history still knows where its instructions went.
    """

    archive_id: str
    name: str
    archived_at: str
    reason: str | None
    absorbed_into: str | None
    archived_by: str | None
    origin: str
    description: str
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "name": self.name,
            "archived_at": self.archived_at,
            "reason": self.reason,
            "absorbed_into": self.absorbed_into,
            "archived_by": self.archived_by,
            "origin": self.origin,
            "description": self.description,
        }


# A changed file larger than this reports no text; its change is still reported.
MAX_CHANGE_TEXT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SkillFileChange:
    """One file a Skill mutation changed, by its package-relative path.

    ``before``/``after`` hold its text with LF line endings; ``None`` means the
    file was absent on that side or is not UTF-8 text.
    """

    path: str
    change: SkillFileChangeKind
    before: str | None
    after: str | None


@dataclass(frozen=True)
class SkillWriteResult:
    """Outcome of one successful direct Skill mutation."""

    name: str
    operation: str
    path: Path
    warnings: list[str] = field(default_factory=list)
    changes: tuple[SkillFileChange, ...] = ()
    # The recorded history revision, ``None`` when nothing was recorded.
    revision: int | None = None
    archive_id: str | None = None


class SkillAuthoringService:
    """One validated write core for complete Skill imports and authored text files."""

    def __init__(self, protected_roots: Sequence[Path] = ()) -> None:
        self._protected_roots = [self._resolve(root) for root in protected_roots]
        self._write_lock = RLock()

    def create(
        self,
        target_root: Path,
        skill_name: str,
        content: str,
        *,
        writer: SkillWriter,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Create ``<target_root>/<skill_name>/SKILL.md``."""
        with self._write_lock:
            _check_writer(writer)
            _validate_skill_name(skill_name)
            if is_reserved_name(skill_name):
                raise SkillAuthoringError(reserved_name_message("Skill name", skill_name))
            skill_dir = self._skill_dir(target_root, skill_name)
            if skill_dir.exists():
                raise SkillAuthoringError(f"Skill '{skill_name}' already exists.")
            skill_file = skill_dir / SKILL_FILENAME
            document, validation = self._prepare_document(
                content,
                skill_name=skill_name,
                skill_file=skill_file,
                author=writer.author,
                source=source,
            )
            history = SkillHistory(skill_dir.parent)
            self._observe(history, skill_name, None, writer)
            skill_dir.mkdir(parents=True, exist_ok=False)
            try:
                _atomic_write_styled(skill_file, document, "\n")
            except OSError:
                shutil.rmtree(skill_dir, ignore_errors=True)
                raise
            revision = self._commit(
                history,
                skill_name,
                skill_dir,
                writer,
                "create",
                paths=(SKILL_FILENAME,),
                origin=writer.actor,
            )
            return SkillWriteResult(
                name=skill_name,
                operation="create",
                path=skill_file,
                warnings=validation.warnings,
                changes=(SkillFileChange(SKILL_FILENAME, "created", None, document),),
                revision=revision,
            )

    def install(
        self,
        target_root: Path,
        source: str,
        *,
        writer: SkillWriter = HUMAN_WRITER,
        path: str | None = None,
        ref: str | None = None,
        replace: bool = False,
        dry_run: bool = False,
        archive: bytes | None = None,
        expected_sha256: str | None = None,
    ) -> SkillInstallResult:
        """Install a source or uploaded archive (source is its filename), never executing it."""
        _check_writer(writer)
        self._reject_protected(self._resolve(target_root))
        try:
            return install_package(
                self,
                target_root,
                source,
                writer=writer,
                path=path,
                ref=ref,
                replace=replace,
                dry_run=dry_run,
                archive=archive,
                expected_sha256=expected_sha256,
            )
        except PackageError as error:
            raise SkillAuthoringError(str(error)) from error

    def edit(
        self,
        target_root: Path,
        skill_name: str,
        content: str,
        *,
        writer: SkillWriter,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Replace an existing Skill's complete ``SKILL.md``.

        The new document does not depend on the old one, so a ``SKILL.md`` that
        is not UTF-8 text is replaced too; its old text is not reported.
        """
        with self._write_lock:
            _check_writer(writer)
            skill_file = self._existing_skill_file(target_root, skill_name)
            skill_dir = skill_file.parent
            history = SkillHistory(skill_dir.parent)
            self._observe(history, skill_name, skill_dir, writer)
            try:
                current: str | None = _read_raw_text(skill_file)
            except UnicodeDecodeError:
                current = None
            file_ending = _detect_line_ending(current) if current else "\n"
            document, validation = self._prepare_document(
                content,
                skill_name=skill_name,
                skill_file=skill_file,
                author=writer.author,
                source=source,
            )
            _atomic_write_styled(skill_file, document, file_ending)
            return SkillWriteResult(
                name=skill_name,
                operation="edit",
                path=skill_file,
                warnings=validation.warnings,
                changes=(
                    SkillFileChange(
                        SKILL_FILENAME,
                        "updated",
                        None if current is None else _normalize_newlines(current),
                        document,
                    ),
                ),
                revision=self._commit(
                    history, skill_name, skill_dir, writer, "change", paths=(SKILL_FILENAME,)
                ),
            )

    def read_text(self, target_root: Path, skill_name: str, relative_path: str) -> str:
        """Return ``SKILL.md`` or one UTF-8 support file with LF line endings."""
        target, normalized = self._existing_text_file(target_root, skill_name, relative_path)
        return _normalize_newlines(_read_text_file(target, normalized))

    def rewrite(
        self,
        target_root: Path,
        skill_name: str,
        relative_path: str,
        edit: Callable[[str], str],
        *,
        writer: SkillWriter,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Rewrite ``SKILL.md`` or one UTF-8 support file through ``edit``.

        ``edit`` receives the current text with LF line endings while the write
        lock is held and returns the new text; raising aborts without writing.
        A rewritten ``SKILL.md`` is validated and stamped like ``edit``. The file
        keeps its line-ending style.
        """
        with self._write_lock:
            _check_writer(writer)
            target, normalized = self._existing_text_file(target_root, skill_name, relative_path)
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            history = SkillHistory(skill_dir.parent)
            self._observe(history, skill_name, skill_dir, writer)
            current = _read_text_file(target, normalized)
            file_ending = _detect_line_ending(current)
            updated = _normalize_newlines(edit(_normalize_newlines(current)))
            warnings: list[str] = []
            if normalized == SKILL_FILENAME:
                updated, validation = self._prepare_document(
                    updated,
                    skill_name=skill_name,
                    skill_file=target,
                    author=writer.author,
                    source=source,
                )
                warnings = validation.warnings
            _atomic_write_styled(target, updated, file_ending)
            return SkillWriteResult(
                name=skill_name,
                operation="rewrite",
                path=target,
                warnings=warnings,
                changes=(
                    SkillFileChange(normalized, "updated", _normalize_newlines(current), updated),
                ),
                revision=self._commit(
                    history, skill_name, skill_dir, writer, "change", paths=(normalized,)
                ),
            )

    def _existing_text_file(
        self, target_root: Path, skill_name: str, relative_path: str
    ) -> tuple[Path, str]:
        skill_dir = self._existing_skill_dir(target_root, skill_name)
        normalized = normalize_skill_file_path(relative_path)
        target = (
            skill_dir / SKILL_FILENAME
            if normalized == SKILL_FILENAME
            else self._resource_path(skill_dir, normalized)
        )
        if not target.is_file():
            raise SkillAuthoringError(f"Skill file not found: {normalized}")
        return target, normalized

    def delete(
        self,
        target_root: Path,
        skill_name: str,
        *,
        writer: SkillWriter,
        reason: SkillArchiveReason | None = None,
        absorbed_into: str | None = None,
    ) -> SkillWriteResult:
        """Move a Skill package into the home's archive.

        ``reason`` is ``deleted`` by default and ``absorbed`` with
        ``absorbed_into``, another Skill in the same home that now holds this
        Skill's instructions; ``inactive`` marks a Skill retired for disuse.
        The reported changes delete every package file; the archived package
        keeps them for ``restore`` until ``purge``.
        """
        with self._write_lock:
            _check_writer(writer)
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            root = skill_dir.parent
            reason, absorbed_into = self._archive_reason(root, skill_dir, reason, absorbed_into)
            history = SkillHistory(root)
            self._observe(history, skill_name, skill_dir, writer)
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
            revision = self._commit(
                history,
                skill_name,
                None,
                writer,
                "archive",
                live=False,
                reason=reason,
                absorbed_into=absorbed_into,
                archive_id=archive_id,
            )
            return SkillWriteResult(
                name=skill_name,
                operation="delete",
                path=destination,
                changes=changes,
                revision=revision,
                archive_id=archive_id,
            )

    def write_file(
        self,
        target_root: Path,
        skill_name: str,
        relative_path: str,
        content: str,
        *,
        writer: SkillWriter,
    ) -> SkillWriteResult:
        """Create or replace one UTF-8 support file."""
        if not isinstance(content, str):
            raise SkillAuthoringError("Support file content must be a string.")
        with self._write_lock:
            _check_writer(writer)
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            _reject_new_reserved_path(skill_dir, relative_path)
            resource_path = self._resource_path(skill_dir, relative_path)
            history = SkillHistory(skill_dir.parent)
            self._observe(history, skill_name, skill_dir, writer)
            existed = resource_path.is_file()
            file_ending = "\n"
            if existed:
                try:
                    existing = _read_raw_text(resource_path)
                except UnicodeDecodeError:
                    existing = ""
                if existing:
                    file_ending = _detect_line_ending(existing)
            before = _change_text(resource_path) if existed else None
            text = _normalize_newlines(content)
            styled = _to_line_ending(text, file_ending)
            resource_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(resource_path, styled.encode("utf-8"))
            normalized = _normalized_support_path(relative_path)
            return SkillWriteResult(
                name=skill_name,
                operation="write_file",
                path=resource_path,
                changes=(
                    SkillFileChange(normalized, "updated" if existed else "created", before, text),
                ),
                revision=self._commit(
                    history, skill_name, skill_dir, writer, "change", paths=(normalized,)
                ),
            )

    def remove_file(
        self,
        target_root: Path,
        skill_name: str,
        relative_path: str,
        *,
        writer: SkillWriter,
    ) -> SkillWriteResult:
        """Remove one support file."""
        with self._write_lock:
            _check_writer(writer)
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            resource_path = self._resource_path(skill_dir, relative_path)
            if not resource_path.is_file():
                raise SkillAuthoringError(f"Support file not found: {relative_path}")
            history = SkillHistory(skill_dir.parent)
            self._observe(history, skill_name, skill_dir, writer)
            before = _change_text(resource_path)
            resource_path.unlink()
            _remove_empty_resource_parents(resource_path.parent, skill_dir)
            normalized = _normalized_support_path(relative_path)
            return SkillWriteResult(
                name=skill_name,
                operation="remove_file",
                path=resource_path,
                changes=(SkillFileChange(normalized, "deleted", before, None),),
                revision=self._commit(
                    history, skill_name, skill_dir, writer, "change", paths=(normalized,)
                ),
            )

    def check_writable(self, target_root: Path, skill_name: str, *, writer: SkillWriter) -> None:
        """Raise ``SkillProtectedError`` when *writer* may not change an existing Skill.

        Every write enforces the same rule; this lets a caller refuse before it
        prepares a change.
        """
        with self._write_lock:
            _check_writer(writer)
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            self._observe(SkillHistory(skill_dir.parent), skill_name, skill_dir, writer)

    def set_pinned(
        self, target_root: Path, skill_name: str, pinned: bool, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Pin or unpin a Skill; background writers never change a pinned Skill.

        Only a person pins. The pin lives in the Skill history, so a history that
        cannot be written fails the call. Setting the current state records nothing.
        """
        with self._write_lock:
            _check_writer(writer)
            if writer.actor != "human":
                raise SkillAuthoringError("Only a person can pin or unpin a Skill.")
            skill_dir = self._existing_skill_dir(target_root, skill_name)
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
        with self._write_lock:
            root = self._resolve(target_root)
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

        Skills a background writer may change are absent. When the history
        cannot be read, every Skill of the home is ``unknown``, as a write would be.
        """
        records = self.records(target_root)
        with self._write_lock:
            names = [package.name for package in _live_packages(self._resolve(target_root))]
        protection: dict[str, SkillProtection] = {}
        for name in names:
            record = records.get(name)
            if record is None:
                protection[name] = "unknown"
            elif record.pinned:
                protection[name] = "pinned"
            elif record.origin not in BACKGROUND_WRITABLE_ORIGINS:
                protection[name] = "user"
        return protection

    def record(self, target_root: Path, skill_name: str) -> SkillRecord | None:
        """Return the record of one Skill in a writable home, if it exists there."""
        with self._write_lock:
            root = self._resolve(target_root)
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
        with self._write_lock:
            root = self._resolve(target_root)
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
        history = SkillHistory(self._resolve(target_root))
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
        with self._write_lock:
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
        blocks.
        """
        with self._write_lock:
            history = self._revert_history(target_root, writer)
            with _revert_errors():
                return revert_revisions(
                    history,
                    history.home,
                    revision_ids,
                    RevertActor(writer.actor, writer.session_id, writer.run_id, writer.run_kind),
                    related=related,
                )

    def _revert_history(self, target_root: Path, writer: SkillWriter) -> SkillHistory:
        _check_writer(writer)
        if writer.background:
            raise SkillAuthoringError("Background writers cannot revert Skill revisions.")
        return SkillHistory(self._writable_root(target_root))

    def archived(self, target_root: Path) -> list[ArchivedSkill]:
        """Return the packages in a home's archive, newest first."""
        with self._write_lock:
            history = SkillHistory(self._resolve(target_root))
            return self._archived(history, self._archive_infos(history))

    def archived_skill(self, target_root: Path, skill_name: str) -> ArchivedSkill | None:
        """Return the newest archive entry of *skill_name* in a home, if any.

        Includes an absorbed Skill whose archived files were purged
        (``available`` false).
        """
        with self._write_lock:
            history = SkillHistory(self._resolve(target_root))
            infos = self._archive_infos(history)
            entries = [
                entry for entry in self._archived(history, infos) if entry.name == skill_name
            ]
            listed = {entry.archive_id for entry in entries}
            for info in infos.values():
                if (
                    info.skill == skill_name
                    and info.absorbed_into
                    and not info.restored
                    and info.archive_id not in listed
                    and not (history.archive_root / info.archive_id).exists()
                ):
                    entries.append(_archived_from_info(info, available=False))
            return max(entries, key=lambda entry: entry.archived_at, default=None)

    def restore(
        self, target_root: Path, archive_id: str, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Move an archived package back into the home under its Skill name.

        Refused while a Skill with that name exists in the home.
        """
        with self._write_lock:
            _check_writer(writer)
            root = self._writable_root(target_root)
            history = SkillHistory(root)
            package = self._archived_package(history, archive_id)
            info = self._archive_infos(history).get(archive_id)
            name = info.skill if info is not None else _declared_name(package)
            destination = self._skill_dir(root, name)
            if destination.exists():
                raise SkillAuthoringError(
                    f"A Skill named '{name}' already exists. Delete or rename it first, "
                    "then restore this one."
                )
            self._observe(history, name, None, writer)
            package.rename(destination)
            revision = self._commit(
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
        with self._write_lock:
            root = self._writable_root(target_root)
            history = SkillHistory(root)
            package = self._archived_package(history, archive_id)
            entry = _archived_entry(package, self._archive_infos(history).get(archive_id))
            shutil.rmtree(package)
            _LOGGER.info("Purged archived Skill '%s' (%s) from %s", entry.name, archive_id, root)
            return entry

    def _observe(
        self,
        history: SkillHistory,
        skill_name: str,
        skill_dir: Path | None,
        writer: SkillWriter,
    ) -> None:
        """Record outside changes before a write; enforce the background rules.

        A history failure only logs, except for a background writer changing an
        existing Skill: it cannot know the Skill's origin and is refused.
        """
        try:
            record = history.observe(skill_name, skill_dir)
        except Exception as error:
            if writer.background and skill_dir is not None:
                raise SkillProtectedError(skill_name, "unknown") from error
            _LOGGER.warning("Skill history of '%s' not updated: %s", skill_name, error)
            return
        if writer.background and record is not None:
            if record.pinned:
                raise SkillProtectedError(skill_name, "pinned")
            if record.origin not in BACKGROUND_WRITABLE_ORIGINS:
                raise SkillProtectedError(skill_name, "user")

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
                **fields,
            ).id
        except Exception as error:
            _LOGGER.warning("Skill history of '%s' not recorded: %s", skill_name, error)
            return None

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
        target = self._existing_skill_dir(root, absorbed_into)
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
        _reject_redirect(history.archive_root)
        package = history.archive_root / archive_id
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

    def _writable_root(self, target_root: Path) -> Path:
        root = self._resolve(target_root)
        self._reject_protected(root)
        return root

    def _skill_dir(self, target_root: Path, skill_name: str) -> Path:
        _validate_skill_name(skill_name)
        root = self._resolve(target_root)
        self._reject_protected(root)
        _reject_redirect(root / skill_name)
        skill_dir = self._resolve(root / skill_name)
        if skill_dir.parent != root:
            raise SkillAuthoringError(f"Illegal skill name escapes target root: {skill_name!r}")
        return skill_dir

    def _existing_skill_dir(self, target_root: Path, skill_name: str) -> Path:
        skill_dir = self._skill_dir(target_root, skill_name)
        if not skill_dir.is_dir():
            raise SkillAuthoringError(f"Skill '{skill_name}' not found.")
        _reject_redirect(skill_dir / SKILL_FILENAME)
        if not (skill_dir / SKILL_FILENAME).is_file():
            raise SkillAuthoringError(f"Skill '{skill_name}' has no {SKILL_FILENAME}.")
        return skill_dir

    def _existing_skill_file(self, target_root: Path, skill_name: str) -> Path:
        return self._existing_skill_dir(target_root, skill_name) / SKILL_FILENAME

    def _resource_path(self, skill_dir: Path, relative_path: str) -> Path:
        normalized = _normalized_support_path(relative_path)
        skill_dir_resolved = self._resolve(skill_dir)
        candidate = skill_dir_resolved
        for part in PurePosixPath(normalized).parts:
            candidate /= part
            _reject_redirect(candidate)
        candidate = self._resolve(candidate)
        if candidate == skill_dir_resolved or not _is_within(
            candidate,
            skill_dir_resolved,
        ):
            raise SkillAuthoringError(f"Illegal support file path: {relative_path}")
        return candidate

    def _reject_protected(self, root: Path) -> None:
        for protected in self._protected_roots:
            if root == protected or _is_within(root, protected):
                raise SkillAuthoringError(
                    "Refusing to write skills under a protected (bundled) root."
                )

    @staticmethod
    def _resolve(path: Path) -> Path:
        return Path(path).expanduser().resolve()

    def _prepare_document(
        self,
        content: str,
        *,
        skill_name: str,
        skill_file: Path,
        author: SkillAuthor,
        source: str | None,
    ) -> tuple[str, ValidationResult]:
        front_matter, body, document_warnings = split_skill_document(content)
        fields, parse_warnings = parse_skill_front_matter(front_matter)
        fields, result = normalize_and_validate_skill_metadata(
            fields,
            directory_name=skill_name,
            skill_file=skill_file,
            body=body,
            parse_warnings=[*document_warnings, *parse_warnings],
        )
        declared_name = str(fields.get("name", "")).strip()
        if declared_name != skill_name:
            raise SkillAuthoringError(
                f"Skill name '{declared_name}' must match its directory name '{skill_name}'."
            )

        metadata = fields.get("metadata")
        try:
            parse_vbot_requirements(metadata if isinstance(metadata, dict) else {})
        except RequirementParseError as error:
            raise SkillAuthoringError(
                str(error),
                diagnostics=[str(error)],
            ) from error

        stamped = _with_provenance(fields, author=author, source=source)
        return _assemble_document(stamped, body), result


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


def _declared_name(package: Path) -> str:
    name = document_fields(package / SKILL_FILENAME).get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillAuthoringError("The archived package declares no Skill name.")
    return name.strip()


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


def _reject_redirect(path: Path) -> None:
    try:
        redirect = is_redirect(path)
    except FileNotFoundError:
        return
    if redirect:
        raise SkillAuthoringError("Refusing to write through a symlink or junction Skill path.")


def _reject_new_reserved_path(skill_dir: Path, relative_path: str) -> None:
    """Refuse to create a support file or folder under a name Windows reserves.

    Checked on every platform, before the path is resolved (Windows maps device
    names to devices). Replacing a file that already exists stays allowed.
    """
    normalized = _normalized_support_path(relative_path)
    reserved = next(
        (part for part in PurePosixPath(normalized).parts if is_reserved_name(part)), None
    )
    if reserved is not None and not (skill_dir / normalized).is_file():
        raise SkillAuthoringError(reserved_name_message("file or folder name", reserved))


def normalize_skill_file_path(relative_path: str) -> str:
    """Normalize one skill-package-relative path or raise ``SkillAuthoringError``.

    The authoring rule for addressing a file inside a skill package:
    ``SKILL.md`` passes through unchanged; every other path must be relative
    and live under one of the resource directories (``scripts/``,
    ``references/``, ``assets/``). Rejects absolute paths, empty segments,
    and dot segments. Backslashes are accepted as separators.
    """
    if relative_path.replace("\\", "/") == SKILL_FILENAME:
        return SKILL_FILENAME
    return _normalized_support_path(relative_path)


def _normalized_support_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise SkillAuthoringError("Support file path must be a non-empty string.")
    raw = PurePosixPath(relative_path.replace("\\", "/"))
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise SkillAuthoringError(f"Illegal support file path: {relative_path}")
    if len(raw.parts) < 2 or raw.parts[0] not in RESOURCE_DIRECTORIES:
        allowed = " or ".join(f"{name}/" for name in RESOURCE_DIRECTORIES)
        raise SkillAuthoringError(f"Support files must live under {allowed}")
    return raw.as_posix()


def _normalize_newlines(text: str) -> str:
    """Normalize CR/CRLF to LF for line-ending-tolerant matching."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_raw_text(path: Path) -> str:
    """Read a file without newline translation (style-preserving read)."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _detect_line_ending(text: str) -> str:
    """Return the line ending used in ``text`` (CRLF, CR, or LF)."""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _to_line_ending(text: str, ending: str) -> str:
    """Convert LF-normalized text to the target line ending."""
    if ending == "\n":
        return text
    return text.replace("\n", ending)


def _change_text(path: Path) -> str | None:
    """Return a changed file's LF text, or ``None`` when it is not reportable text."""
    try:
        if path.stat().st_size > MAX_CHANGE_TEXT_BYTES:
            return None
        return _normalize_newlines(_read_raw_text(path))
    except OSError, UnicodeDecodeError:
        return None


def _read_text_file(path: Path, relative_path: str) -> str:
    try:
        return _read_raw_text(path)
    except UnicodeDecodeError as error:
        raise SkillAuthoringError(f"Skill file is not UTF-8 text: {relative_path}") from error


def _atomic_write_styled(target_path: Path, text: str, file_ending: str) -> None:
    """Write ``text`` to ``target_path`` preserving ``file_ending``.

    Goes through bytes so the text-mode newline translation of
    ``atomic_write_text`` cannot double-convert (on Windows it would turn an
    explicit CRLF text into CRCRLF).
    """
    styled = _to_line_ending(_normalize_newlines(text), file_ending)
    atomic_write_bytes(target_path, styled.encode("utf-8"))


def _remove_empty_resource_parents(parent: Path, skill_dir: Path) -> None:
    skill_root = skill_dir.resolve()
    current = parent
    while current != skill_root and _is_within(current.resolve(), skill_root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _validate_skill_name(skill_name: str) -> None:
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise SkillAuthoringError("Skill name must be a non-empty string.")
    if skill_name in {".", ".."} or ".." in skill_name or "\x00" in skill_name:
        raise SkillAuthoringError(f"Illegal skill name: {skill_name!r}")
    if "/" in skill_name or "\\" in skill_name:
        raise SkillAuthoringError(f"Skill name must be a single path segment: {skill_name!r}")
    if skill_name != skill_name.strip():
        raise SkillAuthoringError("Skill name must not have leading or trailing whitespace.")
    if not SKILL_NAME_TRIGGER_PATTERN.match(skill_name):
        raise SkillAuthoringError(
            "Skill name must start with a letter or digit, contain only letters, "
            f"digits, '-', or '_', and be at most {MAX_SKILL_NAME_LENGTH} characters "
            f"long: {skill_name!r}"
        )


def _with_provenance(
    fields: dict[str, Any],
    *,
    author: SkillAuthor,
    source: str | None,
) -> dict[str, Any]:
    updated = dict(fields)
    raw_metadata = updated.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    raw_vbot = metadata.get(REQUIREMENTS_METADATA_KEY)
    vbot = dict(raw_vbot) if isinstance(raw_vbot, dict) else {}
    vbot[PROVENANCE_AUTHOR_KEY] = author
    if source is not None:
        vbot[PROVENANCE_SOURCE_KEY] = source
    metadata[REQUIREMENTS_METADATA_KEY] = vbot
    updated["metadata"] = metadata
    return updated


def _assemble_document(fields: dict[str, Any], body: str) -> str:
    front = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True).strip()
    document = f"{FRONT_MATTER_DELIMITER}\n{front}\n{FRONT_MATTER_DELIMITER}"
    stripped_body = body.strip("\n")
    if stripped_body:
        return f"{document}\n\n{stripped_body}\n"
    return f"{document}\n"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "HUMAN_WRITER",
    "PROVENANCE_AUTHOR_KEY",
    "PROVENANCE_SOURCE_KEY",
    "ArchivedSkill",
    "SkillActor",
    "SkillAuthor",
    "SkillAuthoringError",
    "SkillAuthoringService",
    "SkillProtectedError",
    "SkillRecord",
    "SkillRevertConflictError",
    "SkillRevision",
    "SkillWriteResult",
    "SkillWriter",
]
