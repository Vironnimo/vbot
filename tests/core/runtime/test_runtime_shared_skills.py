"""Shared private Skills in Agent-aware registries, and the Skill manager surface."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from core.runtime.runtime import Runtime
from core.skills.policy import SkillPackageRef
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_GLOBAL,
    SkillRegistry,
    project_skills_dir,
)
from core.tools import ToolContext
from core.utils.config import Config
from server.events import ServerEventBus
from server.rpc import agent_methods, skill_methods
from tests.core.runtime.runtime_test_support import call_rpc, write_agent_skill, write_skill


@pytest.fixture
def runtime(config: Config) -> Iterator[Runtime]:
    """A started test-mode Runtime: Skill resolution needs no Extensions or producers."""
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    yield runtime
    runtime.stop()


def _names(registry: SkillRegistry) -> set[str]:
    return {skill.name for skill in registry.list_all()}


def _allowed(registry: SkillRegistry, allowlist: list[str]) -> list[str]:
    return [skill.name for skill in registry.filter_allowed(allowlist)]


@pytest.mark.asyncio
async def test_owner_skill_mutation_refreshes_shared_receivers_in_every_project(
    config: Config, tmp_path: Path
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        runtime.agents.create("receiver", "Receiver")
        runtime.agents.create("unrelated", "Unrelated")
        repo = tmp_path / "repo"
        repo.mkdir()
        project = runtime.projects.create("p", "P", repo)
        # The share names the package before it exists.
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["receiver"])
        scopes = [None, project.project_id]
        state = SimpleNamespace(
            runtime=runtime,
            chat_runs=runtime.chat_runs,
            agent_delete_lock=asyncio.Lock(),
            event_bus=ServerEventBus(),
        )

        async def mutate(operation: str) -> None:
            """Apply one owner mutation; every receiver scope refreshes, nothing else."""
            before = {scope: runtime.skills_for(scope, "receiver") for scope in scopes}
            unrelated = runtime.skills_for(project.project_id, "unrelated")
            if operation == "delete_owner":
                await call_rpc(
                    agent_methods.method_handlers(), "agent.delete", state, {"id": "main"}
                )
            else:
                params = {"scope": "agent:main", "name": "deploy"}
                if operation != "delete":
                    params["content"] = (
                        f"---\nname: deploy\ndescription: After {operation}\n---\n\nSteps.\n"
                    )
                await call_rpc(skill_methods.method_handlers(), f"skill.{operation}", state, params)
            for scope in scopes:
                current = runtime.skills_for(scope, "receiver")
                assert current is not before[scope]
                if operation in {"delete", "delete_owner"}:
                    assert "deploy" not in _names(current)
                else:
                    assert current.get("deploy").description == f"After {operation}"
            assert runtime.skills_for(project.project_id, "unrelated") is unrelated

        for operation in ("create", "update", "delete"):
            await mutate(operation)
        write_agent_skill(config.data_dir, "main", "deploy", "Before the owner leaves")
        runtime.invalidate_agent_skills("main")
        assert "deploy" in _names(runtime.skills_for(None, "receiver"))
        await mutate("delete_owner")
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
    # Librarian aging counts the receivers' use of the Skills the owner shares.
    assert runtime.shared_skill_receivers("main") == {"deploy": frozenset({"two"})}
    assert runtime.shared_skill_receivers("two") == {}

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


def test_a_stale_shared_entry_warns_once_until_it_resolves_not_per_registry_build(
    runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    runtime.agents.create("two", "Two")
    runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
    runtime.skill_policy.set_shared("gone", "notes", shared=True, receivers=["two"])

    def warnings_of_builds(count: int) -> list[str]:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            for _build in range(count):
                runtime.invalidate_agent_skills(None)
                runtime.skills_for(None, "two")
        return [record.getMessage() for record in caplog.records]

    # One warning per stale entry, however many receiver registries are built.
    first = warnings_of_builds(3)
    assert len(first) == 2
    assert any("gone" in message for message in first)
    assert any("deploy" in message for message in first)
    # The package appears, so the entry resolves; losing it again warns again.
    package = write_agent_skill(runtime.storage.data_dir, "main", "deploy", "Shared.")
    assert warnings_of_builds(2) == []
    shutil.rmtree(package)
    [again] = warnings_of_builds(2)
    assert "deploy" in again


@pytest.mark.asyncio
async def test_a_deleted_skills_shares_and_automations_follow_it_or_are_named(
    config: Config, tmp_path: Path
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        runtime.agents.create("receiver", "Receiver")
        data_dir = runtime.storage.data_dir
        write_agent_skill(data_dir, "main", "deploy-web", "Deploy the web app.")
        write_agent_skill(data_dir, "main", "deploy", "Deploy anything.")
        runtime.skill_policy.set_shared("main", "deploy-web", shared=True, receivers=["receiver"])
        job = await runtime.cron_service.create_job(
            agent_id="main",
            name="Nightly",
            prompt="/deploy-web tonight",
            schedule_type="interval",
            interval_seconds=3600,
        )
        context = ToolContext(
            agent_id="main",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name="skill_manage",
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=data_dir,
            cwd=tmp_path,
        )

        result = await runtime.tools.dispatch(
            context,
            {"action": "delete", "name": "deploy-web", "absorbed_into": "deploy"},
            ["skill_manage"],
        )

        assert result["data"]["content"].endswith(
            "\nNote: These now use Skill 'deploy' in place of 'deploy-web': the share with "
            "Agent 'Receiver'; Cron job 'Nightly'."
        )
        assert runtime.skill_policy.load().shared == {"main": {"deploy": frozenset({"receiver"})}}
        assert _names(runtime.skills_for(None, "receiver")) >= {"deploy"}
        assert runtime.cron_service.get_job(job.id).prompt == "/deploy tonight"
        home = runtime.agent_skills_dir("main")
        [revision] = runtime.skill_authoring.history(home, "deploy-web", limit=1)
        assert [reference.to_dict() for reference in revision.followed] == [
            {"kind": "shared", "id": "receiver", "name": "Receiver"},
            {"kind": "cron", "id": job.id, "name": "Nightly"},
        ]

        # Without absorbed_into nothing holds the instructions: nothing moves, and
        # the result names what still points at the deleted Skill.
        write_agent_skill(data_dir, "main", "deploy-old", "Deploy the old way.")
        runtime.skill_policy.set_shared("main", "deploy-old", shared=True, receivers=["receiver"])
        old_job = await runtime.cron_service.create_job(
            agent_id="main",
            name="Weekly",
            prompt="/deploy-old now",
            schedule_type="interval",
            interval_seconds=3600,
        )

        result = await runtime.tools.dispatch(
            context, {"action": "delete", "name": "deploy-old"}, ["skill_manage"]
        )

        assert result["data"]["content"].endswith(
            "\nWarning: These still name 'deploy-old', which no longer exists: the share with "
            "Agent 'Receiver'; Cron job 'Weekly'. Name them in your reply so the user can "
            "change or remove them."
        )
        assert runtime.skill_policy.load().shared["main"]["deploy-old"] == {"receiver"}
        assert runtime.cron_service.get_job(old_job.id).prompt == "/deploy-old now"
        [revision] = runtime.skill_authoring.history(home, "deploy-old", limit=1)
        assert revision.followed == ()
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_skill_manage_publishes_into_and_changes_only_the_global_home(
    config: Config, tmp_path: Path
) -> None:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        runtime.agents.create("receiver", "Receiver")
        runtime.agents.update("main", allowed_skills=["review"])
        data_dir = runtime.storage.data_dir
        write_agent_skill(data_dir, "main", "deploy", "Deploy the app.")
        write_agent_skill(data_dir, "main", "draft", "An unfinished draft.")
        runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["receiver"])
        private_draft = SkillPackageRef("agent", "draft", "main")
        runtime.skill_policy.set_package_disabled(private_draft, disabled=True)
        folder = tmp_path / "team-skills"
        write_skill(folder, "team", "A team Skill.")
        runtime.storage.update_settings_sections({"skills": {"directories": [str(folder)]}})
        runtime.reload_skills()
        context = ToolContext(
            agent_id="main",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name="skill_manage",
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=data_dir,
            cwd=tmp_path,
        )

        async def call(arguments: dict[str, Any]) -> dict[str, Any]:
            return await runtime.tools.dispatch(context, arguments, ["skill_manage"])

        published = await call({"action": "publish", "name": "deploy"})
        await call({"action": "publish", "name": "draft"})

        assert published["ok"] is True
        # What pointed at the private packages follows them: the share ends, the
        # turned-off draft stays off as the global package, and the publisher
        # keeps both Skills through its allowlist.
        policy = runtime.skill_policy.load()
        assert policy.shared == {}
        assert SkillPackageRef("home", "draft") in policy.disabled_packages
        assert private_draft not in policy.disabled_packages
        assert runtime.agents.get("main").allowed_skills == ["review", "deploy", "draft"]
        for agent_id in ("main", "receiver"):
            registry = runtime.skills_for(None, agent_id)
            assert registry.get("deploy").origin == SKILL_ORIGIN_GLOBAL
            assert "draft" not in _names(registry)

        patched = await call(
            {
                "action": "patch",
                "name": "deploy",
                "old_string": "Use this skill.",
                "new_string": "Use this skill with care.",
            }
        )
        folder_patch = await call(
            {"action": "patch", "name": "team", "old_string": "Use", "new_string": "Try"}
        )

        assert patched["ok"] is True
        assert "with care" in (data_dir / "skills" / "deploy" / "SKILL.md").read_text("utf-8")
        assert folder_patch["error"]["message"].startswith(
            "Skill 'team' comes from a skill folder or an Extension and is read-only"
        )
        assert "Use this skill." in (folder / "team" / "SKILL.md").read_text("utf-8")
    finally:
        runtime.stop()


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
    assert runtime.shared_skill_receivers("main") == {}

    runtime.skill_policy.set_shared("main", "deploy", shared=True, receivers=["two"])
    runtime.invalidate_agent_skills(None)
    assert "deploy" in _names(runtime.skills_for(None, "two"))
    runtime.skill_policy.set_package_disabled(
        SkillPackageRef("agent", "deploy", "main"), disabled=True
    )
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
    state = SimpleNamespace(
        runtime=runtime, agent_delete_lock=asyncio.Lock(), event_bus=ServerEventBus()
    )
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

    # The inventory carries each writable package's history record.
    [entry] = [
        entry
        for entry in runtime.skill_inventory()["skills"]
        if entry["name"] == "imported" and entry["editable_scope"] == "agent:main"
    ]
    assert (entry["created_by"], entry["changed_by"], entry["pinned"]) == ("human", "human", False)

    # Deleting archives the package: it leaves every registry, the inventory lists
    # it under its home, and the skill Tool's archive lookup finds it.
    asyncio.run(
        call_rpc(handlers, "skill.delete", state, {"scope": "agent:main", "name": "imported"})
    )
    assert "imported" not in _names(runtime.skills_for(None, "receiver"))
    [archived] = runtime.skill_inventory()["archived"]
    assert (archived["scope"], archived["name"], archived["reason"]) == (
        "agent:main",
        "imported",
        "deleted",
    )
    found = runtime.archived_skill("main", "imported")
    assert found is not None and found.archive_id == archived["archive_id"]
    assert runtime.archived_skill("receiver", "imported") is None


def test_manager_lists_inspects_and_evaluates_each_same_name_package(
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
        extra = tmp_path / "external"
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
            (extra, "external", {}),
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
        runtime.storage.save_settings(
            {**runtime.storage.load_settings(), "skill_directories": [str(extra)]}
        )
        runtime.skill_policy.set_shared("main", "duplicate", shared=True, receivers=["two"])
        # Per package: status, missing, optional missing, and the editable scope.
        expected: dict[str, tuple[str, list[str], list[str], str | None]] = {
            "global": (
                "unavailable",
                ["missing environment variable 'VBOT_TEST_GLOBAL_REQUIRED'"],
                [],
                "global",
            ),
            "project": (
                "available",
                [],
                ["missing environment variable 'VBOT_TEST_PROJECT_OPTIONAL'"],
                None,
            ),
            "main": ("available", [], [], "agent:main"),
            "two": (
                "unavailable",
                ["missing environment variable 'VBOT_TEST_PRIVATE_REQUIRED'"],
                [],
                "agent:two",
            ),
            # Only packages in the real global and private write roots are editable.
            "external": ("available", [], [], None),
        }

        def duplicates() -> dict[str, dict[str, Any]]:
            return {
                entry["description"]: entry
                for entry in runtime.skill_inventory()["skills"]
                if entry["name"] == "duplicate"
            }

        entries = duplicates()
        assert entries.keys() == expected.keys()
        assert len({entry["id"] for entry in entries.values()}) == len(entries)
        for label, (status, missing, optional_missing, scope) in expected.items():
            entry = entries[label]
            assert (entry["status"], entry["missing"], entry["optional_missing"]) == (
                status,
                missing,
                optional_missing,
            )
            assert entry["editable_scope"] == scope
            inspection = runtime.inspect_skill(entry["id"])
            assert inspection["id"] == entry["id"]
            assert f"description: {label}" in inspection["content"]
        assert runtime.skills_for(None, "main").availability_for("duplicate").state == "available"

        # Turning each package off outranks every other state but keeps the
        # details; ids are stable across inventory passes.
        for entry in entries.values():
            runtime.set_skill_package_disabled(entry["id"], disabled=True)
        disabled = duplicates()
        assert {label: entry["id"] for label, entry in disabled.items()} == {
            label: entry["id"] for label, entry in entries.items()
        }
        for label, (_status, missing, optional_missing, _scope) in expected.items():
            assert (
                disabled[label]["status"],
                disabled[label]["missing"],
                disabled[label]["optional_missing"],
            ) == ("disabled", missing, optional_missing)

        (runtime.agent_skills_dir("two") / "duplicate" / "SKILL.md").unlink()
        with pytest.raises(ValueError):
            runtime.inspect_skill(entries["two"]["id"])
        # Inspection never addresses arbitrary client filesystem paths.
        with pytest.raises(ValueError):
            runtime.inspect_skill(str(extra / "duplicate" / "SKILL.md"))
    finally:
        runtime.stop()


def _write_skill_requiring(root: Path, name: str, dependency: str) -> None:
    package = root / name
    package.mkdir(parents=True)
    frontmatter = yaml.safe_dump(
        {
            "name": name,
            "description": f"Needs {dependency}.",
            "metadata": {"vbot": {"requirements": {"all": [{"skill": dependency}]}}},
        }
    )
    (package / "SKILL.md").write_text(f"---\n{frontmatter}---\n", encoding="utf-8")


def test_manager_projects_each_agents_effective_skill_access(
    runtime: Runtime, tmp_path: Path
) -> None:
    for name in ("alpha", "beta", "zeta"):
        write_skill(runtime.global_skills_dir, name, f"{name} playbook.")
    _write_skill_requiring(runtime.global_skills_dir, "needs-alpha", "alpha")
    runtime.reload_skills()
    repo = tmp_path / "repo"
    write_skill(project_skills_dir(repo, "opencode"), "project-playbook", "Project playbook.")
    runtime.projects.create("p", "P", repo)
    runtime.projects.update("p", skills_global_enabled=["zeta"])
    for name in ("main-private", "main-off", "zeta"):
        write_agent_skill(runtime.storage.data_dir, "main", name, "Main playbook.")
    runtime.agents.update(
        "main", root_project_id="p", excluded_skills=["alpha", "main-off", "zeta"]
    )
    # A root Project that no longer exists falls back to the Agent's own scope.
    runtime.agents.create("two", "Two", allowed_skills=["beta"])
    runtime.agents.update("two", root_project_id="gone")

    inventory = runtime.skill_inventory()
    main, two = inventory["agents"]
    watched = {
        "alpha",
        "beta",
        "zeta",
        "needs-alpha",
        "project-playbook",
        "main-private",
        "main-off",
    }

    def grants(agent: dict[str, Any]) -> dict[str, tuple[bool, str, bool]]:
        return {
            skill["name"]: (skill["own"], skill["grant"], skill["available"])
            for skill in agent["skills"]
            if skill["name"] in watched
        }

    assert {key: main[key] for key in main if key != "skills"} == {
        "id": "main",
        "name": runtime.agents.get("main").name,
        "root_project_id": "p",
        "allowed_skills": ["*"],
        "excluded_skills": ["alpha", "main-off", "zeta"],
        "mode": "all",
    }
    assert [skill["name"] for skill in main["skills"]] == sorted(
        skill["name"] for skill in main["skills"]
    )
    assert grants(main) == {
        "alpha": (False, "excluded", True),
        # An excluded dependency makes its dependent unavailable.
        "needs-alpha": (False, "allowed", False),
        "beta": (False, "allowed", True),
        # The root Project's grant outranks the allowlist and an exclusion, even of
        # a same-named own package.
        "zeta": (True, "project", True),
        "project-playbook": (False, "project", True),
        "main-private": (True, "own", True),
        # Exclusions turn off own Skills too.
        "main-off": (True, "excluded", True),
    }
    assert (two["root_project_id"], two["mode"], two["excluded_skills"]) == ("gone", "selected", [])
    assert grants(two) == {
        "alpha": (False, "not_selected", True),
        "needs-alpha": (False, "not_selected", False),
        "beta": (False, "allowed", True),
        "zeta": (False, "not_selected", True),
    }
    # ``package_id`` names the inventory entry of the package that wins for the Agent.
    entries = {
        (entry["name"], entry["owner_id"], entry["project_id"]): entry["id"]
        for entry in inventory["skills"]
    }
    package_ids = {skill["name"]: skill["package_id"] for skill in main["skills"]}
    assert package_ids["main-private"] == entries[("main-private", "main", None)]
    assert package_ids["project-playbook"] == entries[("project-playbook", None, "p")]
    assert package_ids["alpha"] == entries[("alpha", None, None)]


def test_manager_projects_each_projects_skill_pool(runtime: Runtime, tmp_path: Path) -> None:
    bundled, shadowed = [
        skill.name for skill in runtime.skills.list_all() if skill.origin == "bundled"
    ][:2]
    write_skill(runtime.global_skills_dir, "deploy", "Global deploy.")
    external = tmp_path / "external"
    write_skill(external, "external", "Configured directory Skill.")
    runtime.storage.save_settings(
        {**runtime.storage.load_settings(), "skill_directories": [str(external)]}
    )
    runtime.reload_skills()
    repo = tmp_path / "repo"
    project_root = project_skills_dir(repo, "opencode")
    for name in ("project-playbook", "muted", shadowed):
        write_skill(project_root, name, f"Project {name}.")
    runtime.projects.create("b-project", "Beta", repo)
    runtime.projects.update(
        "b-project",
        skills_project_disabled=["muted"],
        skills_global_enabled=["deploy"],
        skills_bundled_enabled=[bundled],
    )
    runtime.projects.create("a-project", "alpha", tmp_path / "empty")

    inventory = runtime.skill_inventory()

    # Sorted by display name, case-insensitively.
    assert [project["project_id"] for project in inventory["projects"]] == [
        "a-project",
        "b-project",
    ]
    project = inventory["projects"][1]
    assert {key: project[key] for key in project if key != "skills"} == {
        "project_id": "b-project",
        "name": "Beta",
        "skills_project_disabled": ["muted"],
        "skills_global_enabled": ["deploy"],
        "skills_bundled_enabled": [bundled],
    }
    pool = {skill["name"]: (skill["source"], skill["active"]) for skill in project["skills"]}
    assert [skill["name"] for skill in project["skills"]] == sorted(pool)
    assert {
        name: pool[name]
        for name in ("project-playbook", "muted", shadowed, "deploy", "external", bundled)
    } == {
        "project-playbook": ("project", True),
        "muted": ("project", False),
        # A Project Skill shadows the same-named bundled Skill.
        shadowed: ("project", True),
        "deploy": ("global", True),
        "external": ("global", False),
        bundled: ("bundled", True),
    }
    # Every package names the Project whose directory holds it.
    project_entries = {
        entry["name"]: entry for entry in inventory["skills"] if entry["project_id"] == "b-project"
    }
    assert project_entries.keys() == {"project-playbook", "muted", shadowed}
    assert all(
        entry["project_id"] is None
        for entry in inventory["skills"]
        if entry["origin"] in {"global", "bundled", "agent"}
    )
    package_ids = {skill["name"]: skill["package_id"] for skill in project["skills"]}
    assert package_ids[shadowed] == project_entries[shadowed]["id"]
