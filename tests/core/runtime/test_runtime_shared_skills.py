"""Tests for the shared-Skill layer in Runtime agent-aware registries."""

import logging
import re
from pathlib import Path

import pytest

from core.runtime.runtime import Runtime
from core.skills.skills import SKILL_ORIGIN_AGENT
from core.utils.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


def _write_test_skill(skill_root: Path, name: str, description: str) -> None:
    skill_dir = skill_root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


def _write_agent_skill(data_dir: Path, agent_id: str, name: str, description: str) -> None:
    _write_test_skill(data_dir / "agents" / agent_id / "skills", name, description)


def _names(registry) -> set[str]:
    return {skill.name for skill in registry.list_all()}


def test_shared_skill_reaches_receiver_as_own_layer(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        _write_agent_skill(data_dir, "main", "deploy", "Main's private playbook.")

        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
        runtime.invalidate_agent_skills(None)

        registry = runtime.skills_for(None, "two")
        assert "deploy" in _names(registry)
        # Receiver-facing origin: indistinguishable from its own skills.
        assert registry.get("deploy").origin == SKILL_ORIGIN_AGENT
    finally:
        runtime.stop()


def test_shared_skill_is_subject_to_the_receiver_allowlist(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two", allowed_skills=["unrelated"])
        _write_agent_skill(data_dir, "main", "deploy", "Main's private playbook.")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])

        registry = runtime.skills_for(None, "two")

        # Loaded, but filtered like any global skill — never always-allowed.
        assert [s.name for s in registry.filter_allowed(["unrelated"])] == []
        runtime.agents.update("two", allowed_skills=["deploy"])
        runtime.invalidate_agent_skills("two")
        registry = runtime.skills_for(None, "two")
        assert [s.name for s in registry.filter_allowed(["deploy"])] == ["deploy"]
    finally:
        runtime.stop()


def test_unshared_private_neighbors_stay_invisible(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        _write_agent_skill(data_dir, "main", "deploy", "Shared.")
        _write_agent_skill(data_dir, "main", "secret-notes", "Unshared neighbour.")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])

        registry = runtime.skills_for(None, "two")

        assert "deploy" in _names(registry)
        assert "secret-notes" not in _names(registry)
    finally:
        runtime.stop()


def test_owner_view_and_always_allowed_status_unchanged(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        _write_agent_skill(data_dir, "main", "deploy", "Main's private playbook.")
        before = runtime.skills_for(None, "main")

        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
        runtime.invalidate_agent_skills("main")
        after = runtime.skills_for(None, "main")

        assert _names(before) == _names(after)
        # Still always-allowed for the owner with an empty personal allowlist.
        assert [s.name for s in after.filter_allowed([])] == ["deploy"]
    finally:
        runtime.stop()


def test_unshare_drops_the_skill_from_receivers_live(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        _write_agent_skill(data_dir, "main", "deploy", "Shared.")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
        runtime.invalidate_agent_skills(None)
        assert "deploy" in _names(runtime.skills_for(None, "two"))

        runtime.skill_policy.set_shared("main", "deploy", shared=False)
        runtime.invalidate_agent_skills(None)

        assert "deploy" not in _names(runtime.skills_for(None, "two"))
    finally:
        runtime.stop()


def test_project_scoped_registry_without_identity_never_receives_shared(
    config: Config, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        _write_agent_skill(data_dir, "main", "deploy", "Shared.")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
        project = runtime.projects.create("p", "P", repo)

        # A config-agent run passes no identity id: the private-home boundary
        # stays identity-only, so the project bundle never carries shared Skills.
        assert "deploy" not in _names(runtime.skills_for(project.project_id))
    finally:
        runtime.stop()


def test_receiver_catalog_renders_shared_among_own_skills(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        receiver = runtime.agents.create("two", "Two")
        _write_agent_skill(data_dir, "main", "deploy", "Shared playbook.")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
        registry = runtime.skills_for(None, "two")

        catalog = runtime.system_prompts.render_skill_catalog(receiver, registry)

        # No new group and no provenance hint: the shared skill renders inside the
        # ordinary "Your own skills" group beside the bundled pool.
        labels = re.findall(r'<skill_group label="([^"]+)">', catalog.catalog_text)
        assert set(labels) <= {"Bundled skills", "Your global skills", "Your own skills"}
        own_group = catalog.catalog_text.split('label="Your own skills"', 1)[1]
        own_group = own_group.split("</skill_group>", 1)[0]
        assert "<name>deploy</name>" in own_group
    finally:
        runtime.stop()


def test_disabled_shared_skill_is_hidden_from_receivers_too(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        _write_agent_skill(data_dir, "main", "deploy", "Shared.")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
        runtime.invalidate_agent_skills(None)

        runtime.skill_policy.set_disabled("deploy", disabled=True)
        runtime.reload_skills()

        assert "deploy" not in _names(runtime.skills_for(None, "two"))
    finally:
        runtime.stop()


def test_manager_inspects_exact_original_and_projects_write_scope(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        for owner in ("main", "two"):
            _write_agent_skill(config.data_dir, owner, "duplicate", f"sentinel-{owner}")
        _write_test_skill(runtime.global_skills_dir, "duplicate", "sentinel-global")
        extra = tmp_path / "external"
        _write_test_skill(extra, "duplicate", "sentinel-external")
        runtime.storage.save_settings(
            {**runtime.storage.load_settings(), "skill_directories": [str(extra)]}
        )
        runtime.skill_policy.set_shared("main", "duplicate", shared=True, receivers=["two"])
        entries = [
            entry for entry in runtime.skill_inventory()["skills"] if entry["name"] == "duplicate"
        ]
        assert len(entries) == 4
        assert len({entry["id"] for entry in entries}) == 4
        for entry in entries:
            inspection = runtime.inspect_skill(entry["id"])
            assert inspection["id"] == entry["id"]
            assert entry["description"] in inspection["content"]
            if entry["description"] == "sentinel-external":
                assert entry["editable_scope"] is None
            else:
                assert entry["editable_scope"] == (
                    f"agent:{entry['owner_id']}" if entry["owner_id"] else "global"
                )
        assert {entry["id"] for entry in entries} == {
            entry["id"]
            for entry in runtime.skill_inventory()["skills"]
            if entry["name"] == "duplicate"
        }
        removed = next(entry for entry in entries if entry["owner_id"] == "two")
        (runtime.agent_skills_dir("two") / "duplicate" / "SKILL.md").unlink()
        with pytest.raises(ValueError):
            runtime.inspect_skill(removed["id"])
        with pytest.raises(ValueError):
            runtime.inspect_skill(str(tmp_path / "external" / "duplicate" / "SKILL.md"))
    finally:
        runtime.stop()
