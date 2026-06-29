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

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

from core.skills.requirements import (
    REQUIREMENTS_METADATA_KEY,
    RequirementParseError,
    parse_vbot_requirements,
)
from core.skills.skill_validator import (
    MALFORMED_YAML_FALLBACK_WARNING,
    repair_colon_scalars,
    validate_skill_metadata,
)
from core.skills.skills import (
    FRONT_MATTER_DELIMITER,
    RESOURCE_DIRECTORIES,
    SKILL_FILENAME,
)
from core.utils.errors import VBotError

# Provenance keys recorded under ``metadata.vbot`` beside ``requirements``.
PROVENANCE_AUTHOR_KEY = "author"
PROVENANCE_SOURCE_KEY = "source"

SkillAuthor = Literal["agent", "human"]
_VALID_AUTHORS: tuple[SkillAuthor, ...] = ("agent", "human")


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


class SkillAuthoringService:
    """Validated, path-confined write operations for local skills.

    Operates on an already-resolved *target root* (a scope's skills directory):
    ``create`` / ``edit`` / ``patch`` / ``delete`` for the skill itself, plus
    ``write_file`` / ``remove_file`` for its ``scripts/`` / ``references/`` support
    files. Scope→root resolution is the caller's job; this service confines every
    path strictly under the target root, refuses protected (bundled) roots,
    validates the skill document, and stamps provenance into ``metadata.vbot``.

    ``protected_roots`` are the roots a write must never target (the bundled
    ``resources/skills`` directory); a target at or under any of them is refused.
    """

    def __init__(self, protected_roots: Sequence[Path] = ()) -> None:
        self._protected_roots = [self._resolve(root) for root in protected_roots]

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
        """Write a support file under the skill's ``scripts/`` or ``references/``."""
        skill_dir = self._existing_skill_dir(target_root, skill_name)
        resource_path = self._resource_path(skill_dir, relative_path)
        resource_path.parent.mkdir(parents=True, exist_ok=True)
        resource_path.write_text(content, encoding="utf-8")
        return SkillWriteResult(name=skill_name, operation="write_file", path=resource_path)

    def remove_file(
        self, target_root: Path, skill_name: str, relative_path: str
    ) -> SkillWriteResult:
        """Remove a support file under the skill's ``scripts/`` or ``references/``."""
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
    ) -> tuple[str, Any]:
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


def _validate_skill_name(skill_name: str) -> None:
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise SkillAuthoringError("Skill name must be a non-empty string.")
    if skill_name in {".", ".."} or ".." in skill_name or "\x00" in skill_name:
        raise SkillAuthoringError(f"Illegal skill name: {skill_name!r}")
    if "/" in skill_name or "\\" in skill_name:
        raise SkillAuthoringError(f"Skill name must be a single path segment: {skill_name!r}")
    if skill_name != skill_name.strip():
        raise SkillAuthoringError("Skill name must not have leading or trailing whitespace.")


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
    "SkillWriteResult",
]
