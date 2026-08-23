"""Tests for package-root scan support in the skill loader."""

from pathlib import Path

from core.skills.skills import SKILL_ORIGIN_AGENT, SKILL_ORIGIN_GLOBAL, SkillRegistry


def write_skill(skills_dir: Path, directory_name: str, description: str) -> Path:
    skill_dir = skills_dir / directory_name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {directory_name}\ndescription: {description}\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    return skill_file


class TestPackageRoots:
    def test_root_containing_skill_md_contributes_exactly_one_package(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "deploy", "Ship the app.")
        write_skill(tmp_path / "deploy" / "scripts", "inner", "Must not leak.")

        registry = SkillRegistry.load(
            tmp_path / "deploy",
            origins=[SKILL_ORIGIN_AGENT],
        )

        assert [skill.name for skill in registry.list_all()] == ["deploy"]
        assert registry.get("deploy").origin == SKILL_ORIGIN_AGENT

    def test_package_root_without_skill_md_is_empty(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-skill"
        plain.mkdir()

        registry = SkillRegistry.load(plain)

        assert registry.list_all() == []

    def test_missing_package_root_is_empty(self, tmp_path: Path) -> None:
        registry = SkillRegistry.load(tmp_path / "gone")

        assert registry.list_all() == []
        assert registry.diagnostics() == []

    def test_package_root_mixed_with_regular_roots_keeps_order(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        owner_home = tmp_path / "owner-home"
        write_skill(home, "shared-skill", "Shared into me.")
        write_skill(owner_home, "shared-skill", "The owner's copy.")

        registry = SkillRegistry.load(
            owner_home,
            extra_dirs=[home / "shared-skill"],
            origins=[SKILL_ORIGIN_AGENT, SKILL_ORIGIN_AGENT],
        )

        # First-found-wins: the receiver's own copy precedes the shared package.
        assert registry.get("shared-skill").description == "The owner's copy."

    def test_shared_package_collision_with_global_is_recorded(self, tmp_path: Path) -> None:
        own = tmp_path / "own"
        global_dir = tmp_path / "global"
        write_skill(own, "deploy", "Own copy.")
        write_skill(global_dir, "deploy", "Global copy.")

        registry = SkillRegistry.load(
            own,
            extra_dirs=[global_dir],
            origins=[SKILL_ORIGIN_AGENT, SKILL_ORIGIN_GLOBAL],
        )

        assert registry.get("deploy").description == "Own copy."
        duplicates = [
            diagnostic
            for diagnostic in registry.invalid_diagnostics()
            if "Duplicate skill name 'deploy'" in " ".join(diagnostic.warnings)
        ]
        assert len(duplicates) == 1
