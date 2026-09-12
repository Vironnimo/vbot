"""worktree helpers coverage."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


MODULE_PATH = PROJECT_ROOT / "scripts" / "worktree.py"


def _load_worktree_module():
    spec = importlib.util.spec_from_file_location("worktree", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": f"~/.vbot-{name}", "managed_branch": True}),
        encoding="utf-8",
    )
    return worktree_path


def _patch_repo_globals(monkeypatch, module, repo: Path) -> None:
    monkeypatch.setattr(module, "PROJECT_ROOT", repo)
    monkeypatch.setattr(module, "WORKTREES_DIR", repo / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: repo.parent / "home"))


@pytest.fixture
def real_repo(tmp_path):
    return _init_repo(tmp_path)


def _list_porcelain(repo: Path) -> list[str]:
    output = _git_output(repo, "status", "--porcelain")
    return [line for line in output.splitlines() if line.strip()]
