"""Tests for the local skill metadata registry."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.skills.skills import SkillMetadata, SkillRegistry, _scan_skill_resources


def write_skill(skills_dir: Path, directory_name: str, metadata: str) -> Path:
    skill_dir = skills_dir / directory_name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(metadata, encoding="utf-8")
    return skill_file


class TestSkillMetadata:
    def test_fields(self) -> None:
        path = Path("/skills/coder/SKILL.md")
        skill = SkillMetadata(
            name="coder",
            description="Handle coding tasks.",
            path=path,
            license="MIT",
            compatibility={"vbot": ">=0.1"},
            metadata={"owner": "test"},
            allowed_tools=["read"],
        )

        assert skill.name == "coder"
        assert skill.description == "Handle coding tasks."
        assert skill.path == path
        assert skill.license == "MIT"
        assert skill.compatibility == {"vbot": ">=0.1"}
        assert skill.metadata == {"owner": "test"}
        assert skill.allowed_tools == ["read"]

    def test_frozen(self) -> None:
        skill = SkillMetadata(
            name="coder",
            description="Handle coding tasks.",
            path=Path("/skills/coder/SKILL.md"),
        )

        with pytest.raises(FrozenInstanceError):
            skill.name = "changed"  # type: ignore[misc]


class TestSkillRegistryLoad:
    def test_loads_skill_metadata_from_yaml_front_matter(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill_file = write_skill(
            skills_dir,
            "agent-cli",
            """---
name: agent-cli
description: Delegate coding tasks to an external CLI.
license: MIT
compatibility:
  vbot: ">=0.1"
metadata:
  owner: tests
allowed-tools:
  - read
---

# Agent CLI
""",
        )

        registry = SkillRegistry.load(skills_dir)
        skill = registry.get("agent-cli")

        assert skill.name == "agent-cli"
        assert skill.description == "Delegate coding tasks to an external CLI."
        assert skill.path == skill_file.resolve()
        assert skill.license == "MIT"
        assert skill.compatibility == {"vbot": ">=0.1"}
        assert skill.metadata == {"owner": "tests"}
        assert skill.allowed_tools == ["read"]

    def test_loads_folded_multiline_description(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "research",
            """---
name: research
description: >
  Find source material for a task.
  Summarize the relevant facts.
---

# Research
""",
        )

        registry = SkillRegistry.load(skills_dir)
        skill = registry.get("research")

        assert skill.description == "Find source material for a task. Summarize the relevant facts."

    def test_missing_skills_directory_loads_empty_registry(self, tmp_path: Path) -> None:
        registry = SkillRegistry.load(tmp_path / "missing-skills")

        assert registry.list_all() == []
        assert registry.diagnostics() == []

    def test_ignores_non_skill_directories_and_files(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "README.md").write_text("not a skill", encoding="utf-8")
        (skills_dir / "empty-dir").mkdir()

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        assert registry.diagnostics() == []

    def test_duplicate_skill_name_first_found_wins_with_diagnostic(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        first_file = write_skill(
            skills_dir,
            "first",
            """---
name: duplicate
description: First skill.
---
""",
        )
        second_file = write_skill(
            skills_dir,
            "second",
            """---
