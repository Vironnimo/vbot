"""Tests for SkillRegistry exclusion (the policy disable switch)."""

from pathlib import Path

from core.skills.skills import SKILL_ORIGIN_BUNDLED, SKILL_ORIGIN_GLOBAL, SkillRegistry


def write_skill(skills_dir: Path, directory_name: str, description: str) -> Path:
    skill_dir = skills_dir / directory_name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {directory_name}\ndescription: {description}\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    return skill_file


class TestExcludedNames:
    def test_excluded_name_is_invisible_to_every_read_answer(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "deploy", "Ship the app.")
        write_skill(tmp_path, "review", "Review code.")

        registry = SkillRegistry.load(
            tmp_path,
            origins=[SKILL_ORIGIN_GLOBAL],
            excluded_names={"deploy"},
        )

        assert {skill.name for skill in registry.list_all()} == {"review"}
        try:
            registry.get("deploy")
            raised = False
        except KeyError:
            raised = True
        assert raised
        assert [skill.name for skill in registry.filter_allowed(["*"])] == ["review"]
        assert registry.availability_for("deploy", ["*"]).state == "invalid"

    def test_excluded_skills_accessor_returns_hidden_metadata(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "deploy", "Ship the app.")

        registry = SkillRegistry.load(
            tmp_path,
            origins=[SKILL_ORIGIN_GLOBAL],
            excluded_names={"deploy"},
        )

        excluded = registry.excluded_skills()
        assert [skill.name for skill in excluded] == ["deploy"]
        assert excluded[0].origin == SKILL_ORIGIN_GLOBAL

    def test_unknown_excluded_names_are_ignored(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "deploy", "Ship the app.")

        registry = SkillRegistry.load(tmp_path, excluded_names={"ghost"})

        assert {skill.name for skill in registry.list_all()} == {"deploy"}
        assert registry.excluded_skills() == []

    def test_disabling_one_name_hides_colliding_origins_first_found_wins(
        self, tmp_path: Path
    ) -> None:
        global_dir = tmp_path / "global"
        bundled_dir = tmp_path / "bundled"
        write_skill(global_dir, "deploy", "Global copy.")
        write_skill(bundled_dir, "deploy", "Bundled copy.")

        registry = SkillRegistry.load(
            global_dir,
            extra_dirs=[bundled_dir],
            origins=[SKILL_ORIGIN_GLOBAL, SKILL_ORIGIN_BUNDLED],
            excluded_names={"deploy"},
        )

        # One disabled name is a master switch: both origin copies are hidden.
        assert registry.list_all() == []
        assert sorted(skill.name for skill in registry.excluded_skills()) == ["deploy"]

    def test_without_exclusion_the_registry_is_unchanged(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "deploy", "Ship the app.")

        registry = SkillRegistry.load(tmp_path)

        assert {skill.name for skill in registry.list_all()} == {"deploy"}
        assert registry.excluded_skills() == []
