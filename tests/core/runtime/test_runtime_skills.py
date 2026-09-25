"""Tests for runtime skills."""

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.prompts import LayoutEntry
from core.runtime.runtime import Runtime
from core.skills.skills import SKILL_ORIGIN_GLOBAL
from core.tools.tools import ToolNotFoundError
from core.utils.config import Config
from server.rpc.skill_methods import method_handlers
from tests.core.runtime.runtime_test_support import (
    _authorize_session_store,
)
from tests.core.runtime.runtime_test_support import (
    config as config,
)

RELOADED_SKILL_NAME = "runtime-reloaded-skill"


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_reload", ["sync", "async"])
async def test_older_skill_scan_cannot_overwrite_newer_policy(config, monkeypatch, newer_reload):
    _write_test_skill(config.data_dir / "skills", RELOADED_SKILL_NAME, "Reload race fixture.")
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    owner = runtime._skill_operations()
    original_load = owner.load_global_registry

    def controlled_load():
        snapshot = original_load()
        if not entered.is_set():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
        return snapshot

    monkeypatch.setattr(owner, "load_global_registry", controlled_load)
    older = asyncio.create_task(runtime.reload_skills_async())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.skill_policy.set_disabled(RELOADED_SKILL_NAME, disabled=True)
        if newer_reload == "sync":
            runtime.reload_skills()
        else:
            await runtime.reload_skills_async()
        latest = runtime.skills
        with pytest.raises(KeyError):
            latest.get(RELOADED_SKILL_NAME)
        release.set()
        await asyncio.wait_for(older, 5)
        assert runtime.skills is latest
        with pytest.raises(KeyError):
            runtime.skills_for(None, "main").get(RELOADED_SKILL_NAME)
    finally:
        release.set()
        await asyncio.gather(older, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["stop", "aclose"])
@pytest.mark.parametrize("restart", [False, True])
async def test_skill_scan_cannot_publish_across_runtime_shutdown(
    config, monkeypatch, shutdown, restart
):
    _write_test_skill(config.data_dir / "skills", RELOADED_SKILL_NAME, "Reload race fixture.")
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    owner = runtime._skill_operations()
    original_load = owner.load_global_registry

    def controlled_load():
        snapshot = original_load()
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return snapshot

    monkeypatch.setattr(owner, "load_global_registry", controlled_load)
    older = asyncio.create_task(runtime.reload_skills_async())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime.skill_policy.set_disabled(RELOADED_SKILL_NAME, disabled=True)
        if shutdown == "stop":
            runtime.stop()
        else:
            await runtime.aclose()
        if restart:
            runtime.start()
        latest = runtime._skills
        release.set()
        await asyncio.wait_for(older, 5)
        assert runtime._skills is latest
        if restart:
            with pytest.raises(KeyError):
                runtime.skills.get(RELOADED_SKILL_NAME)
        else:
            assert runtime._skills is None
    finally:
        release.set()
        await asyncio.gather(older, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_change", ["add", "remove", "process_override"])
async def test_global_scan_keeps_latest_credential_availability(
    config, monkeypatch, credential_change
):
    key = "VBOT_GLOBAL_SKILL_SCAN_CREDENTIAL"
    monkeypatch.delenv(key, raising=False)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    package = runtime.global_skills_dir / RELOADED_SKILL_NAME
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {RELOADED_SKILL_NAME}\ndescription: Credential race fixture.\n"
        f"metadata:\n  vbot:\n    requirements:\n      env: {key}\n---\nBody\n",
        encoding="utf-8",
    )
    if credential_change in {"remove", "process_override"}:
        runtime.storage.set_data_dir_credential(key, "test-initial")
        runtime.reload_environment_credentials()
    runtime.reload_skills()
    held_registry = runtime.skills
    new_package = runtime.global_skills_dir / "added-during-credential-refresh"
    new_package.mkdir()
    (new_package / "SKILL.md").write_text(
        "---\nname: added-during-credential-refresh\ndescription: Pending addition.\n---\nBody\n",
        encoding="utf-8",
    )
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    owner = runtime._skill_operations()
    original_load = owner.load_global_registry

    def scan():
        registry = original_load()
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return registry

    monkeypatch.setattr(owner, "load_global_registry", scan)
    pending = asyncio.create_task(runtime.reload_skills_async())
    try:
        await entered.wait()
        if credential_change == "add":
            runtime.storage.set_data_dir_credential(key, "test-current")
        elif credential_change == "remove":
            runtime.storage.remove_data_dir_credential(key)
        if credential_change == "process_override":
            # Even an explicitly empty process value takes precedence over the
            # file snapshot captured by the pending scan.
            monkeypatch.setenv(key, "")
        runtime.reload_environment_credentials()
        expected = "available" if credential_change == "add" else "unavailable"
        assert held_registry.availability_for(RELOADED_SKILL_NAME, ["*"]).state == expected
        release.set()
        await pending
        assert runtime.skills is not held_registry
        assert runtime.skills.get("added-during-credential-refresh").name
        assert runtime.skills.availability_for(RELOADED_SKILL_NAME, ["*"]).state == expected
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.aclose()


