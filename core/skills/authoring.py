"""Validated, path-safe direct writes for local vBot Skills.

Every Skill-authoring surface resolves its writable scope to a target root before
calling this service. The service validates Skill documents, confines every path
to that root, protects bundled roots, stamps provenance, and performs each text
write atomically. It never resolves scopes or writes Project Skills.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Literal

import yaml

from core.skills._installation import SkillInstallResult, install_package
from core.skills._packages import MAX_DOWNLOAD_BYTES, PackageError, is_redirect
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

PROVENANCE_AUTHOR_KEY = "author"
PROVENANCE_SOURCE_KEY = "source"
SKILL_ARCHIVE_MAX_BYTES = MAX_DOWNLOAD_BYTES

SkillAuthor = Literal["agent", "human"]
_VALID_AUTHORS: tuple[SkillAuthor, ...] = ("agent", "human")


class SkillAuthoringError(VBotError):
    """Raised when a Skill write fails validation or path confinement."""

    def __init__(self, message: str, *, diagnostics: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.diagnostics: list[str] = list(diagnostics) if diagnostics else [message]


@dataclass(frozen=True)
class SkillWriteResult:
    """Outcome of one successful direct Skill mutation."""

    name: str
    operation: str
    path: Path
    warnings: list[str] = field(default_factory=list)


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
        author: SkillAuthor,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Create ``<target_root>/<skill_name>/SKILL.md``."""
        with self._write_lock:
            skill_dir = self._skill_dir(target_root, skill_name)
            if skill_dir.exists():
                raise SkillAuthoringError(f"Skill '{skill_name}' already exists.")
            skill_file = skill_dir / SKILL_FILENAME
            document, validation = self._prepare_document(
                content,
                skill_name=skill_name,
                skill_file=skill_file,
                author=author,
                source=source,
            )
            skill_dir.mkdir(parents=True, exist_ok=False)
            try:
                _atomic_write_styled(skill_file, document, "\n")
            except OSError:
                shutil.rmtree(skill_dir, ignore_errors=True)
                raise
            return SkillWriteResult(
                name=skill_name,
                operation="create",
                path=skill_file,
                warnings=validation.warnings,
            )

    def install(
        self,
        target_root: Path,
        source: str,
        *,
        path: str | None = None,
        ref: str | None = None,
        replace: bool = False,
        dry_run: bool = False,
        archive: bytes | None = None,
        expected_sha256: str | None = None,
    ) -> SkillInstallResult:
        """Install a source or uploaded archive (source is its filename), never executing it."""
        self._reject_protected(self._resolve(target_root))
        try:
            return install_package(
                self,
                target_root,
                source,
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
        author: SkillAuthor,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Replace an existing Skill's complete ``SKILL.md``."""
        with self._write_lock:
            skill_file = self._existing_skill_file(target_root, skill_name)
            file_ending = _detect_line_ending(_read_raw_text(skill_file))
            document, validation = self._prepare_document(
                content,
                skill_name=skill_name,
                skill_file=skill_file,
                author=author,
                source=source,
            )
            _atomic_write_styled(skill_file, document, file_ending)
            return SkillWriteResult(
                name=skill_name,
                operation="edit",
                path=skill_file,
                warnings=validation.warnings,
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
        author: SkillAuthor,
        source: str | None = None,
    ) -> SkillWriteResult:
        """Rewrite ``SKILL.md`` or one UTF-8 support file through ``edit``.

        ``edit`` receives the current text with LF line endings while the write
        lock is held and returns the new text; raising aborts without writing.
        A rewritten ``SKILL.md`` is validated and stamped like ``edit``. The file
        keeps its line-ending style.
        """
        with self._write_lock:
            target, normalized = self._existing_text_file(target_root, skill_name, relative_path)
            current = _read_text_file(target, normalized)
            file_ending = _detect_line_ending(current)
            updated = _normalize_newlines(edit(_normalize_newlines(current)))
            warnings: list[str] = []
            if normalized == SKILL_FILENAME:
                updated, validation = self._prepare_document(
                    updated,
                    skill_name=skill_name,
                    skill_file=target,
                    author=author,
                    source=source,
                )
                warnings = validation.warnings
            _atomic_write_styled(target, updated, file_ending)
            return SkillWriteResult(
                name=skill_name,
                operation="rewrite",
                path=target,
                warnings=warnings,
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

    def delete(self, target_root: Path, skill_name: str) -> SkillWriteResult:
        """Delete a Skill directory and all support files."""
        with self._write_lock:
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            shutil.rmtree(skill_dir)
            return SkillWriteResult(name=skill_name, operation="delete", path=skill_dir)

    def write_file(
        self,
        target_root: Path,
        skill_name: str,
        relative_path: str,
        content: str,
    ) -> SkillWriteResult:
        """Create or replace one UTF-8 support file."""
        if not isinstance(content, str):
            raise SkillAuthoringError("Support file content must be a string.")
        with self._write_lock:
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            resource_path = self._resource_path(skill_dir, relative_path)
            file_ending = "\n"
            if resource_path.is_file():
                try:
                    existing = _read_raw_text(resource_path)
                except UnicodeDecodeError:
                    existing = ""
                if existing:
                    file_ending = _detect_line_ending(existing)
            styled = _to_line_ending(_normalize_newlines(content), file_ending)
            resource_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(resource_path, styled.encode("utf-8"))
            return SkillWriteResult(
                name=skill_name,
                operation="write_file",
                path=resource_path,
            )

    def remove_file(
        self,
        target_root: Path,
        skill_name: str,
        relative_path: str,
    ) -> SkillWriteResult:
        """Remove one support file."""
        with self._write_lock:
            skill_dir = self._existing_skill_dir(target_root, skill_name)
            resource_path = self._resource_path(skill_dir, relative_path)
            if not resource_path.is_file():
                raise SkillAuthoringError(f"Support file not found: {relative_path}")
            resource_path.unlink()
            _remove_empty_resource_parents(resource_path.parent, skill_dir)
            return SkillWriteResult(
                name=skill_name,
                operation="remove_file",
                path=resource_path,
            )

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
        if author not in _VALID_AUTHORS:
            raise SkillAuthoringError(f"Unknown provenance author: {author!r}")

        front_matter, body, document_warnings = split_skill_document(content)
        fields, parse_warnings = parse_skill_front_matter(front_matter)
        fields, result = normalize_and_validate_skill_metadata(
            fields,
            directory_name=skill_name,
            skill_file=skill_file,
            body=body,
            parse_warnings=[*document_warnings, *parse_warnings],
        )
        if not result.valid:
            raise SkillAuthoringError(
                "Skill metadata is invalid.",
                diagnostics=result.warnings,
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


def _reject_redirect(path: Path) -> None:
    try:
        redirect = is_redirect(path)
    except FileNotFoundError:
        return
    if redirect:
        raise SkillAuthoringError("Refusing to write through a symlink or junction Skill path.")


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
    "PROVENANCE_AUTHOR_KEY",
    "PROVENANCE_SOURCE_KEY",
    "SkillAuthor",
    "SkillAuthoringError",
    "SkillAuthoringService",
    "SkillWriteResult",
]
