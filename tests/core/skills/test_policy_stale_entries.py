"""Tests for stale Skill Policy entries and shared-package resolution."""

import json
from pathlib import Path

from core.skills.policy import POLICY_SCHEMA_VERSION, SkillPolicy, SkillPolicyService
from core.skills.skills import find_skill_package_dir
from core.storage.storage import StorageManager


def write_skill(skills_dir: Path, directory_name: str, description: str) -> None:
    skill_dir = skills_dir / directory_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {directory_name}\ndescription: {description}\n---\n\nUse it.\n",
        encoding="utf-8",
    )


class TestStaleEntries:
    def test_policy_keeps_unknown_owners_and_names_as_written(self, tmp_path: Path) -> None:
        storage = StorageManager(data_dir=tmp_path / "data")
        policy_file = tmp_path / "data" / "skills" / "policy.json"
        policy_file.parent.mkdir(parents=True)
        document = {
            "version": POLICY_SCHEMA_VERSION,
            "disabled": [],
            "shared": {
                "ghost-agent": ["deploy"],
                "main": ["notes", "vanished-skill"],
            },
        }
        policy_file.write_text(json.dumps(document), encoding="utf-8")
        service = SkillPolicyService(storage)

        # Staleness is a resolution-time concern (the resolver knows the live
        # roster); the policy file itself keeps the entries untouched.
        assert service.load() == SkillPolicy(
            disabled=frozenset(),
            shared={
                "ghost-agent": frozenset({"deploy"}),
                "main": frozenset({"notes", "vanished-skill"}),
            },
        )
        assert service.validation_diagnostics() == []

        service.set_disabled("deploy", disabled=True)

        reloaded = json.loads(policy_file.read_text(encoding="utf-8"))
        assert reloaded["shared"] == document["shared"]

    def test_missing_package_resolves_to_none(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_skill(home, "notes", "Still there.")

        assert find_skill_package_dir(home, "notes") == home / "notes"
        assert find_skill_package_dir(home, "vanished-skill") is None

    def test_missing_home_resolves_to_none(self, tmp_path: Path) -> None:
        assert find_skill_package_dir(tmp_path / "gone", "anything") is None
