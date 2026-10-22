"""push.py pushes exactly the commit it checked, and only when every check passed."""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
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
    monkeypatch.setattr(push._worktrees.context, "project_root", repo)
    monkeypatch.setattr(push._worktrees.context, "worktrees_dir", repo / ".worktrees")
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
    assert not list((repo / ".worktrees").glob(f"{push.PUSH_DIR_PREFIX}*"))
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


@pytest.mark.parametrize(
    ("wsl", "code", "output", "passed", "shown"),
    [
        (False, 0, "", False, "WSL is not installed"),
        (True, 0, "1 passed\nflaky: tests/test_b.py::test_b\n", False, "tests/test_b.py::test_b"),
        (True, 1, f"{FAILURE}\n1 failed\n", False, FAILURE),
        (True, 2, "linux tests: uv 1.0 could not be installed\n", False, "uv 1.0 could not"),
    ],
    ids=["no wsl", "flaky", "failure", "setup failure"],
)
def test_linux_step_runs_the_checked_commit_in_wsl_and_never_skips(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    wsl: bool,
    code: int,
    output: str,
    passed: bool,
    shown: str,
) -> None:
    received: list[str] = []
    run = subprocess.run

    def fake_run(command: list[str], **options: object) -> subprocess.CompletedProcess[object]:
        if command[0] == "git":
            return run(command, **options)  # type: ignore[call-overload,no-any-return]
        if command in (
            ["wsl.exe", "--exec", "true"],
            ["wsl.exe", "--exec", "bash", "-c", push.WSL_USER_READY],
        ):
            return subprocess.CompletedProcess(command, 0, "")
        # The budget covers the unpack, setup and tests, including child processes
        # and file cache, without limiting other WSL workloads.
        assert command[:5] == ["wsl.exe", "--exec", "systemd-run", "--user", "--scope"]
        assert "--property=MemoryHigh=2560M" in command
        assert "--property=MemoryMax=3G" in command
        assert "--property=MemorySwapMax=0" in command
        assert "--property=OOMPolicy=kill" in command
        assert command[-3:] == ["bash", "-c", push.WSL_BOOTSTRAP]
        stdin = options["stdin"]
        with tarfile.open(fileobj=stdin, mode="r|") as archive:  # type: ignore[call-overload]
            received.extend(member.name for member in archive)
        return subprocess.CompletedProcess(command, code, output.encode())

    monkeypatch.setattr(push.shutil, "which", lambda name: "wsl.exe" if wsl else None)
    monkeypatch.setattr(push.subprocess, "run", fake_run)

    step = push._linux_pytest_step(repo, io.StringIO())

    assert step.passed is passed
    assert shown in step.note + step.details
    assert ("feature.txt" in received) is wsl


@pytest.mark.parametrize(("initial", "retry"), [(0, None), (1, 0), (1, 1), (1, 2), (2, None)])
def test_backend_gate_keeps_the_original_failure_after_a_diagnostic_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: int, retry: int | None
) -> None:
    commands: list[list[str]] = []
    outcomes = iter([initial] if retry is None else [initial, retry])
    failed = iter(
        [{"tests/test_a.py::test_a"}, set() if retry == 0 else {"tests/test_a.py::test_a"}]
    )

    def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        code = next(outcomes)
        return subprocess.CompletedProcess(command, code, FAILURE if code else "1 passed")

    monkeypatch.setattr(push, "_run", run)
    monkeypatch.setattr(push, "_last_failed", lambda _root: next(failed))
    log = io.StringIO()

    step = push._pytest_step(tmp_path, log, "locked-python")

    assert step.passed is (initial == 0)
    assert len(commands) == (1 if retry is None else 2)
    assert all(command[:3] == ["locked-python", "-m", "pytest"] for command in commands)
    if retry is not None:
        assert commands[1][3:8] == ["--last-failed", "--last-failed-no-failures", "none", "-n", "0"]
        assert FAILURE in step.details
    if retry == 0:
        assert "tests/test_a.py::test_a" in step.note
