"""worktree helpers coverage."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import _worktree_context as worktree_context
from scripts import _worktree_records as worktree_records
from scripts import worktree as worktree_entry

PROJECT_ROOT = Path(__file__).resolve().parents[2]


MODULE_PATH = PROJECT_ROOT / "scripts" / "worktree.py"


def _worktree_manager():
    return worktree_entry.WorktreeManager(
        worktree_context.WorktreeContext.from_checkout(PROJECT_ROOT)
    )


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    if _git_output(repo, "rev-parse", "--abbrev-ref", "HEAD") != "main":
        _git(repo, "branch", "-m", "main")
    return repo


def _commit_file(repo: Path, relative_path: str, content: str, message: str) -> None:
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", relative_path)
    _git(repo, "commit", "-m", message)


def _create_task_worktree(module, repo: Path, name: str) -> Path:
    worktrees_dir = repo / ".worktrees"
    _git(repo, "worktree", "add", "-b", name, str(worktrees_dir / name))
    worktree_path = worktrees_dir / name
    (worktree_path / worktree_records.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(module.context.data_root / name), "managed_branch": True}),
        encoding="utf-8",
    )
    return worktree_path


def _patch_repo_globals(monkeypatch, module, repo: Path) -> None:
    monkeypatch.setattr(module.context, "project_root", repo)
    monkeypatch.setattr(module.context, "worktrees_dir", repo / ".worktrees")
    monkeypatch.setattr(module.context, "data_root", repo.parent / "data")
    monkeypatch.setattr(worktree_context.Path, "home", staticmethod(lambda: repo.parent / "home"))
    monkeypatch.setenv("USERPROFILE", str(repo.parent / "home"))
    monkeypatch.setenv("HOME", str(repo.parent / "home"))


def _record_owned_data(module, worktree_path: Path, data_dir: Path) -> None:
    """Give a temporary test checkout matching data ownership records."""
    marker_path = worktree_path / worktree_records.WORKTREE_FILE_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else {}
    marker.update({"data_dir": str(data_dir), "data_owner": "test-owner"})
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    (data_dir / worktree_records.DATA_OWNER_FILE_NAME).write_text(
        json.dumps(
            {
                "format_version": 1,
                "data_owner": "test-owner",
                "repository": os.path.normcase(str(module.context.project_root.resolve())),
                "worktree": os.path.normcase(str(worktree_path.resolve())),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def real_repo(tmp_path, tmp_path_factory):
    # Each test gets a copy of one initialized repository per worker; a plain
    # repository without linked worktrees holds no absolute paths.
    template = tmp_path_factory.getbasetemp() / "worktree-repository-template"
    if not template.exists():
        partial = tmp_path_factory.mktemp("worktree-repository")
        _init_repo(partial)
        partial.rename(template)
    repo = tmp_path / "repo"
    shutil.copytree(template / "repo", repo)
    return repo


def _list_porcelain(repo: Path) -> list[str]:
    output = _git_output(repo, "status", "--porcelain")
    return [line for line in output.splitlines() if line.strip()]
