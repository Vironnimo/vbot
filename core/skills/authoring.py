"""Validated, path-safe write core for authoring local skills.

One write core shared by every skill-authoring surface (the agent ``skill_manage``
tool, the ``/learn`` command, and the skill-mutation RPCs). Callers resolve a
*scope* (agent home, global, …) to an already-resolved **target root** path and
hand it in; this service owns the rest: validation, strict path confinement under
that root, protected-root refusal, and provenance stamping. It never resolves
scopes itself and never writes the repo or the bundled resources.

The write gate is intentionally **stricter** than the lenient loader: where the
loader merely warns (e.g. skill name not matching its directory), authoring
hard-fails, so an authored skill always has a predictable directory == name and
clean front matter.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Literal, cast
from uuid import uuid4

import yaml

from core.skills.requirements import (
    REQUIREMENTS_METADATA_KEY,
    RequirementParseError,
    parse_vbot_requirements,
)
from core.skills.skill_validator import (
    MALFORMED_YAML_FALLBACK_WARNING,
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_TRIGGER_PATTERN,
    ValidationResult,
    repair_colon_scalars,
    validate_skill_metadata,
)
from core.skills.skills import (
    FRONT_MATTER_DELIMITER,
    RESOURCE_DIRECTORIES,
    SKILL_FILENAME,
)
from core.utils.atomic import atomic_write_text
from core.utils.errors import VBotError

# Provenance keys recorded under ``metadata.vbot`` beside ``requirements``.
PROVENANCE_AUTHOR_KEY = "author"
PROVENANCE_SOURCE_KEY = "source"

SkillAuthor = Literal["agent", "human"]
_VALID_AUTHORS: tuple[SkillAuthor, ...] = ("agent", "human")
SkillDraftMode = Literal["create", "update"]
_VALID_DRAFT_MODES: tuple[SkillDraftMode, ...] = ("create", "update")
_DRAFT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_DRAFT_METADATA_FILENAME = "draft.json"
_DRAFT_PACKAGE_DIRNAME = "package"
_ARCHIVE_SKILLS_DIRNAME = "skills"
_BINARY_SAMPLE_BYTES = 8192


class SkillAuthoringError(VBotError):
    """Raised when a skill write is rejected (validation, path, or scope).

    ``diagnostics`` carries the human-readable rejection reasons so every write
    surface (tool result, RPC error, ``/learn``) can forward the same messages
    without re-deriving them.
    """

    def __init__(self, message: str, *, diagnostics: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.diagnostics: list[str] = list(diagnostics) if diagnostics else [message]


@dataclass(frozen=True)
class SkillWriteResult:
    """Outcome of a successful skill write operation."""

    name: str
    operation: str
    path: Path
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillDraft:
    """One isolated package draft bound to an actor, scope root, and skill."""

    id: str
    name: str
    mode: SkillDraftMode
    path: Path


@dataclass(frozen=True)
class SkillPackageFile:
    """One regular file in a vBot Skill package manifest."""

    path: str
    kind: str
    size: int
    sha256: str
    media_type: str
    binary: bool
    executable: bool


@dataclass(frozen=True)
class SkillPackageInspection:
    """Read-only package view returned for published Skills and drafts."""

    name: str
    files: list[SkillPackageFile]
    skill_md: str | None
    diagnostics: list[str] = field(default_factory=list)
    selected_path: str | None = None
    selected_content: str | None = None


class SkillAuthoringService:
    """Validated, path-confined write operations for local skills.

    Operates on an already-resolved *target root* (a scope's Skills directory).
    Package authoring uses an isolated draft with complete-package validation and
    publish/abort semantics. Direct document and support-file methods remain for
    RPC/accessor compatibility. Scope→root resolution is the caller's job; this
    service confines every path strictly under the target root, refuses protected
    bundled roots, validates the Skill document, and stamps vBot provenance.

    ``protected_roots`` are the roots a write must never target (the bundled
    ``resources/skills`` directory); a target at or under any of them is refused.
    """

    def __init__(
        self,
        protected_roots: Sequence[Path] = (),
        *,
        drafts_root: Path | None = None,
        archive_root: Path | None = None,
    ) -> None:
        self._protected_roots = [self._resolve(root) for root in protected_roots]
        self._drafts_root = self._resolve(drafts_root) if drafts_root is not None else None
        self._archive_root = self._resolve(archive_root) if archive_root is not None else None
        self._package_lock = RLock()

    # -- package draft lifecycle -------------------------------------------

    def begin_draft(
        self,
        target_root: Path,
        skill_name: str,
        *,
        mode: SkillDraftMode,
        actor_id: str,
        author: SkillAuthor,
        source: str | None = None,
    ) -> SkillDraft:
        """Create an isolated new-package or update-package draft."""
        if mode not in _VALID_DRAFT_MODES:
            raise SkillAuthoringError(f"Draft mode must be one of: {', '.join(_VALID_DRAFT_MODES)}")
        if author not in _VALID_AUTHORS:
            raise SkillAuthoringError(f"Unknown provenance author: {author!r}")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise SkillAuthoringError("Draft actor id must be a non-empty string.")

        with self._package_lock:
            skill_dir = self._skill_dir(target_root, skill_name)
            if mode == "create" and skill_dir.exists():
                raise SkillAuthoringError(f"Skill '{skill_name}' already exists.")
            if mode == "update" and not skill_dir.is_dir():
                raise SkillAuthoringError(f"Skill '{skill_name}' not found.")

            draft_id = uuid4().hex
            draft_dir = self._draft_dir(draft_id)
            package_dir = draft_dir / _DRAFT_PACKAGE_DIRNAME
            try:
                draft_dir.mkdir(parents=True, exist_ok=False)
                if mode == "update":
                    shutil.copytree(skill_dir, package_dir, symlinks=True)
                else:
                    package_dir.mkdir()
                metadata = {
                    "id": draft_id,
                    "name": skill_name,
                    "mode": mode,
                    "target_root": str(self._resolve(target_root)),
                    "actor_id": actor_id,
                    "author": author,
                    "source": source,
                }
                atomic_write_text(
                    draft_dir / _DRAFT_METADATA_FILENAME,
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                )
            except (OSError, TypeError, ValueError):
                shutil.rmtree(draft_dir, ignore_errors=True)
                raise
            return SkillDraft(id=draft_id, name=skill_name, mode=mode, path=package_dir)

    def inspect_published(
        self,
        target_root: Path,
        skill_name: str,
        *,
        selected_path: str | None = None,
    ) -> SkillPackageInspection:
        """Return a manifest and optional text-file content for one published Skill."""
        with self._package_lock:
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            return self._inspect_package(skill_dir, skill_name, selected_path=selected_path)

    def inspect_draft(
        self,
        target_root: Path,
        draft_id: str,
        *,
        actor_id: str,
        selected_path: str | None = None,
    ) -> SkillPackageInspection:
        """Return a manifest and optional text-file content for one owned draft."""
        with self._package_lock:
            metadata, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            return self._inspect_package(
                package_dir,
                _metadata_string(metadata, "name"),
                selected_path=selected_path,
            )

    def put_draft_text(
        self,
        target_root: Path,
        draft_id: str,
        relative_path: str,
        content: str,
        *,
        actor_id: str,
        executable: bool = False,
    ) -> SkillPackageFile:
        """Write one UTF-8 text file into an owned draft."""
        if not isinstance(content, str):
            raise SkillAuthoringError("Draft file content must be a string.")
        with self._package_lock:
            _, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            target = self._package_path(package_dir, relative_path)
            atomic_write_text(target, content)
            self._apply_executable(target, relative_path, executable)
            return self._manifest_file(package_dir, target)

    def copy_draft_file(
        self,
        target_root: Path,
        draft_id: str,
        relative_path: str,
        source_path: Path,
        *,
        actor_id: str,
        executable: bool | None = None,
    ) -> SkillPackageFile:
        """Copy one regular source file byte-for-byte into an owned draft."""
        source = self._resolve(source_path)
        if not source.is_file() or source.is_symlink():
            raise SkillAuthoringError(f"Draft source is not a regular file: {source}")
        with self._package_lock:
            _, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            target = self._package_path(package_dir, relative_path)
            _atomic_copy_file(source, target)
            self._apply_executable(target, relative_path, executable is True)
            return self._manifest_file(package_dir, target)

    def patch_draft_text(
        self,
        target_root: Path,
        draft_id: str,
        relative_path: str,
        old_string: str,
        new_string: str,
        *,
        actor_id: str,
    ) -> SkillPackageFile:
        """Apply one unique text replacement inside an owned draft."""
        if old_string == new_string:
            raise SkillAuthoringError("patch old_string and new_string must differ.")
        with self._package_lock:
            _, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            target = self._package_path(package_dir, relative_path)
            try:
                current = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise SkillAuthoringError(f"Cannot patch binary file: {relative_path}") from exc
            except FileNotFoundError as exc:
                raise SkillAuthoringError(f"Draft file not found: {relative_path}") from exc
            occurrences = current.count(old_string)
            if occurrences == 0:
                raise SkillAuthoringError(f"patch old_string not found in {relative_path}.")
            if occurrences > 1:
                raise SkillAuthoringError(
                    f"patch old_string is not unique in {relative_path} "
                    f"({occurrences} matches); add more context."
                )
            atomic_write_text(target, current.replace(old_string, new_string))
            return self._manifest_file(package_dir, target)

    def remove_draft_file(
        self,
        target_root: Path,
        draft_id: str,
        relative_path: str,
        *,
        actor_id: str,
    ) -> None:
        """Remove one support file from an owned draft."""
        if _normalized_package_path(relative_path) == SKILL_FILENAME:
            raise SkillAuthoringError(f"{SKILL_FILENAME} cannot be removed from a draft.")
        with self._package_lock:
            _, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            target = self._package_path(package_dir, relative_path)
            if not target.is_file() or target.is_symlink():
                raise SkillAuthoringError(f"Draft file not found: {relative_path}")
            target.unlink()
            _remove_empty_resource_parents(target.parent, package_dir)

    def validate_draft(
        self,
        target_root: Path,
        draft_id: str,
        *,
        actor_id: str,
    ) -> SkillPackageInspection:
        """Validate the complete vBot package shape and Skill metadata."""
        with self._package_lock:
            metadata, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            skill_name = _metadata_string(metadata, "name")
            _, validation = self._validated_package_document(package_dir, skill_name, metadata)
            inspection = self._inspect_package(package_dir, skill_name)
            return SkillPackageInspection(
                name=inspection.name,
                files=inspection.files,
                skill_md=inspection.skill_md,
                diagnostics=list(validation.warnings),
            )

    def commit_draft(
        self,
        target_root: Path,
        draft_id: str,
        *,
        actor_id: str,
    ) -> SkillWriteResult:
        """Validate and publish an owned draft as one complete package swap."""
        with self._package_lock:
            metadata, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            skill_name = _metadata_string(metadata, "name")
            mode = _metadata_draft_mode(metadata)
            document, validation = self._validated_package_document(
                package_dir, skill_name, metadata
            )
            atomic_write_text(package_dir / SKILL_FILENAME, document)
            target = self._skill_dir(target_root, skill_name)
            if mode == "create" and target.exists():
                raise SkillAuthoringError(f"Skill '{skill_name}' already exists.")
            if mode == "update" and not target.is_dir():
                raise SkillAuthoringError(
                    f"Skill '{skill_name}' disappeared while its draft was open."
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            backup = self._draft_dir(draft_id) / "previous-package"
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(package_dir, target)
            except OSError:
                if backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.rmtree(self._draft_dir(draft_id), ignore_errors=True)
            return SkillWriteResult(
                name=skill_name,
                operation="commit",
                path=target,
                warnings=list(validation.warnings),
            )

    def abort_draft(self, target_root: Path, draft_id: str, *, actor_id: str) -> SkillDraft:
        """Discard an owned draft without touching the published Skill."""
        with self._package_lock:
            metadata, package_dir = self._load_draft(target_root, draft_id, actor_id=actor_id)
            draft = SkillDraft(
                id=draft_id,
                name=_metadata_string(metadata, "name"),
                mode=_metadata_draft_mode(metadata),
                path=package_dir,
            )
            shutil.rmtree(self._draft_dir(draft_id))
            return draft

    def archive_skill(
        self,
        target_root: Path,
        skill_name: str,
        *,
        archive_namespace: Sequence[str],
    ) -> SkillWriteResult:
        """Move a published Skill into a unique, recoverable archive directory."""
        with self._package_lock:
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            archive_root = self._require_archive_root()
            namespace = [_safe_archive_segment(part) for part in archive_namespace]
            archive_parent = archive_root.joinpath(_ARCHIVE_SKILLS_DIRNAME, *namespace, skill_name)
            archive_parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            archive_dir = archive_parent / f"{timestamp}-{uuid4().hex[:8]}"
            shutil.move(str(skill_dir), str(archive_dir))
            return SkillWriteResult(name=skill_name, operation="archive", path=archive_dir)

    def _draft_dir(self, draft_id: str) -> Path:
        if not isinstance(draft_id, str) or not _DRAFT_ID_PATTERN.fullmatch(draft_id):
            raise SkillAuthoringError(f"Invalid Skill draft id: {draft_id!r}")
        drafts_root = self._require_drafts_root()
        draft_dir = self._resolve(drafts_root / draft_id)
        if draft_dir.parent != drafts_root:
            raise SkillAuthoringError(f"Invalid Skill draft id: {draft_id!r}")
        return draft_dir

    def _load_draft(
        self,
        target_root: Path,
        draft_id: str,
        *,
        actor_id: str,
    ) -> tuple[dict[str, Any], Path]:
        draft_dir = self._draft_dir(draft_id)
        metadata_path = draft_dir / _DRAFT_METADATA_FILENAME
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SkillAuthoringError(f"Skill draft not found: {draft_id}") from exc
        except (json.JSONDecodeError, OSError) as exc:
            raise SkillAuthoringError(f"Skill draft metadata is invalid: {draft_id}") from exc
        if not isinstance(metadata, dict):
            raise SkillAuthoringError(f"Skill draft metadata is invalid: {draft_id}")
        if _metadata_string(metadata, "id") != draft_id:
            raise SkillAuthoringError(f"Skill draft identity mismatch: {draft_id}")
        if _metadata_string(metadata, "actor_id") != actor_id:
            raise SkillAuthoringError("Skill draft belongs to a different agent.")
        recorded_root = self._resolve(Path(_metadata_string(metadata, "target_root")))
        requested_root = self._resolve(target_root)
        if recorded_root != requested_root:
            raise SkillAuthoringError("Skill draft belongs to a different scope.")
        self._reject_protected(requested_root)
        _validate_skill_name(_metadata_string(metadata, "name"))
        _metadata_draft_mode(metadata)
        package_dir = draft_dir / _DRAFT_PACKAGE_DIRNAME
        if not package_dir.is_dir():
            raise SkillAuthoringError(f"Skill draft package is missing: {draft_id}")
        return metadata, package_dir

    def _package_path(self, package_dir: Path, relative_path: str) -> Path:
        normalized = _normalized_package_path(relative_path)
        parts = PurePosixPath(normalized).parts
        if normalized != SKILL_FILENAME and (
            not parts or parts[0] not in RESOURCE_DIRECTORIES or len(parts) < 2
        ):
            allowed = ", ".join(f"{name}/" for name in RESOURCE_DIRECTORIES)
            raise SkillAuthoringError(
                f"Skill package files must be {SKILL_FILENAME} or live under {allowed}"
            )
        package_resolved = self._resolve(package_dir)
        candidate = self._resolve(package_resolved.joinpath(*parts))
        if candidate == package_resolved or not _is_within(candidate, package_resolved):
            raise SkillAuthoringError(f"Illegal Skill package path: {relative_path}")
        return candidate

    @staticmethod
    def _apply_executable(target: Path, relative_path: str, executable: bool) -> None:
        normalized = _normalized_package_path(relative_path)
        if executable and not normalized.startswith("scripts/"):
            raise SkillAuthoringError("Only files under scripts/ may be marked executable.")
        mode = target.stat().st_mode
        execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        target.chmod(mode | execute_bits if executable else mode & ~execute_bits)

    def _validated_package_document(
        self,
        package_dir: Path,
        skill_name: str,
        metadata: dict[str, Any],
    ) -> tuple[str, ValidationResult]:
        diagnostics = _package_shape_diagnostics(package_dir)
        skill_file = package_dir / SKILL_FILENAME
        try:
            content = skill_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            diagnostics.append(f"Skill package is missing {SKILL_FILENAME}.")
            content = ""
        except UnicodeDecodeError:
            diagnostics.append(f"{SKILL_FILENAME} must be UTF-8 text.")
            content = ""
        if diagnostics:
            raise SkillAuthoringError(
                "Skill package is invalid.",
                diagnostics=diagnostics,
            )
        author = _metadata_author(metadata)
        source_value = metadata.get("source")
        source = source_value if isinstance(source_value, str) else None
        return self._prepare_document(
            content,
            skill_name=skill_name,
            skill_file=skill_file,
            author=author,
            source=source,
        )

    def _inspect_package(
        self,
        package_dir: Path,
        skill_name: str,
        *,
        selected_path: str | None = None,
    ) -> SkillPackageInspection:
        diagnostics = _package_shape_diagnostics(package_dir)
        files: list[SkillPackageFile] = []
        for candidate in sorted(
            package_dir.rglob("*"),
            key=lambda item: _manifest_sort_key(package_dir, item),
        ):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            files.append(self._manifest_file(package_dir, candidate))

        skill_md: str | None = None
        skill_file = package_dir / SKILL_FILENAME
        if skill_file.is_file() and not skill_file.is_symlink():
            try:
                skill_md = skill_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                diagnostics.append(f"{SKILL_FILENAME} must be UTF-8 text.")
        else:
            diagnostics.append(f"Skill package is missing {SKILL_FILENAME}.")

        selected_content: str | None = None
        normalized_selected: str | None = None
        if selected_path is not None:
            target = self._package_path(package_dir, selected_path)
            normalized_selected = target.relative_to(package_dir.resolve()).as_posix()
            if not target.is_file() or target.is_symlink():
                raise SkillAuthoringError(f"Skill package file not found: {selected_path}")
            try:
                selected_content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                selected_content = None

        return SkillPackageInspection(
            name=skill_name,
            files=files,
            skill_md=skill_md,
            diagnostics=_deduplicate(diagnostics),
            selected_path=normalized_selected,
            selected_content=selected_content,
        )

    @staticmethod
    def _manifest_file(package_dir: Path, path: Path) -> SkillPackageFile:
        relative_path = path.relative_to(package_dir.resolve()).as_posix()
        digest = hashlib.sha256()
        sample = b""
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if len(sample) < _BINARY_SAMPLE_BYTES:
                    needed = _BINARY_SAMPLE_BYTES - len(sample)
                    sample += chunk[:needed]
                digest.update(chunk)
                size += len(chunk)
        try:
            sample.decode("utf-8")
            binary = b"\x00" in sample
        except UnicodeDecodeError:
            binary = True
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        top_level = PurePosixPath(relative_path).parts[0]
        kind = "instructions" if relative_path == SKILL_FILENAME else top_level
        return SkillPackageFile(
            path=relative_path,
            kind=kind,
            size=size,
            sha256=digest.hexdigest(),
            media_type=media_type,
            binary=binary,
            executable=bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
        )

    def _require_drafts_root(self) -> Path:
        if self._drafts_root is None:
            raise SkillAuthoringError("Skill package drafts are not configured.")
        self._drafts_root.mkdir(parents=True, exist_ok=True)
        return self._drafts_root

    def _require_archive_root(self) -> Path:
        if self._archive_root is None:
            raise SkillAuthoringError("Skill archive is not configured.")
        self._archive_root.mkdir(parents=True, exist_ok=True)
        return self._archive_root

    # -- skill document operations ------------------------------------------

    def create(
        self,
        target_root: Path,
        skill_name: str,
        content: str,
        *,
        author: SkillAuthor,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Create a new skill directory ``<target_root>/<skill_name>/SKILL.md``."""
        skill_dir = self._skill_dir(target_root, skill_name)
        if skill_dir.exists():
            raise SkillAuthoringError(f"Skill '{skill_name}' already exists.")
        skill_file = skill_dir / SKILL_FILENAME
        document, result = self._prepare_document(
            content, skill_name=skill_name, skill_file=skill_file, author=author, source=source
        )
        skill_dir.mkdir(parents=True, exist_ok=False)
        skill_file.write_text(document, encoding="utf-8")
        return SkillWriteResult(
            name=skill_name, operation="create", path=skill_file, warnings=result.warnings
        )

    def edit(
        self,
        target_root: Path,
        skill_name: str,
        content: str,
        *,
        author: SkillAuthor,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Rewrite an existing skill's ``SKILL.md`` in full."""
        skill_file = self._existing_skill_file(target_root, skill_name)
        document, result = self._prepare_document(
            content, skill_name=skill_name, skill_file=skill_file, author=author, source=source
        )
        skill_file.write_text(document, encoding="utf-8")
        return SkillWriteResult(
            name=skill_name, operation="edit", path=skill_file, warnings=result.warnings
        )

    def patch(
        self,
        target_root: Path,
        skill_name: str,
        old_string: str,
        new_string: str,
        *,
        author: SkillAuthor,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Apply a single unique ``old_string`` → ``new_string`` edit to ``SKILL.md``."""
        skill_file = self._existing_skill_file(target_root, skill_name)
        if old_string == new_string:
            raise SkillAuthoringError("patch old_string and new_string must differ.")
        current = skill_file.read_text(encoding="utf-8")
        occurrences = current.count(old_string)
        if occurrences == 0:
            raise SkillAuthoringError("patch old_string not found in SKILL.md.")
        if occurrences > 1:
            raise SkillAuthoringError(
                f"patch old_string is not unique ({occurrences} matches); add more context."
            )
        patched = current.replace(old_string, new_string)
        document, result = self._prepare_document(
            patched, skill_name=skill_name, skill_file=skill_file, author=author, source=source
        )
        skill_file.write_text(document, encoding="utf-8")
        return SkillWriteResult(
            name=skill_name, operation="patch", path=skill_file, warnings=result.warnings
        )

    def delete(self, target_root: Path, skill_name: str) -> SkillWriteResult:
        """Delete a skill directory and all its support files."""
        skill_dir = self._existing_skill_dir(target_root, skill_name)
        shutil.rmtree(skill_dir)
        return SkillWriteResult(name=skill_name, operation="delete", path=skill_dir)

    # -- support file operations --------------------------------------------

    def write_file(
        self, target_root: Path, skill_name: str, relative_path: str, content: str
    ) -> SkillWriteResult:
        """Write a support file under the skill's ``scripts/``, ``references/``, or ``assets/``."""
        skill_dir = self._existing_skill_dir(target_root, skill_name)
        resource_path = self._resource_path(skill_dir, relative_path)
        resource_path.parent.mkdir(parents=True, exist_ok=True)
        resource_path.write_text(content, encoding="utf-8")
        return SkillWriteResult(name=skill_name, operation="write_file", path=resource_path)

    def remove_file(
        self, target_root: Path, skill_name: str, relative_path: str
    ) -> SkillWriteResult:
        """Remove a support file under the skill's ``scripts/``, ``references/``, or ``assets/``."""
        skill_dir = self._existing_skill_dir(target_root, skill_name)
        resource_path = self._resource_path(skill_dir, relative_path)
        if not resource_path.is_file():
            raise SkillAuthoringError(f"Support file not found: {relative_path}")
        resource_path.unlink()
        return SkillWriteResult(name=skill_name, operation="remove_file", path=resource_path)

    # -- path resolution / confinement --------------------------------------

    def _skill_dir(self, target_root: Path, skill_name: str) -> Path:
        _validate_skill_name(skill_name)
        root = self._resolve(target_root)
        self._reject_protected(root)
        skill_dir = self._resolve(root / skill_name)
        if skill_dir.parent != root:
            raise SkillAuthoringError(f"Illegal skill name escapes target root: {skill_name!r}")
        return skill_dir

    def _existing_skill_dir(self, target_root: Path, skill_name: str) -> Path:
        skill_dir = self._skill_dir(target_root, skill_name)
        if not skill_dir.is_dir():
            raise SkillAuthoringError(f"Skill '{skill_name}' not found.")
        return skill_dir

    def _existing_skill_file(self, target_root: Path, skill_name: str) -> Path:
        skill_file = self._existing_skill_dir(target_root, skill_name) / SKILL_FILENAME
        if not skill_file.is_file():
            raise SkillAuthoringError(f"Skill '{skill_name}' has no {SKILL_FILENAME}.")
        return skill_file

    def _resource_path(self, skill_dir: Path, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise SkillAuthoringError("Support file path must be a non-empty string.")
        raw = PurePosixPath(relative_path.replace("\\", "/"))
        if raw.is_absolute() or any(part == ".." for part in raw.parts):
            raise SkillAuthoringError(f"Illegal support file path: {relative_path}")
        parts = raw.parts
        if not parts or parts[0] not in RESOURCE_DIRECTORIES:
            allowed = " or ".join(f"{name}/" for name in RESOURCE_DIRECTORIES)
            raise SkillAuthoringError(f"Support files must live under {allowed}")
        skill_dir_resolved = self._resolve(skill_dir)
        candidate = self._resolve(skill_dir_resolved.joinpath(*parts))
        if candidate == skill_dir_resolved or not _is_within(candidate, skill_dir_resolved):
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

    # -- document validation + provenance -----------------------------------

    def _prepare_document(
        self,
        content: str,
        *,
        skill_name: str,
        skill_file: Path,
        author: SkillAuthor,
        source: str | None,
    ) -> tuple[str, ValidationResult]:
        if author not in _VALID_AUTHORS:
            raise SkillAuthoringError(f"Unknown provenance author: {author!r}")

        front_matter, body = _split_front_matter(content)
        fields, parse_warnings = _parse_front_matter(front_matter)
        result = validate_skill_metadata(
            fields,
            directory_name=skill_name,
            skill_file=skill_file,
            parse_warnings=parse_warnings,
        )
        if not result.valid:
            raise SkillAuthoringError("Skill metadata is invalid.", diagnostics=result.warnings)
        if not isinstance(fields, dict):  # guaranteed by validate_skill_metadata; narrows the type
            raise SkillAuthoringError("Skill front matter must be a mapping.")

        declared_name = str(fields.get("name", "")).strip()
        if declared_name != skill_name:
            raise SkillAuthoringError(
                f"Skill name '{declared_name}' must match its directory name '{skill_name}'."
            )

        metadata = fields.get("metadata")
        try:
            parse_vbot_requirements(metadata if isinstance(metadata, dict) else {})
        except RequirementParseError as exc:
            raise SkillAuthoringError(str(exc), diagnostics=[str(exc)]) from exc

        stamped = _with_provenance(fields, author=author, source=source)
        return _assemble_document(stamped, body), result


def _normalized_package_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise SkillAuthoringError("Skill package path must be a non-empty string.")
    raw = PurePosixPath(relative_path.replace("\\", "/"))
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise SkillAuthoringError(f"Illegal Skill package path: {relative_path}")
    return raw.as_posix()


def _package_shape_diagnostics(package_dir: Path) -> list[str]:
    diagnostics: list[str] = []
    if not package_dir.is_dir():
        return ["Skill package directory is missing."]
    for candidate in sorted(package_dir.rglob("*")):
        relative = candidate.relative_to(package_dir).as_posix()
        parts = PurePosixPath(relative).parts
        if candidate.is_symlink():
            diagnostics.append(f"Skill package may not contain symbolic links: {relative}")
            continue
        if relative == SKILL_FILENAME:
            if not candidate.is_file():
                diagnostics.append(f"{SKILL_FILENAME} must be a regular file.")
            continue
        if not parts or parts[0] not in RESOURCE_DIRECTORIES:
            diagnostics.append(f"Unsupported top-level Skill package path: {relative}")
            continue
        if len(parts) == 1 and not candidate.is_dir():
            diagnostics.append(f"Skill resource root must be a directory: {relative}")
        elif not candidate.is_dir() and not candidate.is_file():
            diagnostics.append(f"Skill package entry must be a regular file: {relative}")
    return _deduplicate(diagnostics)


def _manifest_sort_key(package_dir: Path, path: Path) -> tuple[int, str]:
    relative = path.relative_to(package_dir).as_posix()
    return (0 if relative == SKILL_FILENAME else 1, relative)


def _metadata_string(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise SkillAuthoringError(f"Skill draft metadata field is invalid: {key}")
    return value


def _metadata_draft_mode(metadata: dict[str, Any]) -> SkillDraftMode:
    mode = metadata.get("mode")
    if mode not in _VALID_DRAFT_MODES:
        raise SkillAuthoringError("Skill draft metadata field is invalid: mode")
    return cast(SkillDraftMode, mode)


def _metadata_author(metadata: dict[str, Any]) -> SkillAuthor:
    author = metadata.get("author")
    if author not in _VALID_AUTHORS:
        raise SkillAuthoringError("Skill draft metadata field is invalid: author")
    return cast(SkillAuthor, author)


def _safe_archive_segment(value: str) -> str:
    if not isinstance(value, str) or not SKILL_NAME_TRIGGER_PATTERN.fullmatch(value):
        raise SkillAuthoringError(f"Invalid Skill archive namespace segment: {value!r}")
    return value


def _atomic_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _remove_empty_resource_parents(parent: Path, package_dir: Path) -> None:
    package_root = package_dir.resolve()
    current = parent
    while current != package_root and _is_within(current.resolve(), package_root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _deduplicate(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _validate_skill_name(skill_name: str) -> None:
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise SkillAuthoringError("Skill name must be a non-empty string.")
    if skill_name in {".", ".."} or ".." in skill_name or "\x00" in skill_name:
        raise SkillAuthoringError(f"Illegal skill name: {skill_name!r}")
    if "/" in skill_name or "\\" in skill_name:
        raise SkillAuthoringError(f"Skill name must be a single path segment: {skill_name!r}")
    if skill_name != skill_name.strip():
        raise SkillAuthoringError("Skill name must not have leading or trailing whitespace.")
    # Stricter than the lenient loader (which only warns): an authored skill's name
    # must stay within what the `/name`/`$name` chat triggers can match, so a newly
    # created skill is never silently untriggerable.
    if not SKILL_NAME_TRIGGER_PATTERN.match(skill_name):
        raise SkillAuthoringError(
            "Skill name must start with a letter or digit, contain only letters, "
            f"digits, '-', or '_', and be at most {MAX_SKILL_NAME_LENGTH} characters "
            f"long: {skill_name!r}"
        )


def _split_front_matter(content: str) -> tuple[str, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise SkillAuthoringError("SKILL.md must start with YAML front matter ('---').")
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillAuthoringError("SKILL.md front matter is not closed with '---'.")


def _parse_front_matter(front_matter: str) -> tuple[Any, list[str]]:
    try:
        return yaml.safe_load(front_matter) or {}, []
    except yaml.YAMLError:
        repaired = repair_colon_scalars(front_matter)
        if repaired != front_matter:
            try:
                return yaml.safe_load(repaired) or {}, [MALFORMED_YAML_FALLBACK_WARNING]
            except yaml.YAMLError:
                pass
        raise SkillAuthoringError("SKILL.md front matter is not valid YAML.") from None


def _with_provenance(
    fields: dict[str, Any], *, author: SkillAuthor, source: str | None
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
    "PROVENANCE_AUTHOR_KEY",
    "PROVENANCE_SOURCE_KEY",
    "SkillAuthor",
    "SkillAuthoringError",
    "SkillAuthoringService",
    "SkillDraft",
    "SkillDraftMode",
    "SkillPackageFile",
    "SkillPackageInspection",
    "SkillWriteResult",
]
