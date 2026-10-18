"""Skill documents, support paths and styled atomic text writes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from core.skills._authoring_types import (
    MAX_CHANGE_TEXT_BYTES,
    PROVENANCE_SOURCE_KEY,
    SkillAuthor,
    SkillAuthoringError,
)
from core.skills._history import PROVENANCE_AUTHOR_KEY, document_fields
from core.skills._packages import is_redirect
from core.skills.requirements import REQUIREMENTS_METADATA_KEY
from core.skills.skill_validator import (
    FRONT_MATTER_DELIMITER,
    MAX_SKILL_NAME_LENGTH,
    SKILL_NAME_TRIGGER_PATTERN,
)
from core.skills.skills import RESOURCE_DIRECTORIES, SKILL_FILENAME
from core.utils.atomic import atomic_write_bytes
from core.utils.ids import is_reserved_name, reserved_name_message


def _declared_name(package: Path) -> str:
    name = document_fields(package / SKILL_FILENAME).get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillAuthoringError("The archived package declares no Skill name.")
    return name.strip()


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
    # One line per scalar keeps the author's lines, so a patch copied from them matches.
    front = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True, width=float("inf")).strip()
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