def test_credential_reload_updates_existing_skill_registries(config, tmp_path, monkeypatch):
    key = "VBOT_SKILL_RELOAD_TEST"
    monkeypatch.delenv(key, raising=False)
    runtime = Runtime(config)
    runtime.start()
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        project = runtime.projects.create("p", "P", repo)
        homes = [
            runtime.global_skills_dir,
            runtime.agent_skills_dir("main"),
            repo / ".opencode" / "skills",
        ]
        for home, name in zip(homes, ["global-env", "private-env", "project-env"], strict=True):
            package = home / name
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Requires a credential.\n"
                f"metadata:\n  vbot:\n    requirements:\n      env: {key}\n---\nBody\n",
                encoding="utf-8",
            )
        runtime.reload_skills()
        scopes = [
            (None, None),
            (None, "main"),
            (project.project_id, None),
            (project.project_id, "main"),
        ]
        registries = [runtime.skills_for(*scope) for scope in scopes]

        def assert_availability(expected):
            for scope, registry in zip(scopes, registries, strict=True):
                assert runtime.skills_for(*scope) is registry
                for skill in registry.list_all():
                    if skill.name.endswith("-env"):
                        assert registry.availability_for(skill.name, ["*"]).state == expected

        assert_availability("unavailable")
        runtime.storage.set_data_dir_credential(key, "test-value")
        runtime.reload_environment_credentials()
        assert_availability("available")
        runtime.storage.remove_data_dir_credential(key)
        runtime.reload_environment_credentials()
        assert_availability("unavailable")
        monkeypatch.setenv(key, "process-value")
        runtime.reload_environment_credentials()
        assert_availability("available")
    finally:
        runtime.stop()


def test_install_private_global_and_replace_shared_skill_refresh_live_visibility(config, tmp_path):
    runtime = Runtime(config)
    runtime.start()
    source = tmp_path / "incoming"
    source.mkdir()
    document = "---\nname: imported\ndescription: First description.\n---\nBody\n"
    (source / "SKILL.md").write_text(document)
    try:
        runtime.agents.create("receiver", "Receiver", allowed_skills=["imported"])
        runtime.agents.update("main", allowed_skills=[])
        runtime.skills_for(None, "main")
        runtime.skills_for(None, "receiver")
        state = SimpleNamespace(runtime=runtime)
        install = method_handlers()["skill.install"]
        asyncio.run(install(state, {"source": str(source), "scope": "agent:main"}))
        assert [item.name for item in runtime.skills_for(None, "main").filter_allowed([])] == [
            "imported"
        ]
        assert "imported" not in {
            item.name for item in runtime.skills_for(None, "receiver").list_all()
        }
        runtime.skill_policy.set_shared("main", "imported", shared=True, receivers=["receiver"])
        runtime.invalidate_agent_skills(None)
        assert (
            runtime.skills_for(None, "receiver").get("imported").description == "First description."
        )
        (source / "SKILL.md").write_text(document.replace("First", "Updated"))
        asyncio.run(install(state, {"source": str(source), "scope": "agent:main", "replace": True}))
        assert (
            runtime.skills_for(None, "receiver").get("imported").description
            == "Updated description."
        )
        (source / "SKILL.md").write_text(document.replace("imported", "global-import"))
        asyncio.run(install(state, {"source": str(source), "scope": "global"}))
        assert runtime.skills_for(None, "main").get("global-import")
        assert "global-import" not in {
            item.name for item in runtime.skills_for(None, "main").filter_allowed([])
        }
        runtime.skill_policy.set_disabled("imported", disabled=True)
        runtime.reload_skills()
        assert "imported" not in {item.name for item in runtime.skills_for(None, "main").list_all()}
    finally:
        runtime.stop()


