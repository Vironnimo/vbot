"""Tests for skill metadata validation."""

from pathlib import Path

from core.skills.skill_validator import (
    MALFORMED_YAML_FALLBACK_WARNING,
    MAX_SKILL_NAME_LENGTH,
    repair_colon_scalars,
    validate_skill_metadata,
)


def test_name_directory_mismatch_is_warning() -> None:
    result = validate_skill_metadata(
        {"name": "metadata-name", "description": "Useful."},
        directory_name="directory-name",
        skill_file=Path("/skills/directory-name/SKILL.md"),
    )

    assert result.valid is True
    assert result.warnings == [
        "Skill name 'metadata-name' does not match directory name 'directory-name'."
    ]


def test_oversized_name_is_warning() -> None:
    name = "a" * (MAX_SKILL_NAME_LENGTH + 1)

    result = validate_skill_metadata(
        {"name": name, "description": "Useful."},
        directory_name=name,
        skill_file=Path("/skills/long/SKILL.md"),
    )

    assert result.valid is True
    assert result.warnings == [
        f"Skill name '{name}' is longer than {MAX_SKILL_NAME_LENGTH} characters."
    ]


def test_missing_description_is_invalid() -> None:
    skill_file = Path("/skills/broken/SKILL.md")

    result = validate_skill_metadata(
        {"name": "broken"},
        directory_name="broken",
        skill_file=skill_file,
    )

    assert result.valid is False
    assert result.warnings == [f"Skill metadata missing description: {skill_file}"]


def test_non_mapping_yaml_is_invalid() -> None:
    skill_file = Path("/skills/broken/SKILL.md")

    result = validate_skill_metadata(
        ["not", "a", "mapping"],
        directory_name="broken",
        skill_file=skill_file,
    )

    assert result.valid is False
    assert result.warnings == [f"Invalid YAML front matter in {skill_file}: expected a mapping."]


def test_repair_colon_scalars_quotes_values_that_contain_colon_space() -> None:
    repaired = repair_colon_scalars("name: helper\ndescription: Use mode: careful")

    assert repaired == 'name: helper\ndescription: "Use mode: careful"'


def test_malformed_yaml_fallback_warning_constant_is_specific() -> None:
    assert MALFORMED_YAML_FALLBACK_WARNING == (
        "YAML front matter was repaired by quoting scalar values with colons."
    )