name: duplicate
description: Second skill.
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.get("duplicate").path == first_file.resolve()
        invalid = registry.invalid_diagnostics()
        assert invalid[0].path == second_file.resolve()
        assert "Duplicate skill name 'duplicate' rejected" in invalid[0].warnings[-1]

    def test_missing_front_matter_is_preserved_as_invalid_diagnostic(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill_file = write_skill(skills_dir, "broken", "# Broken\n")

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        invalid = registry.invalid_diagnostics()
        assert invalid[0].path == skill_file.resolve()
        assert "missing front matter" in invalid[0].warnings[0]

    def test_missing_required_metadata_is_preserved_as_invalid_diagnostic(
        self, tmp_path: Path
    ) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "broken",
            """---
name: broken
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        invalid = registry.invalid_diagnostics()
        assert invalid[0].name == "broken"
        assert "missing description" in invalid[0].warnings[0]

    def test_loadable_skill_with_warning_remains_available(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "directory-name",
            """---
name: metadata-name
description: Loadable with a warning.
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.get("metadata-name").description == "Loadable with a warning."
        assert registry.warnings_for("metadata-name") == [
            "Skill name 'metadata-name' does not match directory name 'directory-name'."
        ]
        assert registry.invalid_diagnostics() == []

    def test_malformed_yaml_fallback_loads_with_warning(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "careful",
            """---
name: careful
description: Use mode: careful
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.get("careful").description == "Use mode: careful"
        assert registry.warnings_for("careful") == [
            "YAML front matter was repaired by quoting scalar values with colons."
        ]
        assert registry.invalid_diagnostics() == []

    def test_invalid_yaml_is_preserved_as_invalid_diagnostic(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "broken-yaml",
            """---
name: broken-yaml
description: [unterminated
---
""",
        )

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []
        invalid = registry.invalid_diagnostics()
        assert invalid[0].name == "broken-yaml"
        assert "Invalid YAML front matter" in invalid[0].warnings[0]

    def test_extra_scan_directories_are_loaded_after_primary_dir(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        extra_dir = tmp_path / "extra-skills"
        primary_file = write_skill(
            skills_dir,
            "primary",
            "---\nname: shared\ndescription: Primary.\n---\n",
        )
        write_skill(extra_dir, "extra", "---\nname: extra\ndescription: Extra.\n---\n")
        duplicate_file = write_skill(
            extra_dir,
            "shared-duplicate",
            "---\nname: shared\ndescription: Duplicate.\n---\n",
        )

        registry = SkillRegistry.load(skills_dir, extra_dirs=[extra_dir])

        assert [skill.name for skill in registry.list_all()] == ["extra", "shared"]
        assert registry.get("shared").path == primary_file.resolve()
        invalid = registry.invalid_diagnostics()
        assert invalid[0].path == duplicate_file.resolve()
        assert "Duplicate skill name 'shared' rejected" in invalid[0].warnings[-1]


class TestSkillRegistryGet:
    def test_get_missing_skill_raises_key_error(self, tmp_path: Path) -> None:
        registry = SkillRegistry.load(tmp_path / "missing-skills")

        with pytest.raises(KeyError, match="Skill not found: missing"):
            registry.get("missing")


class TestSkillRegistryListAll:
    def test_list_all_returns_skills_sorted_by_name(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(skills_dir, "z-dir", "---\nname: zeta\ndescription: Last.\n---\n")
        write_skill(skills_dir, "a-dir", "---\nname: alpha\ndescription: First.\n---\n")

        registry = SkillRegistry.load(skills_dir)
        skills = registry.list_all()

        assert [skill.name for skill in skills] == ["alpha", "zeta"]


class TestSkillRegistryFilterAllowed:
    def test_wildcard_allows_all_skills(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        skills = registry.filter_allowed(["*"])

        assert [skill.name for skill in skills] == ["agent-cli", "research"]

    def test_empty_allowlist_allows_no_skills(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        assert registry.filter_allowed([]) == []

    def test_explicit_allowlist_filters_by_name(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        skills = registry.filter_allowed(["research"])

        assert [skill.name for skill in skills] == ["research"]

    def test_unknown_allowed_skill_is_ignored(self, tmp_path: Path) -> None:
        registry = registry_with_two_skills(tmp_path)

        skills = registry.filter_allowed(["missing", "agent-cli"])

        assert [skill.name for skill in skills] == ["agent-cli"]


class TestScanSkillResources:
    def test_returns_scripts_and_references_relative_paths(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "coder"
        (skill_dir / "scripts" / "nested").mkdir(parents=True)
        (skill_dir / "references").mkdir()
        (skill_dir / "scripts" / "run.py").write_text("", encoding="utf-8")
        (skill_dir / "scripts" / "nested" / "helper.py").write_text("", encoding="utf-8")
        (skill_dir / "references" / "guide.md").write_text("", encoding="utf-8")
        (skill_dir / "notes" / "ignored.md").parent.mkdir()
        (skill_dir / "notes" / "ignored.md").write_text("", encoding="utf-8")

        resources = _scan_skill_resources(skill_dir)

        assert resources == [
            "scripts/nested/helper.py",
            "scripts/run.py",
            "references/guide.md",
        ]

    def test_returns_empty_list_when_resource_directories_are_missing(self, tmp_path: Path) -> None:
        assert _scan_skill_resources(tmp_path / "skills" / "coder") == []


def registry_with_two_skills(tmp_path: Path) -> SkillRegistry:
    skills_dir = tmp_path / "skills"
    write_skill(
        skills_dir,
        "agent-cli",
        """---
name: agent-cli
description: Delegate coding tasks.
---
""",
    )
    write_skill(
        skills_dir,
        "research",
        """---
name: research
description: Find source material.
---
""",
    )
    return SkillRegistry.load(skills_dir)
