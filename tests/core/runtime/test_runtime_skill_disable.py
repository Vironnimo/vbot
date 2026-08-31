"""Tests for Runtime wiring of the Skill Policy disable switch."""

import logging
from pathlib import Path

import pytest

from core.runtime.runtime import Runtime
from core.sessions.format import write_bootstrap_marker
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


def _write_project_skill(repo: Path, name: str, description: str) -> None:
    _write_test_skill(repo / ".opencode" / "skills", name, description)


def test_disabled_bundled_skill_leaves_every_runtime_answer(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        name = runtime.skills.list_all()[0].name

        runtime.skill_policy.set_disabled(name, disabled=True)
        runtime.reload_skills()

        assert name not in {skill.name for skill in runtime.skills.list_all()}
        try:
            runtime.skills.get(name)
            raised = False
        except KeyError:
            raised = True
        assert raised
        assert runtime.skills.filter_allowed(["*"]) == [
            skill for skill in runtime.skills.filter_allowed(["*"]) if skill.name != name
        ]
        assert runtime.skills.availability_for(name, ["*"]).state == "invalid"
        # The manager-facing excluded bucket still sees exactly what was disabled.
        assert [skill.name for skill in runtime.skills.excluded_skills()] == [name]
    finally:
        runtime.stop()


def test_re_enabling_restores_the_skill_without_restart(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        name = runtime.skills.list_all()[0].name
        runtime.skill_policy.set_disabled(name, disabled=True)
        runtime.reload_skills()

        runtime.skill_policy.set_disabled(name, disabled=False)
        runtime.reload_skills()

        assert name in {skill.name for skill in runtime.skills.list_all()}
    finally:
        runtime.stop()


def test_disabled_project_skill_is_hidden_and_resolver_set_stays_clean(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_project_skill(repo, "proj-only-skill", "A project playbook.")
        project = runtime.projects.create("p", "P", repo)

        runtime.skill_policy.set_disabled("proj-only-skill", disabled=True)
        runtime.reload_skills()

        registry = runtime.skills_for(project.project_id)
        assert "proj-only-skill" not in {skill.name for skill in registry.list_all()}
        # The config-agent resolver input subtracts the disabled set too.
        assert runtime.project_skill_names(project.project_id) == frozenset()
    finally:
        runtime.stop()


def test_disable_beats_always_allowed_private_skills_of_the_owner(
    config: Config, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        _write_agent_skill(data_dir, "main", "private-deploy", "Owner's own playbook.")
        owner_registry = runtime.skills_for(None, "main")
        assert "private-deploy" in {skill.name for skill in owner_registry.filter_allowed([])}

        runtime.skill_policy.set_disabled("private-deploy", disabled=True)
        runtime.reload_skills()

        owner_registry = runtime.skills_for(None, "main")
        assert "private-deploy" not in {skill.name for skill in owner_registry.filter_allowed([])}
        assert "private-deploy" not in {skill.name for skill in owner_registry.list_all()}
    finally:
        runtime.stop()


def test_explicit_project_context_load_hides_disabled_skills(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_project_skill(repo, "context-skill", "Project Context listing.")
        runtime.projects.create("p", "P", repo)

        runtime.skill_policy.set_disabled("context-skill", disabled=True)

        assert runtime.project_own_skills("p") == []
    finally:
        runtime.stop()


def test_malformed_policy_does_not_break_startup(config: Config, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    policy_file = data_dir / "skills" / "policy.json"
    policy_file.parent.mkdir(parents=True)
    write_bootstrap_marker(data_dir)
    policy_file.write_text("{not valid json", encoding="utf-8")

    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        # Startup survived, every skill stays visible, and the diagnostics surface.
        assert runtime.skills.list_all()
        assert any(
            "Cannot read skill policy" in message
            for message in runtime.skill_policy.validation_diagnostics()
        )
    finally:
        runtime.stop()