def test_reload_skills_updates_system_prompt_skill_registry(config: Config, tmp_path: Path):
    """Runtime.reload_skills() makes prompt catalogs use the fresh skill registry."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    agent = runtime.agents.update("main", allowed_skills=[RELOADED_SKILL_NAME])
    skill_root = tmp_path / "team-skills"
    _write_test_skill(
        skill_root,
        RELOADED_SKILL_NAME,
        "Fresh skill loaded after settings update.",
    )

    prompt_before_reload = runtime.system_prompts.build_system_prompt(agent)

    runtime.storage.update_settings_sections({"skills": {"directories": [str(skill_root)]}})
    runtime.reload_skills()
    prompt_after_reload = runtime.system_prompts.build_system_prompt(agent)

    assert f"- {RELOADED_SKILL_NAME}:" not in prompt_before_reload
    assert f"- {RELOADED_SKILL_NAME}:" in prompt_after_reload
    assert "Fresh skill loaded after settings update." in prompt_after_reload


def test_persisted_block_layout_and_override_flow_through_real_storage(config: Config):
    """The runtime wires the real StorageManager block store into the prompt manager.

    Guards the composition-root seam (_StorageManagerBlockStore): a persisted block
    override and a persisted layout written through StorageManager's block write API
    must actually shape the assembled prompt — i.e. the manager is NOT silently on
    the EmptyBlockStore fallback. The adapter bridges both the method names
    (read_layout → read_block_layout) and the scope convention (default scope key →
    None storage token).
    """
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        agent = runtime.agents.get("main")

        # Baseline: bundled tools block present, custom marker absent, skills present.
        baseline = runtime.system_prompts.build_system_prompt(agent)
        assert "## Tool Call Style" in baseline
        assert "PERSISTED-OVERRIDE-MARKER" not in baseline
        assert "## Available Skills" in baseline

        # Persist a default-scope override for the tools block and a layout that
        # disables the skills block — both through StorageManager's block write API
        # (scope None = default). The adapter reads these live on the next build.
        runtime.storage.write_block_override(
            None,
            "core:tools",
            "## PERSISTED-OVERRIDE-MARKER\n{generated:tool_list}",
        )
        runtime.storage.write_block_layout(
            None,
            [
                LayoutEntry(id="core:runtime", enabled=True, source="core"),
                LayoutEntry(id="core:tools", enabled=True, source="core"),
                LayoutEntry(id="core:skills", enabled=False, source="core"),
            ],
        )

        updated = runtime.system_prompts.build_system_prompt(agent)

        # The persisted override replaced the bundled tools text (override cascade).
        assert "## PERSISTED-OVERRIDE-MARKER" in updated
        assert "## Tool Call Style" not in updated
        # The persisted layout disabled the skills block (gate 1).
        assert "## Available Skills" not in updated
    finally:
        runtime.stop()


def test_block_edit_facade_writes_flow_through_real_storage(config: Config):
    """The block-edit facade's writes round-trip through the real StorageManager.

    Guards the write half of the composition-root seam (_StorageManagerBlockStore):
    update_block / set_layout / create_block / remove_block must persist through
    StorageManager's block write API (scope translation included) and shape the very
    next assembled prompt — proving the adapter is not on the EmptyBlockStore no-op
    sink. The facade lives on the runtime's SystemPromptManager.
    """
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    try:
        manager = runtime.system_prompts
        agent = runtime.agents.get("main")

        # update_block: a default-scope override on the tools block replaces the
        # bundled text on the next build (write_block_override → storage scope None).
        manager.update_block("core:tools", "## FACADE-TOOLS-MARKER\n{generated:tool_list}")
        assert (
            runtime.storage.read_block_override(None, "core:tools")
            == "## FACADE-TOOLS-MARKER\n{generated:tool_list}"
        )
        assert "## FACADE-TOOLS-MARKER" in manager.build_system_prompt(agent)

        # set_layout: disabling the skills block persists and gates it out (prune via
        # storage). An unknown id is tolerated — pruned, not an error.
        manager.set_layout(
            [
                {"id": "core:runtime", "enabled": True},
                {"id": "core:tools", "enabled": True},
                {"id": "core:skills", "enabled": False},
                {"id": "extension:gone", "enabled": True},
            ]
        )
        persisted_ids = {entry.id for entry in runtime.storage.read_block_layout(None)}
        assert "extension:gone" not in persisted_ids  # contributor-gone id pruned
        assert "## Available Skills" not in manager.build_system_prompt(agent)

        # create_block then remove_block: the custom block's override file and layout
        # entry are written, then both removed, all through the real storage seam.
        manager.create_block("greeting", "Hello from a custom block.")
        assert runtime.storage.read_block_override(None, "user:greeting") is not None
        assert "Hello from a custom block." in manager.build_system_prompt(agent)

        manager.remove_block("user:greeting")
        assert runtime.storage.read_block_override(None, "user:greeting") is None
        assert "Hello from a custom block." not in manager.build_system_prompt(agent)
    finally:
        runtime.stop()


def test_reload_skills_keeps_provider_tool_set_stable(config: Config, tmp_path: Path):
    """When an identity agent allows the skill tools, a skill reload never changes the
    tool set — only which skills the live registry exposes."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    agent = runtime.agents.update(
        "main",
        tool_access={"mode": "selected", "allowed": ["skill", "skill_manage"]},
        allowed_skills=[RELOADED_SKILL_NAME],
    )
    skill_root = tmp_path / "team-skills"
    _write_test_skill(
        skill_root,
        RELOADED_SKILL_NAME,
        "Fresh skill loaded after settings update.",
    )

    definitions_before_reload = runtime.system_prompts.provider_tool_definitions(agent)

    runtime.storage.update_settings_sections({"skills": {"directories": [str(skill_root)]}})
    runtime.reload_skills()
    definitions_after_reload = runtime.system_prompts.provider_tool_definitions(agent)

    expected_tools = ["memory", "skill", "skill_manage"]
    assert [definition["name"] for definition in definitions_before_reload] == expected_tools
    assert [definition["name"] for definition in definitions_after_reload] == expected_tools


