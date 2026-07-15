"""Prompt Project and Skill scope resolution tests."""

from .resolver_test_support import (
    Path,
    ProjectStore,
    _stub_project,
    resolve_prompt_project,
    resolve_skill_scope,
)
from .resolver_test_support import agents as agents
from .resolver_test_support import data_dir as data_dir
from .resolver_test_support import projects as projects
from .resolver_test_support import repo as repo
from .resolver_test_support import template_dir as template_dir


def test_resolve_prompt_project_uses_explicit_project(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)
    # A config agent has an empty workspace; the explicit project_id is what counts.
    resolved = resolve_prompt_project(store, "vbot")

    assert resolved is not None
    assert resolved.project_id == "vbot"


def test_resolve_prompt_project_does_not_infer_from_workspace(tmp_path: Path) -> None:
    # Workspace equality no longer selects a Project.
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)
    resolved = resolve_prompt_project(store, None)

    assert resolved is None


def test_resolve_prompt_project_none_when_workspace_not_a_repo(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)
    home = tmp_path / "workspace-coder"
    home.mkdir()

    assert resolve_prompt_project(store, None) is None


def test_resolve_prompt_project_none_without_explicit_scope(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create("vbot", "vBot", repo)

    assert resolve_prompt_project(store, None) is None


def test_resolve_skill_scope_project_run_drops_private_layer() -> None:
    # A project run scopes to its own project and never carries an identity layer:
    # a team slug colliding with an identity agent's id must not pull that agent's
    # private skills past the project skill whitelist.
    scope = resolve_skill_scope("vbot", _stub_project("vbot"), "builder")

    assert scope == ("vbot", None)


def test_resolve_skill_scope_rooted_identity_uses_home_project() -> None:
    # A rooted identity run (project_id None, prompt project resolved by rooting)
    # sees its home project's skills plus its own private layer.
    scope = resolve_skill_scope(None, _stub_project("vbot"), "main")

    assert scope == ("vbot", "main")


def test_resolve_skill_scope_plain_identity_stays_global() -> None:
    scope = resolve_skill_scope(None, None, "main")

    assert scope == (None, "main")
