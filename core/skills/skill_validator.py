"""Lenient validation helpers for local skill front matter."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.utils.text_pages import TEXT_PAGE_MAX_BYTES, TEXT_PAGE_MAX_LINES

FRONT_MATTER_DELIMITER = "---"
MAX_SKILL_NAME_LENGTH = 64
# The Agent Skills specification's limit. Every request's Skill list carries each
# description in full, so a longer one only warns and is never cut.
MAX_SKILL_DESCRIPTION_LENGTH = 1024
MALFORMED_YAML_FALLBACK_WARNING = (
    "YAML front matter was repaired by quoting scalar values with colons."
)
SIMPLE_KEY_VALUE_FALLBACK_WARNING = (
    "YAML front matter was read with the simple key: value fallback."
)
MISSING_FRONT_MATTER_FALLBACK_WARNING = (
    "SKILL.md has no complete YAML front matter; using the full file as instructions."
)

# Fragment (no anchors) for a skill name that the `/name` and `$name` chat triggers
# (the trigger regexes below) can actually match: a leading letter or digit, then up
# to MAX_SKILL_NAME_LENGTH - 1 more letters/digits/``-``/``_``. SkillAuthoringService
# enforces SKILL_NAME_TRIGGER_PATTERN as a hard requirement, so a newly authored skill
# is always trigger-compatible. The loader stays lenient (see the charset warning
# below) so an already-existing on-disk skill with an unusual name keeps loading
# unchanged.
SKILL_NAME_CHARSET_FRAGMENT = rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_SKILL_NAME_LENGTH - 1}}}"
SKILL_NAME_TRIGGER_PATTERN = re.compile(f"^{SKILL_NAME_CHARSET_FRAGMENT}$")
# ``/name`` at the very start of a message, and ``$name`` anywhere in it.
SKILL_SLASH_TRIGGER_PATTERN = re.compile(rf"^/({SKILL_NAME_CHARSET_FRAGMENT})(?=\s|$)")
SKILL_INLINE_TRIGGER_PATTERN = re.compile(rf"\$({SKILL_NAME_CHARSET_FRAGMENT})")
# Charset only, no length bound, so a name that is merely too long is not also (and
# misleadingly) reported as having bad characters by the warning below.
_SKILL_NAME_SAFE_CHARSET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_SCALAR_WITH_COLON_PATTERN = re.compile(r"^(?P<key>[A-Za-z0-9_-]+):(?P<space>\s+)(?P<value>.+)$")
_QUOTED_OR_STRUCTURED_PREFIXES = ('"', "'", "[", "{", "&", "*", "!", ">", "|", "#")


def triggered_skill_names(content: str) -> list[str]:
    """Return the Skill names a message triggers, in order and without repeats.

    A message triggers the Skill of a leading ``/name`` and of every ``$name``.
    Chat activation and every check of what an automation's text triggers share
    this one grammar.
    """
    names: list[str] = []
    slash_match = SKILL_SLASH_TRIGGER_PATTERN.search(content)
    if slash_match:
        names.append(slash_match.group(1))
    for inline_match in SKILL_INLINE_TRIGGER_PATTERN.finditer(content):
        name = inline_match.group(1)
        if name not in names:
            names.append(name)
    return names


def rename_skill_triggers(content: str, name: str, new_name: str) -> str:
    """Return ``content`` with every trigger of the Skill ``name`` naming ``new_name``.

    The triggers are exactly those :func:`triggered_skill_names` reads: a leading
    ``/name`` and every ``$name``. Triggers of other Skills, including longer
    names that start with ``name``, stay as they are.
    """
    slash_match = SKILL_SLASH_TRIGGER_PATTERN.match(content)
    if slash_match is not None and slash_match.group(1) == name:
        content = f"/{new_name}{content[slash_match.end() :]}"
    return SKILL_INLINE_TRIGGER_PATTERN.sub(
        lambda match: f"${new_name}" if match.group(1) == name else match.group(0), content
    )


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating one skill's parsed YAML metadata."""

    valid: bool
    warnings: list[str] = field(default_factory=list)


def repair_colon_scalars(front_matter: str) -> str:
    """Quote simple unquoted scalar values that contain colon-space sequences."""

    repaired_lines: list[str] = []
    for line in front_matter.splitlines():
        repaired_lines.append(_repair_colon_scalar_line(line))
    return "\n".join(repaired_lines)


def split_skill_document(content: str) -> tuple[str, str, list[str]]:
    """Split a Skill document without making front matter a loadability gate."""

    if not isinstance(content, str):
        raise TypeError("SKILL.md content must be a string")
    if content.startswith("\ufeff"):
        content = content[1:]
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        return "", content, [MISSING_FRONT_MATTER_FALLBACK_WARNING]

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :]), []

    return "", content, [MISSING_FRONT_MATTER_FALLBACK_WARNING]