def _write_test_skill(skill_root: Path, name: str, description: str) -> None:
    skill_dir = skill_root / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nUse this skill.\n",
        encoding="utf-8",
    )


def _write_project_skill(repo: Path, name: str, description: str) -> None:
    """Write a project-owned skill under ``<repo>/.opencode/skills/<name>/``."""
    _write_test_skill(repo / ".opencode" / "skills", name, description)


def _write_agent_skill(data_dir: Path, agent_id: str, name: str, description: str) -> None:
    """Write an agent-private skill under ``<data_dir>/agents/<id>/skills/<name>/``."""
    _write_test_skill(data_dir / "agents" / agent_id / "skills", name, description)


def _write_extension_with_skill(
    data_dir: Path, ext_name: str, skill_name: str, description: str
) -> None:
    """Write a package extension bundling one skill under ``<ext>/skills/<name>/``."""
    ext_dir = data_dir / "extensions" / ext_name
    ext_dir.mkdir(parents=True)
    _authorize_session_store(data_dir)
    ext_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
    _write_test_skill(ext_dir / "skills", skill_name, description)


def test_skills_for_none_returns_global_registry(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    # The identity path is byte-identical to the global registry — no scoping.
    assert runtime.skills_for(None) is runtime.skills


def test_skills_for_project_merges_project_and_bundled(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-only-skill", "A project-scoped playbook.")
    project = runtime.projects.create("p", "P", repo)

    registry = runtime.skills_for(project.project_id)

    names = {skill.name for skill in registry.list_all()}
    # The project's own skill plus the entire bundled pool are visible.
    assert "proj-only-skill" in names
    assert {skill.name for skill in runtime.skills.list_all()}.issubset(names)


def test_skills_for_project_skill_wins_name_collision(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    bundled_name = next(
        skill.name for skill in runtime.skills.list_all() if skill.origin == "bundled"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, bundled_name, "Project override of a bundled skill.")
    project = runtime.projects.create("p", "P", repo)

    registry = runtime.skills_for(project.project_id)

    # The project skill shadows the bundled one of the same name (one slot, project wins).
    assert registry.get(bundled_name).description == "Project override of a bundled skill."


def test_identity_project_context_grants_effective_project_skills(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    bundled_name = runtime.skills.filter_allowed(["*"])[0].name
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "active-project-skill", "Active Project workflow.")
    _write_project_skill(repo, "disabled-project-skill", "Disabled Project workflow.")
    runtime.projects.create("p", "P", repo)
    project = runtime.projects.update(
        "p",
        skills_project_disabled=["disabled-project-skill"],
        skills_bundled_enabled=[bundled_name],
    )

    registry = runtime.skills_for(project.project_id, "main")
    allowed_with_empty_identity_list = {skill.name for skill in registry.filter_allowed([])}

    assert "active-project-skill" in allowed_with_empty_identity_list
    assert bundled_name in allowed_with_empty_identity_list
    assert "disabled-project-skill" not in allowed_with_empty_identity_list


def test_project_context_skills_returns_complete_effective_set(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    bundled_name = runtime.skills.list_all()[0].name
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "active-project-skill", "Active Project workflow.")
    _write_project_skill(repo, "disabled-project-skill", "Disabled Project workflow.")
    runtime.projects.create("p", "P", repo)
    project = runtime.projects.update(
        "p",
        skills_project_disabled=["disabled-project-skill"],
        skills_bundled_enabled=[bundled_name],
    )

    names = {skill.name for skill in runtime.project_context_skills(project.project_id)}

    assert names == {"active-project-skill", bundled_name}


def test_project_skill_names_returns_project_owned_only(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha.")
    _write_project_skill(repo, "beta", "Beta.")
    project = runtime.projects.create("p", "P", repo)

    assert runtime.project_skill_names(project.project_id) == frozenset({"alpha", "beta"})
    assert runtime.project_skill_names(None) == frozenset()


def test_skills_for_claude_project_reads_claude_skills_dir(config: Config, tmp_path: Path) -> None:
    # A claude project resolves its own skills from .claude/skills/ — the opencode
    # directory is invisible to it (one format per project, no mixing).
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_test_skill(repo / ".claude" / "skills", "claude-skill", "Claude playbook.")
    _write_test_skill(repo / ".opencode" / "skills", "opencode-skill", "OpenCode playbook.")
    project = runtime.projects.create("p", "P", repo, source_format="claude")

    registry = runtime.skills_for(project.project_id)

    names = {skill.name for skill in registry.list_all()}
    assert "claude-skill" in names
    assert "opencode-skill" not in names
    assert runtime.project_skill_names(project.project_id) == frozenset({"claude-skill"})
    # Explicit Project Context sees the same format-scoped set.
    assert [skill.name for skill in runtime.project_own_skills(project.project_id)] == [
        "claude-skill"
    ]


def test_skills_for_project_is_cached_until_invalidated(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha.")
    project = runtime.projects.create("p", "P", repo)

    first = runtime.skills_for(project.project_id)
    assert runtime.skills_for(project.project_id) is first  # cached, not re-scanned
    runtime.invalidate_project_skills(project.project_id)
    assert runtime.skills_for(project.project_id) is not first  # rebuilt after invalidation


def test_invalidate_project_skills_reflects_cwd_change(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    _write_project_skill(repo_a, "skill-a", "From repo A.")
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    _write_project_skill(repo_b, "skill-b", "From repo B.")
    project = runtime.projects.create("p", "P", repo_a)
    assert runtime.project_skill_names(project.project_id) == frozenset({"skill-a"})

    runtime.projects.update(project.project_id, cwd=str(repo_b))
    runtime.invalidate_project_skills(project.project_id)

    assert runtime.project_skill_names(project.project_id) == frozenset({"skill-b"})


def test_reload_skills_drops_project_skill_cache(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha.")
    project = runtime.projects.create("p", "P", repo)
    first = runtime.skills_for(project.project_id)

    runtime.reload_skills()

    # A global skill reload makes project registries stale, so they rebuild.
    assert runtime.skills_for(project.project_id) is not first


def test_refresh_skills_for_rescans_project_and_global_sources(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "alpha", "Alpha.")
    project = runtime.projects.create("p", "P", repo)
    first = runtime.skills_for(project.project_id)
    _write_project_skill(repo, "beta", "Beta.")
    _write_test_skill(runtime.global_skills_dir, "global-new", "New global Skill.")

    refreshed = runtime.refresh_skills_for(project.project_id)

    assert refreshed is not first
    assert {skill.name for skill in refreshed.list_all()}.issuperset(
        {"alpha", "beta", "global-new"}
    )


def test_agent_skills_dir_path(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    assert (
        runtime.agent_skills_dir("main") == runtime.storage.data_dir / "agents" / "main" / "skills"
    )


def test_skills_for_agent_includes_own_private_skills(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "my-private", "An agent-only playbook.")

    registry = runtime.skills_for(None, "main")

    names = {skill.name for skill in registry.list_all()}
    assert "my-private" in names
    # The same skill is invisible to the agent-less global resolution.
    assert "my-private" not in {skill.name for skill in runtime.skills.list_all()}


def test_agent_own_skill_bypasses_owner_allowlist(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "my-private", "An agent-only playbook.")

    registry = runtime.skills_for(None, "main")

    # An empty allow-list normally exposes nothing, but the agent's own skill is
    # always allowed for its owner — and only it (no bundled skill leaks in).
    assert registry.is_allowed("my-private", [])
    assert [skill.name for skill in registry.filter_allowed([])] == ["my-private"]


def test_agent_own_skills_isolated_between_agents(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "main-only", "Main's private playbook.")

    # Another agent with no private home falls through to the global pool, which never
    # contains main's private skill.
    other_registry = runtime.skills_for(None, "other")
    assert "main-only" not in {skill.name for skill in other_registry.list_all()}


def test_skills_for_ignores_private_home_of_nonexistent_identity_agent(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    # A skills directory under ``agents/<id>/`` that belongs to no stored identity
    # agent (left behind, or crafted for a project-team slug) must never be layered:
    # private skills are identity-only and always-allowed for their owner, so an
    # unowned home would bypass every allow-list.
    _write_agent_skill(runtime.storage.data_dir, "ghost", "ghost-skill", "Nobody's playbook.")

    registry = runtime.skills_for(None, "ghost")

    assert "ghost-skill" not in {skill.name for skill in registry.list_all()}
    assert registry is runtime.skills


def test_skills_for_identity_without_own_skills_is_global(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()

    # No private skills home → byte-identical to the global registry (same object).
    assert runtime.skills_for(None, "main") is runtime.skills


def test_skills_for_agent_layers_own_over_project(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-skill", "Project playbook.")
    _write_project_skill(repo, "shared", "Project version.")
    project = runtime.projects.create("p", "P", repo)
    _write_agent_skill(runtime.storage.data_dir, "main", "agent-skill", "Agent playbook.")
    _write_agent_skill(runtime.storage.data_dir, "main", "shared", "Agent version.")

    registry = runtime.skills_for(project.project_id, "main")

    names = {skill.name for skill in registry.list_all()}
    assert {"agent-skill", "proj-skill"}.issubset(names)
    # Agent skills are scanned first, so the agent wins a name collision with project.
    assert registry.get("shared").description == "Agent version."


def test_invalidate_agent_skills_drops_only_that_agent(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "main-skill", "Main.")
    _write_agent_skill(runtime.storage.data_dir, "two", "two-skill", "Two.")
    main_first = runtime.skills_for(None, "main")
    two_first = runtime.skills_for(None, "two")
    assert runtime.skills_for(None, "main") is main_first  # cached

    runtime.invalidate_agent_skills("main")

    assert runtime.skills_for(None, "main") is not main_first  # main rebuilt
    assert runtime.skills_for(None, "two") is two_first  # two untouched


def test_reload_skills_drops_agent_cache(config: Config) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    _write_agent_skill(runtime.storage.data_dir, "main", "main-skill", "Main.")
    first = runtime.skills_for(None, "main")

    runtime.reload_skills()

    assert runtime.skills_for(None, "main") is not first


def test_invalidate_project_skills_drops_matching_agent_cache(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-skill", "Project.")
    project = runtime.projects.create("p", "P", repo)
    _write_agent_skill(runtime.storage.data_dir, "main", "agent-skill", "Agent.")
    first = runtime.skills_for(project.project_id, "main")

    # The agent registry embeds the project layer, so a project invalidation drops it.
    runtime.invalidate_project_skills(project.project_id)

    assert runtime.skills_for(project.project_id, "main") is not first


def test_project_own_skills_returns_scanned_project_metadata(
    config: Config, tmp_path: Path
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "deploy", "Ship it.")
    project = runtime.projects.create("p", "P", repo)

    skills = runtime.project_own_skills(project.project_id)

    # Only the project's own skills (no bundled), with their real SKILL.md paths.
    assert [skill.name for skill in skills] == ["deploy"]
    assert skills[0].path == (repo / ".opencode" / "skills" / "deploy" / "SKILL.md").resolve()


def test_skills_for_tags_origin_per_scope(config: Config, tmp_path: Path) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)
    runtime.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_project_skill(repo, "proj-skill", "Project.")
    project = runtime.projects.create("p", "P", repo)
    _write_agent_skill(runtime.storage.data_dir, "main", "mine", "Mine.")
    bundled_name = next(
        skill.name for skill in runtime.skills.list_all() if skill.origin == "bundled"
    )

    registry = runtime.skills_for(project.project_id, "main")

    assert registry.get("mine").origin == "agent"
    assert registry.get("proj-skill").origin == "project:P"
    assert registry.get(bundled_name).origin == "bundled"


def test_extension_bundled_skill_loads_as_global(config: Config) -> None:
    # A loaded extension bundling ``<ext>/skills/<name>/`` contributes that skill to
    # the global pool, tagged ``global`` like any other global skill (no code).
    logging.getLogger("vbot").handlers = []
    _write_extension_with_skill(config.data_dir, "ext-a", "ext-skill", "From an extension.")
    runtime = Runtime(config)
    runtime.start()

    assert runtime.skills.get("ext-skill").description == "From an extension."
    assert runtime.skills.get("ext-skill").origin == SKILL_ORIGIN_GLOBAL


def test_disabled_extension_contributes_no_skill(config: Config) -> None:
    # An extension in the disabled set is never imported, so its bundled skill
    # stays out of the pool.
    logging.getLogger("vbot").handlers = []
    _write_extension_with_skill(config.data_dir, "ext-a", "ext-skill", "From an extension.")
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"format_version": 1, "extensions": {"disabled": ["ext-a"]}}),
        encoding="utf-8",
    )
    runtime = Runtime(config)
    runtime.start()

    with pytest.raises(KeyError):
        runtime.skills.get("ext-skill")


def test_own_global_skill_wins_over_extension_skill(config: Config) -> None:
    # ``<data_dir>/skills`` is scanned before extension skill dirs, so a hand-authored
    # global skill wins a name collision with an extension's.
    logging.getLogger("vbot").handlers = []
    _write_extension_with_skill(config.data_dir, "ext-a", "shared", "From the extension.")
    _write_test_skill(config.data_dir / "skills", "shared", "My own global skill.")
    runtime = Runtime(config)
    runtime.start()

    assert runtime.skills.get("shared").description == "My own global skill."


def test_global_skill_sources_win_over_bundled_in_documented_order(
    config: Config, tmp_path: Path
) -> None:
    # Global precedence: <data_dir>/skills > skill_directories > Extension skills,
    # and every global source outranks a same-named bundled Skill.
    logging.getLogger("vbot").handlers = []
    bundled_root = Path(__file__).resolve().parents[3] / "resources" / "skills"
    extra = tmp_path / "external"
    _write_test_skill(extra, "weather", "From skill_directories.")
    _write_test_skill(extra, "shared", "From skill_directories.")
    _write_extension_with_skill(config.data_dir, "ext-a", "pdf", "From the extension.")
    _write_test_skill(config.data_dir / "extensions" / "ext-a" / "skills", "shared", "Ext.")
    _write_test_skill(config.data_dir / "skills", "coding-agents", "My own global skill.")
    config.data_dir.joinpath("settings.json").write_text(
        json.dumps({"format_version": 1, "skill_directories": [str(extra)]}), encoding="utf-8"
    )
    runtime = Runtime(config)
    runtime.start()

    for name, description in (
        ("weather", "From skill_directories."),
        ("pdf", "From the extension."),
        ("coding-agents", "My own global skill."),
        ("shared", "From skill_directories."),
    ):
        skill = runtime.skills.get(name)
        assert (skill.description, skill.origin) == (description, SKILL_ORIGIN_GLOBAL)
    rejected = {
        (diagnostic.name, diagnostic.path)
        for diagnostic in runtime.skills.diagnostics()
        if not diagnostic.loadable
        and any("Duplicate skill name" in warning for warning in diagnostic.warnings)
    }
    extension_shared = config.data_dir / "extensions" / "ext-a" / "skills" / "shared"
    assert {
        ("weather", (bundled_root / "weather" / "SKILL.md").resolve()),
        ("pdf", (bundled_root / "pdf" / "SKILL.md").resolve()),
        ("coding-agents", (bundled_root / "coding-agents" / "SKILL.md").resolve()),
        ("shared", (extension_shared / "SKILL.md").resolve()),
    } <= rejected


def test_disabling_extension_live_drops_its_skill(config: Config) -> None:
    # Live-deactivating an extension refreshes the skill registry, so its bundled
    # skill disappears without a restart.
    logging.getLogger("vbot").handlers = []
    _write_extension_with_skill(config.data_dir, "ext-a", "ext-skill", "From an extension.")
    runtime = Runtime(config)
    runtime.start()
    assert runtime.skills.get("ext-skill").description == "From an extension."

    asyncio.run(runtime.apply_extension_disabled_change({"ext-a"}))

    with pytest.raises(KeyError):
        runtime.skills.get("ext-skill")


def test_playwright_replaces_archived_browser_extension(config: Config) -> None:
    runtime = Runtime(config)
    runtime.start()
    try:
        skill = runtime.skills.get("playwright-cli")
        assert (
            skill.path.parent
            == Path(__file__).resolve().parents[3] / "resources/skills/playwright-cli"
        )
        assert skill.requirements.empty
        assert runtime.extensions is not None
        assert not any(record.name == "browser_use" for record in runtime.extensions.records())
        with pytest.raises(KeyError):
            runtime.skills.get("browser-use")
        with pytest.raises(ToolNotFoundError):
            runtime.tools.get("browser")
    finally:
        asyncio.run(runtime.aclose())
