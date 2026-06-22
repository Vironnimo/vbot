"""Tests for the project anchor lifecycle (ProjectStore)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.projects.paths import cwd_exists
from core.projects.projects import (
    PROJECT_DEFAULT_ALLOWED_TOOLS,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
)
from core.projects.store import ProjectStore
from core.sessions import ChatSessionManager


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repos" / "vbot"
    repo_dir.mkdir(parents=True)
    return repo_dir


def test_create_writes_anchor_layout(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)

    project = store.create("vbot", "vBot", repo)

    anchor = data_dir / "projects" / "vbot"
    assert (anchor / "project.json").is_file()
    assert (anchor / "agents").is_dir()
    assert project.cwd == str(Path(os.path.realpath(repo)))


def test_create_persists_validatable_config(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo, default_agent="orchestrator", auto_load=["AGENTS.md"])

    payload = json.loads((data_dir / "projects" / "vbot" / "project.json").read_text("utf-8"))
    assert payload["project_id"] == "vbot"
    assert payload["default_agent"] == "orchestrator"
    assert payload["auto_load"] == ["AGENTS.md"]


def test_create_seeds_agents_file_into_empty_auto_load(data_dir: Path, repo: Path) -> None:
    # A new project starts with AGENTS.md seeded as its first (and only) auto-load
    # entry — the convention loads with zero config, yet stays a removable entry.
    store = ProjectStore(data_dir)

    project = store.create("vbot", "vBot", repo)

    assert project.auto_load == ["AGENTS.md"]


def test_create_prepends_agents_file_before_user_auto_load(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)

    project = store.create("vbot", "vBot", repo, auto_load=["docs/guide.md", "CONTEXT.md"])

    assert project.auto_load == ["AGENTS.md", "docs/guide.md", "CONTEXT.md"]


def test_create_does_not_duplicate_agents_file(data_dir: Path, repo: Path) -> None:
    # Idempotent: a caller that already named AGENTS.md (any case) is not seeded a
    # second copy, so the file never renders twice.
    store = ProjectStore(data_dir)

    project = store.create("vbot", "vBot", repo, auto_load=["agents.md", "CONTEXT.md"])

    assert project.auto_load == ["agents.md", "CONTEXT.md"]


def test_create_seeds_base_tool_whitelist_and_empty_skill_lists(data_dir: Path, repo: Path) -> None:
    # A new project starts at the base Tool Whitelist ceiling; the Skill Whitelist
    # rule lists start empty (only the project's own scanned skills are active).
    store = ProjectStore(data_dir)

    project = store.create("vbot", "vBot", repo)

    assert project.allowed_tools == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert project.skills_bundled_enabled == []
    assert project.skills_project_disabled == []


def test_create_persists_whitelist_fields_to_disk(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    payload = json.loads((data_dir / "projects" / "vbot" / "project.json").read_text("utf-8"))
    assert payload["allowed_tools"] == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert payload["skills_bundled_enabled"] == []
    assert payload["skills_project_disabled"] == []


def test_update_round_trips_whitelist_fields(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    updated = store.update(
        "vbot",
        allowed_tools=["read", "grep"],
        skills_bundled_enabled=["frontend-design"],
        skills_project_disabled=["debugging"],
    )

    assert updated.allowed_tools == ["read", "grep"]
    assert updated.skills_bundled_enabled == ["frontend-design"]
    assert updated.skills_project_disabled == ["debugging"]
    reloaded = store.get("vbot")
    assert reloaded.allowed_tools == ["read", "grep"]
    assert reloaded.skills_bundled_enabled == ["frontend-design"]
    assert reloaded.skills_project_disabled == ["debugging"]


def test_load_falls_back_to_base_tools_for_old_config(data_dir: Path, repo: Path) -> None:
    # An old project.json missing the whitelist fields loads at the base defaults,
    # without any migration step.
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    config_path = data_dir / "projects" / "vbot" / "project.json"
    payload = json.loads(config_path.read_text("utf-8"))
    del payload["allowed_tools"]
    del payload["skills_bundled_enabled"]
    del payload["skills_project_disabled"]
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = store.get("vbot")

    assert reloaded.allowed_tools == list(PROJECT_DEFAULT_ALLOWED_TOOLS)
    assert reloaded.skills_bundled_enabled == []
    assert reloaded.skills_project_disabled == []


def test_set_model_override_persists_one_entry(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    updated = store.set_model_override("vbot", "builder", "openai/gpt-5")

    assert updated.model_overrides == {"builder": "openai/gpt-5"}
    payload = json.loads((data_dir / "projects" / "vbot" / "project.json").read_text("utf-8"))
    assert payload["model_overrides"] == {"builder": "openai/gpt-5"}


def test_set_model_override_replaces_existing_leaves_others(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    store.set_model_override("vbot", "builder", "openai/gpt-5")
    store.set_model_override("vbot", "planner", "anthropic/claude-sonnet-4")

    updated = store.set_model_override("vbot", "builder", "openai/gpt-mini")

    # Exactly the targeted entry changed; the other agent's override is intact.
    assert updated.model_overrides == {
        "builder": "openai/gpt-mini",
        "planner": "anthropic/claude-sonnet-4",
    }


def test_clear_model_override_removes_only_target(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    store.set_model_override("vbot", "builder", "openai/gpt-5")
    store.set_model_override("vbot", "planner", "anthropic/claude-sonnet-4")

    updated = store.clear_model_override("vbot", "builder")

    assert updated.model_overrides == {"planner": "anthropic/claude-sonnet-4"}
    assert store.get("vbot").model_overrides == {"planner": "anthropic/claude-sonnet-4"}


def test_clear_model_override_absent_entry_is_noop(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    store.set_model_override("vbot", "planner", "anthropic/claude-sonnet-4")

    # Clearing an agent that has no override succeeds and changes nothing.
    updated = store.clear_model_override("vbot", "builder")

    assert updated.model_overrides == {"planner": "anthropic/claude-sonnet-4"}


def test_set_model_override_rejects_empty_model(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    with pytest.raises(ProjectError):
        store.set_model_override("vbot", "builder", "  ")


def test_set_model_override_raises_for_unknown_project(data_dir: Path) -> None:
    store = ProjectStore(data_dir)

    with pytest.raises(ProjectNotFoundError):
        store.set_model_override("missing", "builder", "openai/gpt-5")


def test_update_preserves_model_overrides_across_unrelated_edit(
    data_dir: Path, repo: Path
) -> None:
    # model_overrides is carried through an unrelated update, never dropped.
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    store.set_model_override("vbot", "builder", "openai/gpt-5")

    updated = store.update("vbot", display_name="vBot Renamed")

    assert updated.model_overrides == {"builder": "openai/gpt-5"}


def test_update_rejects_model_overrides_as_generic_field(data_dir: Path, repo: Path) -> None:
    # model_overrides has its own set/clear seam; it is not a generic update field.
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    with pytest.raises(ProjectError):
        store.update("vbot", model_overrides={"builder": "openai/gpt-5"})


def test_update_does_not_reseed_agents_file(data_dir: Path, repo: Path) -> None:
    # Seeding is creation-only: clearing the list through update keeps it cleared,
    # so a user who removes AGENTS.md is not fought by a re-seed.
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    updated = store.update("vbot", auto_load=[])

    assert updated.auto_load == []


def test_create_rejects_duplicate_id(data_dir: Path, repo: Path, tmp_path: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    other_repo = tmp_path / "repos" / "other"
    other_repo.mkdir(parents=True)

    with pytest.raises(ProjectAlreadyExistsError):
        store.create("vbot", "vBot Again", other_repo)


def test_create_rejects_same_cwd_twice(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    with pytest.raises(ProjectAlreadyExistsError):
        store.create("vbot-2", "vBot Copy", repo)


def test_create_rejects_same_cwd_with_trailing_slash(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    with pytest.raises(ProjectAlreadyExistsError):
        store.create("vbot-2", "vBot Copy", f"{repo}{os.sep}")


def test_create_allows_missing_cwd_folder(data_dir: Path, tmp_path: Path) -> None:
    # A bare/not-yet-existing repo is detected at open time, not rejected here.
    store = ProjectStore(data_dir)
    missing = tmp_path / "repos" / "not-cloned-yet"

    project = store.create("future", "Future", missing)

    assert store.exists("future")
    assert cwd_exists(project.cwd) is False


def test_get_returns_persisted_project(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo, default_model="openai/gpt-5")

    loaded = store.get("vbot")

    assert loaded.display_name == "vBot"
    assert loaded.default_model == "openai/gpt-5"


def test_create_persists_default_temperature_and_thinking(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create(
        "vbot",
        "vBot",
        repo,
        default_temperature=0.4,
        default_thinking_effort="high",
    )

    loaded = store.get("vbot")
    assert loaded.default_temperature == 0.4
    assert loaded.default_thinking_effort == "high"


def test_update_sets_default_temperature_and_thinking(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    updated = store.update("vbot", default_temperature=0.2, default_thinking_effort="low")

    assert updated.default_temperature == 0.2
    assert updated.default_thinking_effort == "low"
    reloaded = store.get("vbot")
    assert reloaded.default_temperature == 0.2
    assert reloaded.default_thinking_effort == "low"


def test_update_one_default_leaves_the_other_untouched(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo, default_temperature=0.5, default_thinking_effort="high")

    updated = store.update("vbot", default_temperature=0.1)

    assert updated.default_temperature == 0.1
    # The thinking effort was not in the change set, so it must survive unchanged.
    assert updated.default_thinking_effort == "high"


def test_update_clears_default_thinking_with_none(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo, default_thinking_effort="high")

    updated = store.update("vbot", default_thinking_effort=None)

    assert updated.default_thinking_effort is None


def test_get_raises_for_unknown_project(data_dir: Path) -> None:
    store = ProjectStore(data_dir)

    with pytest.raises(ProjectNotFoundError):
        store.get("missing")


def test_list_returns_projects_sorted_by_id(data_dir: Path, tmp_path: Path) -> None:
    store = ProjectStore(data_dir)
    for name in ["zeta", "alpha", "mid"]:
        repo_dir = tmp_path / "repos" / name
        repo_dir.mkdir(parents=True)
        store.create(name, name.title(), repo_dir)

    ids = [project.project_id for project in store.list()]
    assert ids == ["alpha", "mid", "zeta"]


def test_list_skips_corrupt_config(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    bad_dir = data_dir / "projects" / "broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "project.json").write_text("{ not json", encoding="utf-8")

    ids = [project.project_id for project in store.list()]
    assert ids == ["vbot"]


def test_update_changes_display_name(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    updated = store.update("vbot", display_name="vBot Renamed")

    assert updated.display_name == "vBot Renamed"
    assert store.get("vbot").display_name == "vBot Renamed"


def test_rename_keeps_key_and_sessions_path_stable(data_dir: Path, repo: Path) -> None:
    # Renaming changes only the display name; the project_id key — and therefore
    # the project-scoped sessions path — stays put.
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    before = store.sessions_dir("vbot", "orchestrator")

    store.update("vbot", display_name="vBot Renamed")

    assert store.get("vbot").project_id == "vbot"
    assert store.sessions_dir("vbot", "orchestrator") == before


def test_update_rejects_unknown_field(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    with pytest.raises(ProjectError):
        store.update("vbot", team=["builder"])


def test_update_cwd_renormalizes_and_keeps_key(data_dir: Path, repo: Path, tmp_path: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    new_repo = tmp_path / "repos" / "moved"
    new_repo.mkdir(parents=True)

    updated = store.update("vbot", cwd=str(new_repo))

    assert updated.cwd == str(Path(os.path.realpath(new_repo)))
    assert store.exists("vbot")


def test_update_cwd_rejects_collision_with_other_project(
    data_dir: Path, repo: Path, tmp_path: Path
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    other_repo = tmp_path / "repos" / "other"
    other_repo.mkdir(parents=True)
    store.create("other", "Other", other_repo)

    with pytest.raises(ProjectAlreadyExistsError):
        store.update("other", cwd=str(repo))


def test_delete_archives_anchor_and_removes_active(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    archive_path = store.delete("vbot")

    assert not (data_dir / "projects" / "vbot").exists()
    assert (archive_path / "project.json").is_file()
    assert archive_path == data_dir / "archive" / "projects" / "vbot"


def test_delete_does_not_touch_repo(data_dir: Path, repo: Path) -> None:
    marker = repo / "keep.txt"
    marker.write_text("repo content", encoding="utf-8")
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    store.delete("vbot")

    assert marker.read_text(encoding="utf-8") == "repo content"


def test_delete_replaces_existing_archive(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    store.delete("vbot")
    store.create("vbot", "vBot Again", repo)

    archive_path = store.delete("vbot")

    payload = json.loads((archive_path / "project.json").read_text("utf-8"))
    assert payload["display_name"] == "vBot Again"


def test_delete_raises_for_unknown_project(data_dir: Path) -> None:
    store = ProjectStore(data_dir)

    with pytest.raises(ProjectNotFoundError):
        store.delete("missing")


def test_sessions_dir_is_project_scoped(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    sessions_dir = store.sessions_dir("vbot", "orchestrator")

    assert sessions_dir == data_dir / "projects" / "vbot" / "agents" / "orchestrator" / "sessions"


def test_workspace_dir_is_under_agent_anchor(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    workspace_dir = store.workspace_dir("vbot", "rooted")

    assert workspace_dir == data_dir / "projects" / "vbot" / "agents" / "rooted" / "workspace"


def _write_anchor_session(data_dir: Path, project_id: str, agent_id: str) -> None:
    """Create one session file under a project anchor via the session backbone."""
    manager = ChatSessionManager(data_dir)
    manager.create(agent_id, project_id=project_id)


def test_session_owning_agents_lists_only_agents_with_sessions(data_dir: Path, repo: Path) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)
    _write_anchor_session(data_dir, "vbot", "builder")
    _write_anchor_session(data_dir, "vbot", "orchestrator")
    # An agent dir created without any session must not count as an owner.
    (data_dir / "projects" / "vbot" / "agents" / "empty" / "sessions").mkdir(parents=True)

    owners = store.session_owning_agents("vbot")

    assert owners == ["builder", "orchestrator"]


def test_session_owning_agents_empty_for_project_without_sessions(
    data_dir: Path, repo: Path
) -> None:
    store = ProjectStore(data_dir)
    store.create("vbot", "vBot", repo)

    assert store.session_owning_agents("vbot") == []


def test_session_owning_agents_empty_for_unknown_project(data_dir: Path) -> None:
    store = ProjectStore(data_dir)

    assert store.session_owning_agents("missing") == []
