"""Tests for the project anchor lifecycle (ProjectStore)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.projects.paths import cwd_exists
from core.projects.projects import (
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
)
from core.projects.store import ProjectStore


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


def test_get_raises_for_unknown_project(data_dir: Path) -> None:
    store = ProjectStore(data_dir)

    with pytest.raises(ProjectNotFoundError):
        store.get("missing")


def test_list_returns_projects_sorted_by_id(data_dir: Path, tmp_path: Path) -> None:
    store = ProjectStore(data_dir)
    for index, name in enumerate(["zeta", "alpha", "mid"]):
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
