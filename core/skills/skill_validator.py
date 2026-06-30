"""Lenient validation helpers for local skill front matter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_SKILL_NAME_LENGTH = 64
MALFORMED_YAML_FALLBACK_WARNING = (
    "YAML front matter was repaired by quoting scalar values with colons."
)

# Fragment (no anchors) for a skill name that the `/name` and `$name` chat triggers
# (core.chat.tool_dispatch's trigger regexes) can actually match: a leading letter or
# digit, then up to MAX_SKILL_NAME_LENGTH - 1 more letters/digits/``-``/``_``. The
# trigger regexes are built from this same fragment, and SkillAuthoringService enforces
# SKILL_NAME_TRIGGER_PATTERN as a hard requirement, so a newly authored skill is always
# trigger-compatible. The loader stays lenient (see the charset warning below) so an
# already-existing on-disk skill with an unusual name keeps loading unchanged.
SKILL_NAME_CHARSET_FRAGMENT = rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_SKILL_NAME_LENGTH - 1}}}"
SKILL_NAME_TRIGGER_PATTERN = re.compile(f"^{SKILL_NAME_CHARSET_FRAGMENT}$")
# Charset only, no length bound, so a name that is merely too long is not also (and
# misleadingly) reported as having bad characters by the warning below.
_SKILL_NAME_SAFE_CHARSET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_SCALAR_WITH_COLON_PATTERN = re.compile(r"^(?P<key>[A-Za-z0-9_-]+):(?P<space>\s+)(?P<value>.+)$")
_QUOTED_OR_STRUCTURED_PREFIXES = ('"', "'", "[", "{", "&", "*", "!", ">", "|", "#")


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


def validate_skill_metadata(
    fields: Any,
    *,
    directory_name: str,
    skill_file: Path,
    parse_warnings: list[str] | None = None,
) -> ValidationResult:
    """Validate parsed skill metadata and return loadability plus warnings."""

    warnings = list(parse_warnings or [])
    if not isinstance(fields, dict):
        return ValidationResult(
            valid=False,
            warnings=[*warnings, f"Invalid YAML front matter in {skill_file}: expected a mapping."],
        )

    name = _field_to_string(fields.get("name"))
    description = _field_to_string(fields.get("description"))

    if not name:
        return ValidationResult(
            valid=False,
            warnings=[*warnings, f"Skill metadata missing name: {skill_file}"],
        )
    if not description:
        return ValidationResult(
            valid=False,
            warnings=[*warnings, f"Skill metadata missing description: {skill_file}"],
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

    return ValidationResult(valid=True, warnings=warnings)


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
