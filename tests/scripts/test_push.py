"""push.py pushes exactly the commit it checked, and only when every check passed."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import pytest

from scripts import push


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, path: str, message: str) -> str:
    (repo / path).write_text(f"{message}\n", encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _clone(origin: Path, target: Path) -> Path:
    _git(origin.parent, "clone", "-q", str(origin), str(target))
    _git(target, "config", "user.email", "test@example.invalid")
    _git(target, "config", "user.name", "Test")
    return target


@pytest.fixture(scope="session")
def _template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bare origin and a primary checkout one commit ahead of it, built once per worker."""
    root = tmp_path_factory.mktemp("push-template")
    _git(root, "init", "-q", "--bare", "--initial-branch=main", "origin.git")
    repo = root / "repo"
    _git(root, "init", "-q", "--initial-branch=main", "repo")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    # Relative, so that a copy of both pushes to its own origin.
    _git(repo, "remote", "add", "origin", "../origin.git")
    _commit(repo, ".gitignore", ".worktrees/")
    _git(repo, "push", "-q", "origin", "main")
    _commit(repo, "feature.txt", "unpushed feature")
    return root


@pytest.fixture
def origin(_template: Path, tmp_path: Path) -> Path:
    shutil.copytree(_template, tmp_path, dirs_exist_ok=True)
    return tmp_path / "origin.git"


@pytest.fixture
def repo(origin: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = origin.parent / "repo"
    monkeypatch.setattr(push.worktree, "PROJECT_ROOT", repo)
    monkeypatch.setattr(push.worktree, "WORKTREES_DIR", repo / ".worktrees")
    return repo


FAILURE = "FAILED tests/test_a.py::test_a - assert False"


def _fake_checks(
    monkeypatch: pytest.MonkeyPatch, passed: bool, during: Callable[[], None] | None = None
) -> list[str]:
    """Replace the checks with one step that *passed* or not; return the commits checked."""
    checked: list[str] = []

    def run_checks(checkout: Path, log: TextIO) -> list[push.Step]:
        checked.append(_git(checkout, "rev-parse", "HEAD"))
        # Uncommitted work in the primary checkout stays out of the check.
        assert not (checkout / "uncommitted.txt").exists()
        if during is not None:
            during()
        return [push.Step("pytest", passed, 1.0, "" if passed else FAILURE)]

    monkeypatch.setattr(push, "run_checks", run_checks)
    return checked


@pytest.mark.parametrize(
    ("passed", "arguments", "pushes"),
    [(True, [], True), (False, [], False), (True, ["--no-push"], False)],
    ids=["all pass", "a check fails", "no push"],
)
def test_push_sends_the_checked_commit_only_when_every_check_passes(
    origin: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passed: bool,
    arguments: list[str],
    pushes: bool,
) -> None:
    main = _git(repo, "rev-parse", "main")
    before = _git(origin, "rev-parse", "main")
    (repo / "uncommitted.txt").write_text("work in progress\n", encoding="utf-8")
    checked = _fake_checks(monkeypatch, passed)

    result = push.main(arguments)
    output = capsys.readouterr().out

    assert checked == [main]
    assert result == (0 if passed else 1)
    assert _git(origin, "rev-parse", "main") == (main if pushes else before)
    # The private checkout is gone; its log stays.
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not list((repo / ".worktrees").glob(f"{push.worktree.PUSH_DIR_PREFIX}*"))
    log = Path(output.split("Complete output: ")[1].splitlines()[0])
    assert log.is_file()
    if not passed:
        assert FAILURE in output
        assert "Not pushed" in output


@pytest.mark.parametrize("when", ["before the checks", "during the checks"])
def test_push_never_overwrites_a_moved_origin(
    origin: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: str,
) -> None:
    other = _clone(origin, origin.parent / "other")

    def move_origin() -> None:
        _commit(other, "other.txt", "pushed elsewhere")
        _git(other, "push", "-q", "origin", "main")

    if when == "before the checks":
        move_origin()
    checked = _fake_checks(monkeypatch, True, move_origin if when == "during the checks" else None)

    result = push.main([])

    assert result == 1
    assert _git(origin, "rev-parse", "main") == _git(other, "rev-parse", "HEAD")
    assert len(checked) == (0 if when == "before the checks" else 1)
    assert "origin's main moved" in capsys.readouterr().out