def parse_skill_front_matter(front_matter: str) -> tuple[Any, list[str]]:
    """Parse YAML, then degrade to simple ``key: value`` extraction."""

    if not front_matter.strip():
        return {}, []
    try:
        return yaml.safe_load(front_matter) or {}, []
    except yaml.YAMLError:
        repaired = repair_colon_scalars(front_matter)
        if repaired != front_matter:
            try:
                return yaml.safe_load(repaired) or {}, [MALFORMED_YAML_FALLBACK_WARNING]
            except yaml.YAMLError:
                pass
        return _parse_simple_key_values(front_matter), [SIMPLE_KEY_VALUE_FALLBACK_WARNING]


def normalize_and_validate_skill_metadata(
    fields: Any,
    *,
    directory_name: str,
    skill_file: Path,
    body: str = "",
    parse_warnings: list[str] | None = None,
) -> tuple[dict[str, Any], ValidationResult]:
    """Return usable metadata plus non-blocking diagnostics for local Skills."""

    warnings = list(parse_warnings or [])
    if not isinstance(fields, dict):
        warnings.append(
            f"Invalid YAML front matter in {skill_file}: expected a mapping; "
            "using directory and body fallbacks."
        )
        normalized: dict[str, Any] = {}
    else:
        normalized = dict(fields)

    name = _field_to_string(normalized.get("name"))
    if not name:
        name = directory_name
        normalized["name"] = name
        warnings.append(f"Skill metadata missing name; using directory name '{directory_name}'.")

    description = _field_to_string(normalized.get("description"))
    if not description:
        description = _infer_description(body)
        normalized["description"] = description
        if description:
            warnings.append("Skill metadata missing description; using the first body text line.")
        else:
            warnings.append(
                "Skill metadata missing description and no body text line was available; "
                "using an empty description."
            )

    if name != directory_name:
        warnings.append(f"Skill name '{name}' does not match directory name '{directory_name}'.")
    if len(name) > MAX_SKILL_NAME_LENGTH:
        warnings.append(f"Skill name '{name}' is longer than {MAX_SKILL_NAME_LENGTH} characters.")
    if not _SKILL_NAME_SAFE_CHARSET_PATTERN.match(name):
        warnings.append(
            f"Skill name '{name}' uses characters other than letters, digits, '-', or "
            "'_' (or does not start with a letter or digit); it cannot be triggered "
            "with /name or $name, only loaded by name with the skill tool."
        )
    if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
        warnings.append(
            f"Skill description has {len(description):,} characters, more than "
            f"{MAX_SKILL_DESCRIPTION_LENGTH:,}. Every Agent request lists it in full; "
            "keep it to what the Skill is for and when to load it, and move details "
            "into the instructions."
        )
    oversized_instructions = _oversized_instructions_warning(body)
    if oversized_instructions:
        warnings.append(oversized_instructions)

    return normalized, ValidationResult(valid=True, warnings=warnings)


def _oversized_instructions_warning(body: str) -> str | None:
    """Warn when loading the Skill cannot return its instructions in one page.

    The skill tool pages the stripped body like ``read`` pages a file; lines are
    counted as ``str.splitlines`` splits them, which matches the pager for the
    LF, CRLF and CR line breaks of real documents.
    """
    instructions = body.strip()
    size = len(instructions.encode("utf-8"))
    lines = len(instructions.splitlines())
    if size <= TEXT_PAGE_MAX_BYTES and lines <= TEXT_PAGE_MAX_LINES:
        return None
    return (
        f"SKILL.md instructions do not fit one page ({math.ceil(size / 1024)} KB, {lines} lines; "
        f"a page holds at most {TEXT_PAGE_MAX_BYTES // 1024} KB and {TEXT_PAGE_MAX_LINES} "
        "lines). Loading the Skill returns only the first page, and the rest takes "
        "further calls to the skill tool. Move reference material into separate "
        "files, such as references/*.md, that the instructions name."
    )


def _parse_simple_key_values(front_matter: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for line in front_matter.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        fields[key] = value
    return fields


def _infer_description(body: str) -> str:
    unclosed_front_matter = body.lstrip().startswith(FRONT_MATTER_DELIMITER)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped == FRONT_MATTER_DELIMITER or stripped.startswith("#"):
            continue
        if unclosed_front_matter and ":" in stripped:
            continue
        return stripped
    return ""


def _repair_colon_scalar_line(line: str) -> str:
    match = _SCALAR_WITH_COLON_PATTERN.match(line)
    if match is None:
        return line

    value = match.group("value").strip()
    if ": " not in value or value.startswith(_QUOTED_OR_STRUCTURED_PREFIXES):
        return line

    return f"{match.group('key')}:{match.group('space')}{json.dumps(value)}"


def _field_to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
