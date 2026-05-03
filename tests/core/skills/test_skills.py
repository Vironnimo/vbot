"""Tests for the local skill metadata registry."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.skills.skills import SkillMetadata, SkillRegistry


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
        )

        assert skill.name == "coder"
        assert skill.description == "Handle coding tasks."
        assert skill.path == path

    def test_frozen(self) -> None:
        skill = SkillMetadata(
            name="coder",
            description="Handle coding tasks.",
            path=Path("/skills/coder/SKILL.md"),
        )

        with pytest.raises(FrozenInstanceError):
            skill.name = "changed"  # type: ignore[misc]


class TestSkillRegistryLoad:
    def test_loads_skill_metadata_from_front_matter(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill_file = write_skill(
            skills_dir,
            "agent-cli",
            """---
name: agent-cli
description: Delegate coding tasks to an external CLI.
---

# Agent CLI
""",
        )

        registry = SkillRegistry.load(skills_dir)
        skill = registry.get("agent-cli")

        assert skill.name == "agent-cli"
        assert skill.description == "Delegate coding tasks to an external CLI."
        assert skill.path == skill_file.resolve()

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

    def test_ignores_non_skill_directories_and_files(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "README.md").write_text("not a skill", encoding="utf-8")
        (skills_dir / "empty-dir").mkdir()

        registry = SkillRegistry.load(skills_dir)

        assert registry.list_all() == []

    def test_duplicate_skill_name_raises_value_error(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "first",
            """---
name: duplicate
description: First skill.
---
""",
        )
        write_skill(
            skills_dir,
            "second",
            """---
name: duplicate
description: Second skill.
---
""",
        )

        with pytest.raises(ValueError, match="Duplicate skill name: duplicate"):
            SkillRegistry.load(skills_dir)

    def test_missing_front_matter_raises_value_error(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(skills_dir, "broken", "# Broken\n")

        with pytest.raises(ValueError, match="missing front matter"):
            SkillRegistry.load(skills_dir)

    def test_missing_required_metadata_raises_value_error(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        write_skill(
            skills_dir,
            "broken",
            """---
name: broken
---
""",
        )

        with pytest.raises(ValueError, match="missing description"):
            SkillRegistry.load(skills_dir)


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
