"""Tests for skill metadata validation."""

from pathlib import Path

from core.skills.skill_validator import (
    MALFORMED_YAML_FALLBACK_WARNING,
    MAX_SKILL_NAME_LENGTH,
    MISSING_FRONT_MATTER_FALLBACK_WARNING,
    SIMPLE_KEY_VALUE_FALLBACK_WARNING,
    normalize_and_validate_skill_metadata,
    parse_skill_front_matter,
    repair_colon_scalars,
    split_skill_document,
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


def test_missing_name_uses_directory_name() -> None:
    skill_file = Path("/skills/broken/SKILL.md")

    fields, result = normalize_and_validate_skill_metadata(
        {"description": "Useful."},
        directory_name="broken",
        skill_file=skill_file,
    )

    assert fields["name"] == "broken"
    assert result.valid is True
    assert result.warnings == ["Skill metadata missing name; using directory name 'broken'."]


def test_missing_description_uses_first_body_text_line() -> None:
    skill_file = Path("/skills/broken/SKILL.md")

    fields, result = normalize_and_validate_skill_metadata(
        {"name": "broken"},
        directory_name="broken",
        skill_file=skill_file,
        body="# Heading\n\nRun the recovery steps.\n",
    )

    assert fields["description"] == "Run the recovery steps."
    assert result.valid is True
    assert result.warnings == [
        "Skill metadata missing description; using the first body text line."
    ]


def test_non_mapping_yaml_uses_directory_and_body_fallbacks() -> None:
    skill_file = Path("/skills/broken/SKILL.md")

    fields, result = normalize_and_validate_skill_metadata(
        ["not", "a", "mapping"],
        directory_name="broken",
        skill_file=skill_file,
        body="Do the useful thing.",
    )

    assert fields == {"name": "broken", "description": "Do the useful thing."}
    assert result.valid is True
    assert result.warnings == [
        f"Invalid YAML front matter in {skill_file}: expected a mapping; "
        "using directory and body fallbacks.",
        "Skill metadata missing name; using directory name 'broken'.",
        "Skill metadata missing description; using the first body text line.",
    ]


def test_repair_colon_scalars_quotes_values_that_contain_colon_space() -> None:
    repaired = repair_colon_scalars("name: helper\ndescription: Use mode: careful")

    assert repaired == 'name: helper\ndescription: "Use mode: careful"'


def test_malformed_yaml_fallback_warning_constant_is_specific() -> None:
    assert MALFORMED_YAML_FALLBACK_WARNING == (
        "YAML front matter was repaired by quoting scalar values with colons."
    )


def test_malformed_yaml_uses_simple_key_value_fallback() -> None:
    fields, warnings = parse_skill_front_matter(
        "name: broken-yaml\ndescription: Use mode: careful\nbroken: [unterminated"
    )

    assert fields == {
        "name": "broken-yaml",
        "description": "Use mode: careful",
        "broken": "[unterminated",
    }
    assert warnings == [SIMPLE_KEY_VALUE_FALLBACK_WARNING]


def test_missing_front_matter_uses_whole_document_as_body() -> None:
    content = "# Deploy\n\nRun the deploy steps.\n"

    front_matter, body, warnings = split_skill_document(content)

    assert front_matter == ""
    assert body == content
    assert warnings == [MISSING_FRONT_MATTER_FALLBACK_WARNING]


def test_leading_utf8_bom_does_not_hide_front_matter() -> None:
    content = "\ufeff---\nname: deploy\ndescription: Deploy it.\n---\n\nRun it.\n"

    front_matter, body, warnings = split_skill_document(content)

    assert front_matter == "name: deploy\ndescription: Deploy it."
    assert body == "\nRun it."
    assert warnings == []
