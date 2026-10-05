"""Runtime Skill resolution: scopes, source precedence, caching, and the disable policy."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.runtime.runtime import Runtime
from core.skills.skills import SKILL_ORIGIN_AGENT, SKILL_ORIGIN_GLOBAL, SkillRegistry
from core.tools import ToolContext, tool_failure
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import (
    write_agent_skill,
    write_extension_with_skill,
    write_project_skill,
    write_settings,
    write_skill,
)

BUNDLED_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "resources" / "skills"
RELOADED_SKILL_NAME = "runtime-reloaded-skill"


@pytest.fixture
def runtime(config: Config) -> Iterator[Runtime]:
    """A started test-mode Runtime: Skill resolution needs no Extensions or producers."""
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    yield runtime
    runtime.stop()


def _names(registry: SkillRegistry) -> set[str]:
    return {skill.name for skill in registry.list_all()}


def _bundled_names(runtime: Runtime) -> list[str]:
    return [skill.name for skill in runtime.skills.list_all() if skill.origin == "bundled"]


def test_identity_skills_layer_the_owners_private_home_over_the_global_pool(
    runtime: Runtime,
) -> None:
    data_dir = runtime.storage.data_dir
    assert runtime.agent_skills_dir("main") == data_dir / "agents" / "main" / "skills"
    runtime.agents.create("two", "Two")
    # Without a private home, the identity path is the global registry itself.
    assert runtime.skills_for(None) is runtime.skills
    assert runtime.skills_for(None, "two") is runtime.skills

    write_agent_skill(data_dir, "main", "my-private", "An agent-only playbook.")
    # A home under ``agents/<id>/`` that belongs to no stored identity Agent is
    # never layered: private Skills are always allowed for their owner, so an
    # unowned home would bypass every allowlist.
    write_agent_skill(data_dir, "ghost", "ghost-skill", "Nobody's playbook.")

    registry = runtime.skills_for(None, "main")
    assert registry.get("my-private").origin == SKILL_ORIGIN_AGENT
    # The owner's own Skill is always allowed, and only it: an empty allowlist
    # lets no bundled Skill in.
    assert registry.is_allowed("my-private", [])
    assert [skill.name for skill in registry.filter_allowed([])] == ["my-private"]
    assert "my-private" not in _names(runtime.skills)
    assert "my-private" not in _names(runtime.skills_for(None, "two"))
    assert runtime.skills_for(None, "ghost") is runtime.skills
    assert "ghost-skill" not in _names(runtime.skills_for(None, "ghost"))


def test_project_skills_layer_between_the_agent_and_the_bundled_pool(
    runtime: Runtime, tmp_path: Path
) -> None:
    data_dir = runtime.storage.data_dir
    overridden, bundled_name = _bundled_names(runtime)[:2]
    repo = tmp_path / "repo"
    repo.mkdir()
    write_project_skill(repo, "proj-skill", "Project playbook.")
    write_project_skill(repo, "shared", "Project version.")
    write_project_skill(repo, overridden, "Project override of a bundled skill.")
    project = runtime.projects.create("p", "P", repo)
    write_agent_skill(data_dir, "main", "agent-skill", "Agent playbook.")
    write_agent_skill(data_dir, "main", "shared", "Agent version.")

    project_registry = runtime.skills_for(project.project_id)
    # The project's own Skills plus the entire bundled pool; the project wins a
    # name collision with a bundled Skill.
    assert _names(runtime.skills) | {"proj-skill", "shared"} == _names(project_registry)
    assert project_registry.get(overridden).description == "Project override of a bundled skill."
    assert project_registry.get("shared").description == "Project version."
    assert runtime.project_skill_names(project.project_id) == frozenset(
        {"proj-skill", "shared", overridden}
    )
    assert runtime.project_skill_names(None) == frozenset()
    own = {skill.name: skill.path for skill in runtime.project_own_skills(project.project_id)}
    assert own.keys() == {"proj-skill", "shared", overridden}
    assert (
        own["proj-skill"] == (repo / ".opencode" / "skills" / "proj-skill" / "SKILL.md").resolve()
    )

    registry = runtime.skills_for(project.project_id, "main")
    assert {"agent-skill", "proj-skill"} <= _names(registry)
    # Agent Skills are scanned first, so the Agent wins a collision with the project.
    assert registry.get("shared").description == "Agent version."
    assert registry.get("agent-skill").origin == "agent"
    assert registry.get("proj-skill").origin == "project:P"
    assert registry.get(bundled_name).origin == "bundled"


def test_project_context_grants_its_enabled_project_and_bundled_skills(
    runtime: Runtime, tmp_path: Path
) -> None:
    bundled_name = runtime.skills.filter_allowed(["*"])[0].name
    repo = tmp_path / "repo"
    repo.mkdir()
    write_project_skill(repo, "active-project-skill", "Active Project workflow.")
    write_project_skill(repo, "disabled-project-skill", "Disabled Project workflow.")
    runtime.projects.create("p", "P", repo)
    project = runtime.projects.update(
        "p",
        skills_project_disabled=["disabled-project-skill"],
        skills_bundled_enabled=[bundled_name],
    )

    # The Project Context grants its effective set even to an identity Agent
    # with an empty personal allowlist.
    registry = runtime.skills_for(project.project_id, "main")
    assert {skill.name for skill in registry.filter_allowed([])} == {
        "active-project-skill",
        bundled_name,
    }
    assert {skill.name for skill in runtime.project_context_skills(project.project_id)} == {
        "active-project-skill",
        bundled_name,
    }

    # A claude-format project reads only ``.claude/skills/``; one format per project.
    claude_repo = tmp_path / "claude-repo"
    claude_repo.mkdir()
    write_skill(claude_repo / ".claude" / "skills", "claude-skill", "Claude playbook.")
    write_project_skill(claude_repo, "opencode-skill", "OpenCode playbook.")
    claude = runtime.projects.create("c", "C", claude_repo, source_format="claude")

    names = _names(runtime.skills_for(claude.project_id))
    assert "claude-skill" in names
    assert "opencode-skill" not in names
    assert runtime.project_skill_names(claude.project_id) == frozenset({"claude-skill"})
    assert [skill.name for skill in runtime.project_own_skills(claude.project_id)] == [
        "claude-skill"
    ]


def test_agent_exclusions_narrow_its_allowlist_and_own_skills_but_never_project_grants(
    runtime: Runtime, tmp_path: Path
) -> None:
    data_dir = runtime.storage.data_dir
    write_skill(runtime.global_skills_dir, "alpha", "Alpha playbook.")
    write_skill(runtime.global_skills_dir, "beta", "Beta playbook.")
    runtime.reload_skills()
    runtime.agents.create("two", "Two")
    write_agent_skill(data_dir, "two", "two-private", "Two's playbook.")
    # An own package that shadows a same-named Project Skill.
    write_agent_skill(data_dir, "two", "project-playbook", "Two's copy.")
    repo = tmp_path / "repo"
    write_project_skill(repo, "project-playbook", "Project playbook.")
    runtime.projects.create("p", "P", repo)
    runtime.agents.create("three", "Three")

    def granted(project_id: str | None, agent_id: str) -> set[str]:
        registry = runtime.skills_for(project_id, agent_id)
        return {skill.name for skill in registry.filter_allowed(["*"])}

    before = runtime.skills_for(None, "two")
    runtime.agents.update(
        "two", excluded_skills=["alpha", "two-private", "project-playbook", "unknown"]
    )

    # The update alone replaces the cached registry: "*" now means all but "alpha",
    # which stays loaded for the manager and for dependency diagnostics.
    excluding = runtime.skills_for(None, "two")
    assert excluding is not before
    assert runtime.skills_for(None, "two") is excluding
    assert excluding.get("alpha").name == "alpha"
    # Exclusions also turn off the Agent's own Skills, which stay loaded.
    assert excluding.get("two-private").name == "two-private"
    assert "beta" in granted(None, "two")
    assert {"alpha", "two-private", "project-playbook"}.isdisjoint(granted(None, "two"))
    # The active Project's grant outranks an exclusion, even for a same-named own Skill.
    assert {"beta", "project-playbook"} <= granted("p", "two")
    assert {"alpha", "two-private"}.isdisjoint(granted("p", "two"))

    # An Agent without private Skills still gets its exclusions applied.
    assert runtime.skills_for(None, "three") is runtime.skills
    runtime.agents.update("three", excluded_skills=["beta"])
    assert "beta" not in granted(None, "three")
    assert "alpha" in granted(None, "three")

    runtime.agents.update("three", excluded_skills=[])
    assert runtime.skills_for(None, "three") is runtime.skills
    runtime.agents.update("two", excluded_skills=[])
    assert {"alpha", "two-private", "project-playbook"} <= granted(None, "two")
    assert {"alpha", "two-private"} <= granted("p", "two")


def test_scoped_skill_registries_stay_cached_until_their_sources_are_invalidated(
    runtime: Runtime, tmp_path: Path
) -> None:
    data_dir = runtime.storage.data_dir
    runtime.agents.create("two", "Two")
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    write_project_skill(repo_a, "skill-a", "From repo A.")
    write_project_skill(repo_b, "skill-b", "From repo B.")
    write_agent_skill(data_dir, "main", "main-skill", "Main.")
    write_agent_skill(data_dir, "two", "two-skill", "Two.")
    project = runtime.projects.create("p", "P", repo_a)
    project_id = project.project_id

    project_skills = runtime.skills_for(project_id)
    main_skills = runtime.skills_for(None, "main")
    two_skills = runtime.skills_for(None, "two")
    main_project_skills = runtime.skills_for(project_id, "main")
    assert runtime.skills_for(project_id) is project_skills
    assert runtime.skills_for(None, "main") is main_skills
    assert runtime.skills_for(project_id, "main") is main_project_skills

    # An owner invalidation rebuilds that Agent in every Project Context only.
    runtime.invalidate_agent_skills("main")
    assert runtime.skills_for(None, "main") is not main_skills
    assert runtime.skills_for(project_id, "main") is not main_project_skills
    assert runtime.skills_for(None, "two") is two_skills
    assert runtime.skills_for(project_id) is project_skills
    main_skills = runtime.skills_for(None, "main")
    main_project_skills = runtime.skills_for(project_id, "main")

    # A project invalidation rescans the project and the Agent registries that
    # embed it, for example after the working directory moved.
    runtime.projects.update(project_id, cwd=str(repo_b))
    runtime.invalidate_project_skills(project_id)
    assert runtime.project_skill_names(project_id) == frozenset({"skill-b"})
    assert runtime.skills_for(project_id) is not project_skills
    assert runtime.skills_for(project_id, "main") is not main_project_skills
    assert runtime.skills_for(None, "main") is main_skills
    project_skills = runtime.skills_for(project_id)

    # A global reload makes every scoped registry stale.
    runtime.reload_skills()
    assert runtime.skills_for(project_id) is not project_skills
    assert runtime.skills_for(None, "main") is not main_skills
    assert runtime.skills_for(None, "two") is not two_skills

    # An explicit refresh rescans project and global sources.
    cached = runtime.skills_for(project_id)
    write_project_skill(repo_b, "beta", "Beta.")
    write_skill(runtime.global_skills_dir, "global-new", "New global Skill.")
    refreshed = runtime.refresh_skills_for(project_id)
    assert refreshed is not cached
    assert {"skill-b", "beta", "global-new"} <= _names(refreshed)


def test_global_skill_sources_follow_the_documented_precedence(
    config: Config, tmp_path: Path
) -> None:
    # Global precedence: <data_dir>/skills > skill_directories > Extension Skills,
    # and every global source outranks a same-named bundled Skill.
    data_dir = config.data_dir
    extra = tmp_path / "external"
    write_skill(extra, "weather", "From skill_directories.")
    write_skill(extra, "shared", "From skill_directories.")
    extension_skills = write_extension_with_skill(data_dir, "ext-a", "pdf", "From the extension.")
    extension_skills = extension_skills / "skills"
    write_skill(extension_skills, "shared", "Ext.")
    write_skill(extension_skills, "mine", "From the extension.")
    write_skill(extension_skills, "ext-skill", "From an extension.")
    write_skill(data_dir / "skills", "coding-agents", "My own global skill.")
    write_skill(data_dir / "skills", "mine", "My own global skill.")
    # A disabled Extension is never imported, so its bundled Skill stays out.
    write_extension_with_skill(data_dir, "ext-off", "off-skill", "From a disabled extension.")
    write_settings(
        data_dir, {"skill_directories": [str(extra)], "extensions": {"disabled": ["ext-off"]}}
    )
    runtime = Runtime(config)
    runtime.start()
    try:
        for name, description in (
            ("weather", "From skill_directories."),
            ("pdf", "From the extension."),
            ("ext-skill", "From an extension."),
            ("coding-agents", "My own global skill."),
            ("mine", "My own global skill."),
            ("shared", "From skill_directories."),
        ):
            skill = runtime.skills.get(name)
            assert (skill.description, skill.origin) == (description, SKILL_ORIGIN_GLOBAL)
        with pytest.raises(KeyError):
            runtime.skills.get("off-skill")
        rejected = {
            (diagnostic.name, diagnostic.path)
            for diagnostic in runtime.skills.diagnostics()
            if not diagnostic.loadable
            and any("Duplicate skill name" in warning for warning in diagnostic.warnings)
        }
        assert {
            ("weather", (BUNDLED_SKILLS_ROOT / "weather" / "SKILL.md").resolve()),
            ("pdf", (BUNDLED_SKILLS_ROOT / "pdf" / "SKILL.md").resolve()),
            ("coding-agents", (BUNDLED_SKILLS_ROOT / "coding-agents" / "SKILL.md").resolve()),
            ("shared", (extension_skills / "shared" / "SKILL.md").resolve()),
            ("mine", (extension_skills / "mine" / "SKILL.md").resolve()),
        } <= rejected
        # The manager inventory names each package's kind of source, so the
        # WebUI can say why a global package is not editable.
        inventory = runtime.skill_inventory()["skills"]
        kinds = {(entry["name"], entry["description"]): entry["source_kind"] for entry in inventory}
        assert kinds[("mine", "My own global skill.")] == "home"
        assert kinds[("weather", "From skill_directories.")] == "folder"
        assert kinds[("ext-skill", "From an extension.")] == "extension"
        assert {entry["source_kind"] for entry in inventory if entry["origin"] == "bundled"} == {
            "bundled"
        }

        # Live-disabling the Extension refreshes the registry without a restart.
        asyncio.run(runtime.apply_extension_disabled_change({"ext-a"}))
        with pytest.raises(KeyError):
            runtime.skills.get("ext-skill")
        assert runtime.skills.get("pdf").origin == "bundled"
    finally:
        runtime.stop()


def test_skill_reload_updates_the_prompt_catalog_but_keeps_the_tool_set(
    runtime: Runtime, tmp_path: Path
) -> None:
    agent = runtime.agents.update(
        "main",
        tool_access={"mode": "selected", "allowed": ["skill", "skill_manage"]},
        allowed_skills=[RELOADED_SKILL_NAME],
    )
    skill_root = tmp_path / "team-skills"
    write_skill(skill_root, RELOADED_SKILL_NAME, "Fresh skill loaded after settings update.")
    prompt_before = runtime.system_prompts.build_system_prompt(agent)
    tools_before = runtime.system_prompts.provider_tool_definitions(agent)

    runtime.storage.update_settings_sections({"skills": {"directories": [str(skill_root)]}})
    runtime.reload_skills()

    prompt_after = runtime.system_prompts.build_system_prompt(agent)
    assert f"- {RELOADED_SKILL_NAME}:" not in prompt_before
    assert f"- {RELOADED_SKILL_NAME}:" in prompt_after
    assert "Fresh skill loaded after settings update." in prompt_after
    # A reload changes which Skills the live registry exposes, never the Tools.
    expected_tools = ["memory", "skill", "skill_manage"]
    assert [definition["name"] for definition in tools_before] == expected_tools
    assert [
        definition["name"] for definition in runtime.system_prompts.provider_tool_definitions(agent)
    ] == expected_tools


def test_agent_authored_skill_changes_reach_subscribers(runtime: Runtime, tmp_path: Path) -> None:
    changes: list[set[str]] = []
    unsubscribe = runtime.add_skill_changed_callback(
        lambda: changes.append(_names(runtime.skills_for(None, "main")))
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
        data_root=runtime.storage.data_dir,
        cwd=tmp_path,
    )

    def manage(arguments: dict[str, object]) -> dict[str, object]:
        return asyncio.run(runtime.tools.dispatch(context, arguments, ["skill_manage"]))

    content = "---\nname: authored\ndescription: An authored playbook.\n---\n\n# Authored\n"
    assert manage({"action": "create", "name": "authored", "content": content})["ok"] is True
    # Subscribers learn of the change once, after the Agent's Skills were refreshed.
    assert len(changes) == 1
    assert "authored" in changes[0]

    unsubscribe()
    assert manage({"action": "delete", "name": "authored"})["ok"] is True
    assert len(changes) == 1


def test_skill_tools_in_a_session_of_a_missing_project_refuse_or_miss(
    runtime: Runtime, tmp_path: Path
) -> None:
    def dispatch(tool_name: str, arguments: dict[str, object]) -> dict[str, Any]:
        context = ToolContext(
            agent_id="main",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name=tool_name,
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=runtime.storage.data_dir,
            skill_project_id="gone",
        )
        return asyncio.run(runtime.tools.dispatch(context, arguments, [tool_name]))

    # The Session's Skills come from the missing Project, so no Skill resolves.
    assert dispatch("skill", {"name": "pdf"}) == tool_failure(
        "project_not_found",
        'skill was not run: the Project "gone" that this Session\'s Skills come from does '
        "not exist. Tell the user that this Project is missing.",
        retryable=False,
    )
    # skill_manage writes only the Agent's own Skills; a name that only the missing
    # Project could provide is plainly unknown.
    result = dispatch("skill_manage", {"action": "delete", "name": "proj-only"})
    assert result["error"]["code"] == "skill_not_found"


def test_disabled_skills_leave_every_scope_until_re_enabled(
    runtime: Runtime, tmp_path: Path
) -> None:
    name = runtime.skills.list_all()[0].name
    repo = tmp_path / "repo"
    repo.mkdir()
    write_project_skill(repo, "proj-only-skill", "A project playbook.")
    project = runtime.projects.create("p", "P", repo)
    write_agent_skill(runtime.storage.data_dir, "main", "private-deploy", "Owner's own playbook.")
    assert "private-deploy" in {
        skill.name for skill in runtime.skills_for(None, "main").filter_allowed([])
    }

    # The explicit Project Context listing reads the policy without a reload.
    runtime.skill_policy.set_disabled("proj-only-skill", disabled=True)
    assert runtime.project_own_skills(project.project_id) == []

    runtime.skill_policy.set_disabled(name, disabled=True)
    runtime.skill_policy.set_disabled("private-deploy", disabled=True)
    runtime.reload_skills()

    assert name not in _names(runtime.skills)
    with pytest.raises(KeyError):
        runtime.skills.get(name)
    assert name not in {skill.name for skill in runtime.skills.filter_allowed(["*"])}
    assert runtime.skills.availability_for(name, ["*"]).state == "invalid"
    # The manager-facing excluded bucket still sees exactly what was disabled.
    assert [skill.name for skill in runtime.skills.excluded_skills()] == [name]
    # The config-Agent resolver input subtracts the disabled set too.
    assert "proj-only-skill" not in _names(runtime.skills_for(project.project_id))
    assert runtime.project_skill_names(project.project_id) == frozenset()
    # Disable beats the owner's always-allowed private Skill.
    owner_skills = runtime.skills_for(None, "main")
    assert "private-deploy" not in _names(owner_skills)
    assert "private-deploy" not in {skill.name for skill in owner_skills.filter_allowed([])}

    runtime.skill_policy.set_disabled(name, disabled=False)
    runtime.reload_skills()
    assert name in _names(runtime.skills)


def test_malformed_skill_policy_does_not_break_startup(config: Config) -> None:
    policy_file = config.data_dir / "skills" / "policy.json"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text("{not valid json", encoding="utf-8")
    runtime = Runtime(config)

    runtime.start()
    try:
        # Startup survived, every Skill stays visible, and the diagnostics surface.
        assert runtime.skills.list_all()
        assert any(
            "Cannot read skill policy" in message
            for message in runtime.skill_policy.validation_diagnostics()
        )
    finally:
        runtime.stop()
