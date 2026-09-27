"""Shared private Skills in Agent-aware registries, and the Skill manager surface."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from core.runtime.runtime import Runtime
from core.skills.skills import SKILL_ORIGIN_AGENT, SkillRegistry, project_skills_dir
from core.utils.config import Config
from server.rpc import agent_methods, skill_methods
from tests.core.runtime.runtime_test_support import call_rpc, write_agent_skill, write_skill


def _names(registry: SkillRegistry) -> set[str]:
    return {skill.name for skill in registry.list_all()}


def _allowed(registry: SkillRegistry, allowlist: list[str]) -> list[str]:
    return [skill.name for skill in registry.filter_allowed(allowlist)]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete", "create", "delete_owner"])
async def test_owner_skill_mutation_refreshes_shared_receivers_in_every_project(
    config: Config, tmp_path: Path, operation: str
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        runtime.agents.create("receiver", "Receiver")
        runtime.agents.create("unrelated", "Unrelated")
        repo = tmp_path / "repo"
        repo.mkdir()
        project = runtime.projects.create("p", "P", repo)
        if operation != "create":
            write_agent_skill(config.data_dir, "main", "deploy", "Before mutation")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["receiver"])
        scopes = [None, project.project_id]
        before = {scope: runtime.skills_for(scope, "receiver") for scope in scopes}
        unrelated = runtime.skills_for(project.project_id, "unrelated")
        params = {"scope": "agent:main", "name": "deploy"}
        if operation != "delete":
            params["content"] = (
                "---\nname: deploy\ndescription: After mutation\n---\n\nNew instructions.\n"
            )

        state = SimpleNamespace(runtime=runtime, chat_runs=runtime.chat_runs)
        if operation == "delete_owner":
            await call_rpc(agent_methods.method_handlers(), "agent.delete", state, {"id": "main"})
        else:
            await call_rpc(skill_methods.method_handlers(), f"skill.{operation}", state, params)

        for scope in scopes:
            current = runtime.skills_for(scope, "receiver")
            assert current is not before[scope]
            if operation in {"delete", "delete_owner"}:
                assert "deploy" not in _names(current)
            else:
                assert current.get("deploy").description == "After mutation"
        assert runtime.skills_for(project.project_id, "unrelated") is unrelated
    finally:
        await runtime.aclose()


def test_shared_skill_reaches_only_its_receivers_as_their_own_skill(
    runtime: Runtime, tmp_path: Path
) -> None:
    data_dir = runtime.storage.data_dir
    runtime.agents.create("two", "Two", allowed_skills=["unrelated"])
    write_agent_skill(data_dir, "main", "deploy", "Shared playbook.")
    write_agent_skill(data_dir, "main", "secret-notes", "Unshared neighbour.")
    owner_before = runtime.skills_for(None, "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    project = runtime.projects.create("p", "P", repo)

    runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
    runtime.invalidate_agent_skills("main")

    registry = runtime.skills_for(None, "two")
    assert "deploy" in _names(registry)
    assert "secret-notes" not in _names(registry)
    # Receiver-facing origin: indistinguishable from its own Skills, but filtered
    # by the receiver's allowlist like any global Skill, never always allowed.
    assert registry.get("deploy").origin == SKILL_ORIGIN_AGENT
    assert _allowed(registry, ["unrelated"]) == []
    receiver = runtime.agents.update("two", allowed_skills=["deploy"])
    runtime.invalidate_agent_skills("two")
    registry = runtime.skills_for(None, "two")
    assert _allowed(registry, ["deploy"]) == ["deploy"]

    # The owner's view is unchanged, including its always-allowed private Skills.
    owner_after = runtime.skills_for(None, "main")
    assert _names(owner_after) == _names(owner_before)
    assert sorted(_allowed(owner_after, [])) == ["deploy", "secret-notes"]

    # A config-Agent Run passes no identity id: the private-home boundary stays
    # identity-only, so the project bundle never carries shared Skills.
    assert "deploy" not in _names(runtime.skills_for(project.project_id))

    # No new group and no provenance hint: the shared Skill renders inside the
    # ordinary "Your own skills" group.
    catalog = runtime.system_prompts.render_skill_catalog(receiver, registry)
    labels = re.findall(r"^(\S[^\n]*):$", catalog.catalog_text, re.MULTILINE)
    assert set(labels) <= {"Bundled skills", "Your global skills", "Your own skills"}
    own_group = catalog.catalog_text.split("Your own skills:\n", 1)[1]
    own_group = own_group.split("</available_skills>", 1)[0]
    assert "- deploy: Shared playbook." in own_group


def test_unsharing_or_disabling_removes_a_shared_skill_from_receivers_live(
    runtime: Runtime,
) -> None:
    runtime.agents.create("two", "Two")
    write_agent_skill(runtime.storage.data_dir, "main", "deploy", "Shared.")
    runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
    runtime.invalidate_agent_skills(None)
    assert "deploy" in _names(runtime.skills_for(None, "two"))

    runtime.skill_policy.set_shared("main", "deploy", shared=False)
    runtime.invalidate_agent_skills(None)
    assert "deploy" not in _names(runtime.skills_for(None, "two"))

    runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
    runtime.invalidate_agent_skills(None)
    assert "deploy" in _names(runtime.skills_for(None, "two"))
    runtime.skill_policy.set_disabled("deploy", disabled=True)
    runtime.reload_skills()
    assert "deploy" not in _names(runtime.skills_for(None, "two"))


def test_installed_private_and_global_skills_refresh_live_visibility(
    runtime: Runtime, tmp_path: Path
) -> None:
    source = tmp_path / "incoming"
    source.mkdir()
    document = "---\nname: imported\ndescription: First description.\n---\nBody\n"
    (source / "SKILL.md").write_text(document, encoding="utf-8")
    runtime.agents.create("receiver", "Receiver", allowed_skills=["imported"])
    runtime.agents.update("main", allowed_skills=[])
    runtime.skills_for(None, "main")
    runtime.skills_for(None, "receiver")
    state = SimpleNamespace(runtime=runtime)
    handlers = skill_methods.method_handlers()

    def install(params: dict[str, Any]) -> None:
        asyncio.run(call_rpc(handlers, "skill.install", state, params))

    install({"source": str(source), "scope": "agent:main"})
    assert _allowed(runtime.skills_for(None, "main"), []) == ["imported"]
    assert "imported" not in _names(runtime.skills_for(None, "receiver"))

    runtime.skill_policy.set_shared("main", "imported", shared=True, receivers=["receiver"])
    runtime.invalidate_agent_skills(None)
    assert runtime.skills_for(None, "receiver").get("imported").description == (
        "First description."
    )
    # Replacing the owner's package refreshes what the receiver sees.
    (source / "SKILL.md").write_text(document.replace("First", "Updated"), encoding="utf-8")
    install({"source": str(source), "scope": "agent:main", "replace": True})
    assert runtime.skills_for(None, "receiver").get("imported").description == (
        "Updated description."
    )

    # A global install is visible but, unlike a private one, not always allowed.
    (source / "SKILL.md").write_text(
        document.replace("imported", "global-import"), encoding="utf-8"
    )
    install({"source": str(source), "scope": "global"})
    assert runtime.skills_for(None, "main").get("global-import")
    assert "global-import" not in _allowed(runtime.skills_for(None, "main"), [])


def test_manager_inspects_exact_original_and_projects_write_scope(
    runtime: Runtime, tmp_path: Path
) -> None:
    runtime.agents.create("two", "Two")
    for owner in ("main", "two"):
        write_agent_skill(runtime.storage.data_dir, owner, "duplicate", f"sentinel-{owner}")
    write_skill(runtime.global_skills_dir, "duplicate", "sentinel-global")
    extra = tmp_path / "external"
    write_skill(extra, "duplicate", "sentinel-external")
    runtime.storage.save_settings(
        {**runtime.storage.load_settings(), "skill_directories": [str(extra)]}
    )
    runtime.skill_policy.set_shared("main", "duplicate", shared=True, receivers=["two"])

    def duplicates() -> list[dict[str, Any]]:
        return [
            entry for entry in runtime.skill_inventory()["skills"] if entry["name"] == "duplicate"
        ]

    entries = duplicates()
    assert len({entry["id"] for entry in entries}) == len(entries) == 4
    for entry in entries:
        inspection = runtime.inspect_skill(entry["id"])
        assert inspection["id"] == entry["id"]
        assert entry["description"] in inspection["content"]
        # Only packages in the real global and private write roots are editable.
        if entry["description"] == "sentinel-external":
            assert entry["editable_scope"] is None
        else:
            assert entry["editable_scope"] == (
                f"agent:{entry['owner_id']}" if entry["owner_id"] else "global"
            )
    # Ids are stable across inventory passes.
    assert {entry["id"] for entry in entries} == {entry["id"] for entry in duplicates()}
    removed = next(entry for entry in entries if entry["owner_id"] == "two")
    (runtime.agent_skills_dir("two") / "duplicate" / "SKILL.md").unlink()
    with pytest.raises(ValueError):
        runtime.inspect_skill(removed["id"])
    # Inspection never addresses arbitrary client filesystem paths.
    with pytest.raises(ValueError):
        runtime.inspect_skill(str(extra / "duplicate" / "SKILL.md"))


def test_manager_evaluates_each_same_name_package(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "VBOT_TEST_GLOBAL_REQUIRED",
        "VBOT_TEST_PROJECT_OPTIONAL",
        "VBOT_TEST_PRIVATE_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        runtime.agents.create("two", "Two")
        repo = tmp_path / "repo"
        repo.mkdir()
        project = runtime.projects.create("p", "P", repo)
        write_skill(runtime.global_skills_dir, "inventory-helper", "Dependency.")
        packages: tuple[tuple[Path, str, dict[str, object]], ...] = (
            (runtime.global_skills_dir, "global", {"env": "VBOT_TEST_GLOBAL_REQUIRED"}),
            (
                project_skills_dir(repo, project.source_format),
                "project",
                {
                    "all": [{"skill": "inventory-helper"}],
                    "optional": [{"env": "VBOT_TEST_PROJECT_OPTIONAL"}],
                },
            ),
            (runtime.agent_skills_dir("main"), "main", {}),
            (runtime.agent_skills_dir("two"), "two", {"env": "VBOT_TEST_PRIVATE_REQUIRED"}),
        )
        for root, label, requirements in packages:
            package = root / "duplicate"
            package.mkdir(parents=True)
            frontmatter = yaml.safe_dump(
                {
                    "name": "duplicate",
                    "description": label,
                    "metadata": {"vbot": {"requirements": requirements}},
                }
            )
            (package / "SKILL.md").write_text(f"---\n{frontmatter}---\n", encoding="utf-8")
        expected = {
            "global": (
                "unavailable",
                ["missing environment variable 'VBOT_TEST_GLOBAL_REQUIRED'"],
                [],
            ),
            "project": (
                "available",
                [],
                ["missing environment variable 'VBOT_TEST_PROJECT_OPTIONAL'"],
            ),
            "main": ("available", [], []),
            "two": (
                "unavailable",
                ["missing environment variable 'VBOT_TEST_PRIVATE_REQUIRED'"],
                [],
            ),
        }

        def assert_inventory(*, disabled: bool) -> None:
            entries = {
                entry["description"]: entry
                for entry in runtime.skill_inventory()["skills"]
                if entry["name"] == "duplicate"
            }
            assert entries.keys() == expected.keys()
            for label, (status, missing, optional_missing) in expected.items():
                # The disable switch outranks every other state but keeps the details.
                assert entries[label]["status"] == ("disabled" if disabled else status)
                assert entries[label]["missing"] == missing
                assert entries[label]["optional_missing"] == optional_missing

        assert_inventory(disabled=False)
        assert runtime.skills_for(None, "main").availability_for("duplicate").state == "available"
        runtime.skill_policy.set_disabled("duplicate", disabled=True)
        assert_inventory(disabled=True)
    finally:
        runtime.stop()
