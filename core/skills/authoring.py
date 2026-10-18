"""Validated, path-safe direct writes for local vBot Skills, with their history.

Every Skill-authoring surface resolves its writable scope to a target root (a
writable Skill home) before calling this service. The service validates Skill
documents, confines every path to that root, protects bundled roots, stamps
provenance, and performs each text write atomically. It never resolves scopes or
writes Project Skills.

Every write records one revision in the home's Skill history (``_history``),
naming its ``SkillWriter``. Deleting moves the package into the home's archive,
from which it can be restored or purged. Background writers (``reflection``,
``librarian``) never change a Skill the user pinned.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from threading import RLock

from core.skills._authoring_archive import SkillArchives
from core.skills._authoring_documents import (
    _assemble_document,
    _atomic_write_styled,
    _change_text,
    _detect_line_ending,
    _is_within,
    _normalize_newlines,
    _normalized_support_path,
    _read_raw_text,
    _read_text_file,
    _reject_new_reserved_path,
    _reject_redirect,
    _remove_empty_resource_parents,
    _to_line_ending,
    _validate_skill_name,
    _with_provenance,
    normalize_skill_file_path,
)
from core.skills._authoring_history import SkillRevisions, _check_writer
from core.skills._authoring_types import (
    HUMAN_WRITER,
    MAX_CHANGE_TEXT_BYTES,
    PROVENANCE_SOURCE_KEY,
    SKILL_ARCHIVE_MAX_BYTES,
    ArchivedSkill,
    SkillAuthor,
    SkillAuthoringError,
    SkillFileChange,
    SkillFileChangeKind,
    SkillMergeAccessError,
    SkillProtectedError,
    SkillProtection,
    SkillRevertConflictError,
    SkillRevertIncompleteError,
    SkillWriter,
    SkillWriteResult,
)
from core.skills._history import (
    PROVENANCE_AUTHOR_KEY,
    SkillActor,
    SkillArchiveReason,
    SkillHistory,
    SkillRecord,
    SkillReference,
    SkillRevision,
)
from core.skills._installation import SkillInstallResult, install_package
from core.skills._packages import PackageError
from core.skills.requirements import RequirementParseError, parse_vbot_requirements
from core.skills.skill_validator import (
    ValidationResult,
    normalize_and_validate_skill_metadata,
    parse_skill_front_matter,
    split_skill_document,
)
from core.skills.skills import SKILL_FILENAME, SkillRegistry
from core.utils.atomic import atomic_write_bytes
from core.utils.file_status import is_file_strict
from core.utils.ids import is_reserved_name, reserved_name_message
from core.utils.logging import get_logger
from core.utils.paths import model_path

_LOGGER = get_logger("skills.authoring")


class SkillAuthoringService:
    """One validated write core for complete Skill imports and authored text files."""

    def __init__(self, protected_roots: Sequence[Path] = ()) -> None:
        self._protected_roots = [self._resolve(root) for root in protected_roots]
        self._write_lock = RLock()
        self._archives = SkillArchives(self)
        self._revisions = SkillRevisions(self)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold off every other Skill write while the block runs.

        Each method takes the same lock, so a caller's check and the write it
        guards see the same Skills: a person's change cannot land in between.
        """
        with self._write_lock:
            yield

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
            self._revisions._observe(history, skill_name, None, writer)
            skill_dir.mkdir(parents=True, exist_ok=False)
            try:
                _atomic_write_styled(skill_file, document, "\n")
            except OSError:
                shutil.rmtree(skill_dir, ignore_errors=True)
                raise
            revision = self._revisions._commit(
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
            self._revisions._observe(history, skill_name, skill_dir, writer)
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
                revision=self._revisions._commit(
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
            self._revisions._observe(history, skill_name, skill_dir, writer)
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
                revision=self._revisions._commit(
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
        return self._archives.delete(
            target_root,
            skill_name,
            writer=writer,
            reason=reason,
            absorbed_into=absorbed_into,
            followed=followed,
        )

    def publish(
        self, source_root: Path, target_root: Path, skill_name: str, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Move a Skill package into another home, such as a private Skill into the global one.

        The target home records a ``create`` keeping the source's creator; the
        source home archives its package with the reason ``published``, so
        ``restore`` can bring the old copy back. The package files are copied
        unchanged, a link inside the package as a link. The reported changes
        create every regular file in the target, root ``SKILL.md`` first.
        """
        with self._write_lock:
            _check_writer(writer)
            source_dir = self._resolved_skill_dir(source_root, skill_name)
            destination = self._skill_dir(target_root, skill_name)
            if destination.exists():
                raise SkillAuthoringError(f"Skill '{skill_name}' already exists.")
            record = self.record(source_dir.parent, skill_name)
            history = SkillHistory(destination.parent)
            self._revisions._observe(history, skill_name, None, writer)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # A sibling staging directory publishes on one filesystem in one rename.
            staging = Path(tempfile.mkdtemp(prefix=".skill-publish-", dir=destination.parent))
            try:
                shutil.copytree(source_dir, staging / "package", symlinks=True)
                (staging / "package").rename(destination)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            entries = (base / name for base, _, names in destination.walk() for name in names)
            files = sorted(
                (path for path in entries if path.is_file() and not path.is_symlink()),
                key=lambda path: (path.name != SKILL_FILENAME or path.parent != destination, path),
            )
            changes = tuple(
                SkillFileChange(
                    path.relative_to(destination).as_posix(), "created", None, _change_text(path)
                )
                for path in files
            )
            try:
                archived = self.delete(source_root, skill_name, writer=writer, reason="published")
            except Exception:
                try:
                    shutil.rmtree(destination)
                except OSError as cleanup_error:
                    raise SkillAuthoringError(
                        f"Skill '{skill_name}' is still at its source. Publishing failed, "
                        f"and cleanup left files at {model_path(destination)}. Ask the user "
                        "to remove that destination folder from the filesystem before retrying."
                    ) from cleanup_error
                raise
            revision = self._revisions._commit(
                history,
                skill_name,
                destination,
                writer,
                "create",
                origin=record.origin if record is not None else writer.actor,
            )
            return SkillWriteResult(
                name=skill_name,
                operation="publish",
                path=destination / SKILL_FILENAME,
                changes=changes,
                revision=revision,
                archive_id=archived.archive_id,
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
            self._revisions._observe(history, skill_name, skill_dir, writer)
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
                revision=self._revisions._commit(
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
            self._revisions._observe(history, skill_name, skill_dir, writer)
            before = _change_text(resource_path)
            resource_path.unlink()
            _remove_empty_resource_parents(resource_path.parent, skill_dir)
            normalized = _normalized_support_path(relative_path)
            return SkillWriteResult(
                name=skill_name,
                operation="remove_file",
                path=resource_path,
                changes=(SkillFileChange(normalized, "deleted", before, None),),
                revision=self._revisions._commit(
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
            skill_dir = self._resolved_skill_dir(target_root, skill_name)
            self._revisions._observe(SkillHistory(skill_dir.parent), skill_name, skill_dir, writer)

    def set_pinned(
        self, target_root: Path, skill_name: str, pinned: bool, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Pin or unpin a Skill; background writers never change a pinned Skill.

        Only a person pins. The pin lives in the Skill history, so a history that
        cannot be written fails the call. Setting the current state records nothing.
        """
        return self._revisions.set_pinned(target_root, skill_name, pinned, writer=writer)

    def records(self, target_root: Path) -> dict[str, SkillRecord]:
        """Return origin, pin and change records of every Skill in a writable home.

        A Skill the history does not know yet gets its ``baseline`` revision. An
        unreadable history yields no records.
        """
        return self._revisions.records(target_root)

    def background_protection(self, target_root: Path) -> dict[str, SkillProtection]:
        """Return why background writers may not change each protected Skill of a home.

        A pinned Skill is ``pinned``; Skills a background writer may change are
        absent. When the history cannot be read, every Skill of the home is
        ``unknown``, as a write would be.
        """
        return self._revisions.background_protection(target_root)

    def record(self, target_root: Path, skill_name: str) -> SkillRecord | None:
        """Return the record of one Skill in a writable home, if it exists there."""
        return self._revisions.record(target_root, skill_name)

    def history(
        self, target_root: Path, skill_name: str | None = None, *, limit: int = 50
    ) -> list[SkillRevision]:
        """Return a home's revisions, or one Skill's, newest first.

        Changes made outside vBot since the last write are recorded first.
        """
        return self._revisions.history(target_root, skill_name, limit=limit)

    def recorded_revisions(self, target_root: Path) -> list[SkillRevision]:
        """Return the revisions a home's history holds, oldest first.

        Unlike :meth:`history`, this reads only the history: outside changes of
        the packages not noticed yet are not recorded first.
        """
        return self._revisions.recorded_revisions(target_root)

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
        return self._revisions.check_revert(
            target_root, revision_ids, writer=writer, related=related
        )

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
        return self._revisions.revert(target_root, revision_ids, writer=writer, related=related)

    def archived(self, target_root: Path) -> list[ArchivedSkill]:
        """Return the packages in a home's archive, newest first."""
        return self._archives.archived(target_root)

    def archived_skill(self, target_root: Path, skill_name: str) -> ArchivedSkill | None:
        """Return the newest archive entry of *skill_name* in a home, if any.

        Includes an absorbed Skill whose archived files were purged
        (``available`` false).
        """
        return self._archives.archived_skill(target_root, skill_name)

    def restore(
        self, target_root: Path, archive_id: str, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Move an archived package back into the home under its Skill name.

        Refused while a Skill with that name exists in the home.
        """
        return self._archives.restore(target_root, archive_id, writer=writer)

    def purge(self, target_root: Path, archive_id: str) -> ArchivedSkill:
        """Permanently delete one archived package; return what it was."""
        return self._archives.purge(target_root, archive_id)

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

    def _resolved_skill_dir(self, target_root: Path, skill_name: str) -> Path:
        """Resolve the loaded identity before a deletion or its protection check."""
        named = self._skill_dir(target_root, skill_name)
        registry = SkillRegistry.load(named.parent)
        try:
            package = registry.get(skill_name).path.parent
        except KeyError:
            raise SkillAuthoringError(
                f"Nothing changed. Skill '{skill_name}' not found in this home's loaded Skills. "
                "Check its name in SKILL.md before deleting it."
            ) from None
        if sum(diagnostic.name == skill_name for diagnostic in registry.diagnostics()) > 1:
            raise SkillAuthoringError(
                f"Nothing changed. Several packages declare Skill '{skill_name}'. "
                "Ask the user to give each package a distinct name, then retry."
            )
        resolved = self._existing_skill_dir(target_root, package.name)
        if named != resolved and is_file_strict(named / SKILL_FILENAME):
            raise SkillAuthoringError(
                f"Nothing changed. Skill '{skill_name}' is loaded from folder '{package.name}', "
                f"but folder '{skill_name}' contains another Skill. "
                "Ask the user to align the folder names with their SKILL.md names, then retry."
            )
        return resolved

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
    "SkillReference",
    "SkillRevertConflictError",
    "SkillRevertIncompleteError",
    "SkillRevision",
    "SkillWriteResult",
    "SkillWriter",
]


__all__ = [
    "ArchivedSkill",
    "HUMAN_WRITER",
    "MAX_CHANGE_TEXT_BYTES",
    "PROVENANCE_SOURCE_KEY",
    "SKILL_ARCHIVE_MAX_BYTES",
    "SkillAuthor",
    "SkillAuthoringError",
    "SkillFileChange",
    "SkillFileChangeKind",
    "SkillMergeAccessError",
    "SkillProtectedError",
    "SkillProtection",
    "SkillRevertConflictError",
    "SkillRevertIncompleteError",
    "SkillWriteResult",
    "SkillWriter",
    "normalize_skill_file_path",
]
