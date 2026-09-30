"""worktree merge coverage."""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts import _worktree_lock as worktree_lock
from tests.scripts.worktree_helpers import (
    _commit_file,
    _create_task_worktree,
    _git,
    _git_output,
    _list_porcelain,
    _load_worktree_module,
    _patch_repo_globals,
    _record_owned_data,
    real_repo,
)

__all__ = ["real_repo"]


def _landings(repo):
    """List the landing checkouts under the repository's worktrees directory."""
    worktrees = repo / ".worktrees"
    if not worktrees.exists():
        return []
    return sorted(path.name for path in worktrees.iterdir() if path.name.startswith(".landing-"))


def _wait_until(condition, timeout=10.0):
    """Poll *condition* until it holds; return whether it did before the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _around_the_merge_check(monkeypatch, module, action):
    """Run *action* with the landing checkout each time a merge commit is checked.

    Return the landing checkouts of the checks so far.
    """
    commit_landing = module._commit_landing
    checks = []

    def check(landing, message):
        checks.append(landing)
        action(landing)
        return commit_landing(landing, message)

    monkeypatch.setattr(module, "_commit_landing", check)
    return checks


def test_parse_args_merge_and_repair_defaults():
    module = _load_worktree_module()

    merge_args = module.parse_args(["merge", "task"])
    assert merge_args.command == "merge"
    assert merge_args.name == "task"
    assert merge_args.message is None
    assert merge_args.wait_timeout == 1800
    assert worktree_lock.DEFAULT_MERGE_WAIT_TIMEOUT_SECONDS == 1800

    repair_args = module.parse_args(["repair-start", "task"])
    assert repair_args.command == "repair-start"
    assert repair_args.window == 900
    assert worktree_lock.DEFAULT_REPAIR_WINDOW_SECONDS == 900

    finish_args = module.parse_args(["repair-finish", "task"])
    assert finish_args.command == "repair-finish"

    keeper_args = module.parse_args(
        [
            "keeper-hold",
            "--task",
            "t",
            "--deadline",
            "1.0",
            "--lock-path",
            "l",
            "--holder-path",
            "h",
            "--release-path",
            "r",
        ]
    )
    assert keeper_args.command == "keeper-hold"


@pytest.mark.parametrize("name", ["../escape", "dev", "DEV", "dev."])
def test_cmd_merge_rejects_unsafe_name(tmp_path, monkeypatch, name):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    assert module.cmd_merge(argparse.Namespace(name=name, message=None, wait_timeout=1)) == 1
    assert commands == []


def test_cmd_merge_requires_primary_checkout_on_main(tmp_path, monkeypatch):
    module = _load_worktree_module()
    worktrees_dir = tmp_path / ".worktrees"
    worktree_path = worktrees_dir / "task-a"
    worktree_path.mkdir(parents=True)
    (worktree_path / module.WORKTREE_FILE_NAME).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(module, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda path: path.name)

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=5))

    assert result == 1
    assert all(command[:2] != ["git", "merge"] for command in commands)


def test_cmd_merge_refuses_dirty_primary_checkout(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    _create_task_worktree(module, real_repo, "task-a")
    (real_repo / "stray.txt").write_text("untracked\n", encoding="utf-8")

    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=10))

    assert result == 1
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert (real_repo / ".worktrees" / "task-a").exists()


@pytest.mark.parametrize("owned", [False, True])
def test_cmd_merge_merges_removes_worktree_and_branch(real_repo, monkeypatch, capsys, owned):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "feature-a.txt", "a\n", "add feature a")
    data_dir = real_repo.parent / "home" / ".vbot-task-a"
    data_dir.mkdir(parents=True)
    (data_dir / "sentinel.txt").write_text("keep if unowned", encoding="utf-8")
    if owned:
        _record_owned_data(module, worktree_a, data_dir)

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))

    assert result == 0
    assert (real_repo / "feature-a.txt").read_text(encoding="utf-8") == "a\n"
    assert not worktree_a.exists()
    assert (
        subprocess.run(
            ["git", "-C", str(real_repo), "rev-parse", "--verify", "refs/heads/task-a"],
            capture_output=True,
        ).returncode
        != 0
    )
    assert "merge: task-a" in _git_output(real_repo, "log", "--format=%s", "-1")
    assert _landings(real_repo) == []
    assert ".landing-" not in _git_output(real_repo, "worktree", "list", "--porcelain")
    if owned:
        assert not data_dir.exists()
    else:
        assert (data_dir / "sentinel.txt").read_text(encoding="utf-8") == "keep if unowned"
        assert "data-status: preserved (ownership unverified)" in capsys.readouterr().out


def test_cmd_merge_reports_conflict_hints_and_keeps_main_intact(capfd, real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "one\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "shared.txt", "from-a\n", "a edit")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "shared.txt", "from-b\n", "b edit")
    # The conflict is reported before the branch's tests would run and fail.
    _commit_file(worktree_b, "scripts/commit_check.py", FAILING_BRANCH_CHECK, "branch check")

    assert module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60)) == 0

    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    capfd.readouterr()
    result = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))
    captured = capfd.readouterr()

    assert result == module.MERGE_CONFLICT_EXIT_CODE
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert _list_porcelain(real_repo) == []
    assert (real_repo / "shared.txt").read_text(encoding="utf-8") == "from-a\n"
    assert worktree_b.exists()
    assert "conflicted: shared.txt" in captured.out
    assert "FAIL: tests" not in captured.out
    assert "python scripts/worktree.py repair-start task-b" in captured.out
    assert "python scripts/worktree.py merge task-b" in captured.out


def _reject_commits(repo):
    """Install a commit check that rejects every commit, as both hooks .githooks has."""
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "pre-merge-commit"):
        hook = hooks / name
        hook.write_bytes(b"#!/bin/sh\necho 'FAIL: tests' >&2\nexit 1\n")
        hook.chmod(0o755)  # git skips a hook that is not executable on POSIX
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)


def test_cmd_merge_rejected_by_the_merge_check_keeps_main_intact(capsys, real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "feature.txt", "new\n", "a edit")
    _reject_commits(real_repo)
    main_head = _git_output(real_repo, "rev-parse", "HEAD")

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))
    captured = capsys.readouterr()

    assert result == module.MERGE_CONFLICT_EXIT_CODE
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert not (real_repo / "feature.txt").exists()
    assert worktree.exists()
    assert "FAIL: tests" in captured.out + captured.err
    assert "conflicted:" not in captured.out
    assert "repair-start" not in captured.out
    assert "python scripts/worktree.py merge task-a" in captured.out


LOCKED_BEFORE = '{"packages": {"node_modules/vite": {"version": "8.0.1"}}}\n'
LOCKED_AFTER = '{"packages": {"node_modules/vite": {"version": "8.0.3"}}}\n'


def _branch_with_webui_change(module, repo, path):
    """Give main installed WebUI packages and a task branch that changes *path*."""
    _commit_file(repo, ".gitignore", ".worktrees/\nnode_modules/\n", "ignore packages")
    _commit_file(repo, "webui/package-lock.json", LOCKED_BEFORE, "lock packages")
    (repo / "webui" / "node_modules").mkdir()
    # npm's record of what it installed: main's packages match main's lock.
    (repo / "webui" / "node_modules" / ".package-lock.json").write_text(
        LOCKED_BEFORE, encoding="utf-8"
    )
    worktree = _create_task_worktree(module, repo, "task-a")
    _commit_file(worktree, path, LOCKED_AFTER, "change the webui")
    return worktree


def _record_npm(monkeypatch, module, repo, *, failing_call=None):
    """Record each npm command with its checkout, main's HEAD and that checkout's WebUI lock.

    The npm command numbered *failing_call* (from 1) fails, as with a locked file.
    """
    run_command = module._run_command
    installs = []

    def run(command, *, cwd=None):
        if Path(command[0]).stem.lower() != "npm":
            return run_command(command, cwd=cwd)
        checkout = Path(cwd).parent
        where = "main" if checkout == repo else checkout.name.split("-task-a-")[0]
        lock = (checkout / "webui" / "package-lock.json").read_text(encoding="utf-8")
        installs.append((command[1:], where, _git_output(repo, "rev-parse", "HEAD"), lock))
        if len(installs) == failing_call:
            return 1, "npm error EBUSY: resource busy or locked"
        return 0, ""

    monkeypatch.setattr(module, "_run_command", run)
    return installs


@pytest.mark.parametrize(
    ("path", "failing_call"),
    [
        ("webui/package-lock.json", None),
        ("webui/src/app.js", None),
        ("webui/package-lock.json", 2),
    ],
    ids=["lock changed", "sources changed", "main's npm ci fails"],
)
def test_cmd_merge_checks_the_merged_webui_on_the_merged_packages(
    capsys, real_repo, monkeypatch, path, failing_call
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    worktree = _branch_with_webui_change(module, real_repo, path)
    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    installs = _record_npm(monkeypatch, module, real_repo, failing_call=failing_call)
    installed = []
    _around_the_merge_check(
        monkeypatch,
        module,
        lambda landing: installed.append(
            (landing / "webui" / "node_modules" / ".package-lock.json").is_file()
        ),
    )

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))
    captured = capsys.readouterr()

    merged_head = _git_output(real_repo, "rev-parse", "HEAD")
    assert _git_output(real_repo, "rev-parse", "HEAD^1") == main_head
    assert not worktree.exists()
    assert "status: merged" in captured.out
    if path == "webui/src/app.js":
        # main's installed packages match the merged lock: the check runs on a copy.
        assert result == 0
        assert installed == [True]
        assert installs == []
        return
    # The check runs on the merged packages; main installs them once it has the merge.
    assert installs == [
        (["ci"], ".landing", main_head, LOCKED_AFTER),
        (["ci"], "main", merged_head, LOCKED_AFTER),
    ]
    if failing_call is None:
        assert result == 0
    else:
        assert result == 1
        assert f"run `npm ci` in {real_repo / 'webui'}" in captured.out


@pytest.mark.parametrize("failure", ["install", "check"])
def test_cmd_merge_leaves_main_webui_packages_alone_when_the_merge_does_not_land(
    capsys, real_repo, monkeypatch, failure
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    worktree = _branch_with_webui_change(module, real_repo, "webui/package-lock.json")
    if failure == "check":
        _reject_commits(real_repo)
    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    installs = _record_npm(
        monkeypatch, module, real_repo, failing_call=1 if failure == "install" else None
    )

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))
    captured = capsys.readouterr()

    assert result == (1 if failure == "install" else module.MERGE_CONFLICT_EXIT_CODE)
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert _list_porcelain(real_repo) == []
    assert worktree.exists()
    # Only the landing checkout installed packages; main's stay as they are.
    assert installs == [(["ci"], ".landing", main_head, LOCKED_AFTER)]
    assert "python scripts/worktree.py merge task-a" in captured.out


def _observe_main_during_the_check(repo):
    """Install a commit hook that records where it runs and main's state meanwhile."""
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    observed = repo.parent / "observed.txt"
    main = repo.as_posix()
    script = (
        "#!/bin/sh\n"
        "{\n"
        '  echo "cwd=$(pwd)"\n'
        '  echo "landing=$VBOT_COMMIT_CHECK_LANDING"\n'
        "  (\n"
        "    unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX GIT_COMMON_DIR\n"
        f"    git --no-optional-locks -C '{main}' status --porcelain\n"
        f"    test -f '{main}/.git/MERGE_HEAD' && echo 'main is mid-merge'\n"
        "  )\n"
        f"}} >> '{observed.as_posix()}'\n"
        "exit 0\n"
    )
    for name in ("pre-commit", "pre-merge-commit"):
        hook = hooks / name
        hook.write_bytes(script.encode("utf-8"))
        hook.chmod(0o755)  # git skips a hook that is not executable on POSIX
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    return observed


def test_cmd_merge_checks_the_merge_commit_while_main_stays_as_it_was(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "feature.txt", "a\n", "a file")
    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    branch_head = _git_output(worktree, "rev-parse", "HEAD")
    observed = _observe_main_during_the_check(real_repo)

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))

    assert result == 0
    # A merge killed during its check therefore leaves main as it was.
    where, *during = observed.read_text(encoding="utf-8").splitlines()
    assert "/.worktrees/.landing-task-a-" in where
    assert during == ["landing=1"]
    assert _git_output(real_repo, "rev-parse", "HEAD^1") == main_head
    assert _git_output(real_repo, "rev-parse", "HEAD^2") == branch_head
    assert _list_porcelain(real_repo) == []
    assert _landings(real_repo) == []


def test_cmd_merge_merges_again_onto_a_main_that_moved_during_the_check(
    capsys, real_repo, monkeypatch
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "feature.txt", "a\n", "a file")

    def commit_on_main_during_the_first_check(_landing):
        if len(checks) == 1:
            _commit_file(real_repo, "direct.txt", "direct\n", "direct commit")

    checks = _around_the_merge_check(monkeypatch, module, commit_on_main_during_the_first_check)

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))

    assert result == 0
    assert len(checks) == 2
    assert "main moved while the merge commit was checked" in capsys.readouterr().out
    assert _git_output(real_repo, "log", "-1", "--format=%s", "HEAD^1") == "direct commit"
    assert (real_repo / "feature.txt").is_file()
    assert (real_repo / "direct.txt").is_file()
    assert _list_porcelain(real_repo) == []


def test_cmd_merge_checks_with_main_test_records_and_hands_its_own_back(real_repo, monkeypatch):
    from scripts import _test_impact

    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, ".gitignore", ".worktrees/\n.testfiledeps\n", "ignore records")
    _test_impact.record_tested_state(real_repo, "main-tree", ["dirty.txt"])
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "feature.txt", "a\n", "a file")
    seen = []

    def check_records(landing):
        seen.append(_test_impact.tested_state(landing))
        # The merge commit's check records the state it tested.
        _test_impact.record_tested_state(landing, "merged-tree", [])

    _around_the_merge_check(monkeypatch, module, check_records)

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))

    assert result == 0
    assert seen == [("main-tree", frozenset({"dirty.txt"}))]
    assert _test_impact.tested_state(real_repo) == ("merged-tree", frozenset())


def test_merge_removes_landing_checkouts_of_merges_that_ended(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "feature.txt", "a\n", "a file")
    worktrees = real_repo / ".worktrees"
    ended = worktrees / ".landing-task-x-ended"
    running = worktrees / ".landing-task-y-running"
    for landing in (ended, running):
        _git(real_repo, "worktree", "add", "--detach", str(landing), "main")

    # A running merge holds its landing checkout's lock; the OS frees it when it dies.
    with (worktrees / ".landing-task-y-running.lock").open("a+b") as running_lock:
        assert worktree_lock._acquire_file_lock(running_lock)
        result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))
        worktree_lock._release_file_lock(running_lock)

    assert result == 0
    assert not ended.exists()
    assert running.exists()
    registered = _git_output(real_repo, "worktree", "list", "--porcelain")
    assert "landing-task-x-ended" not in registered
    assert "landing-task-y-running" in registered


FAILING_BRANCH_CHECK = (
    "import sys\n"
    "\n"
    'print("FAIL: tests" if sys.argv[1:] == ["--branch"] else "unexpected arguments")\n'
    "sys.exit(1)\n"
)


def test_cmd_merge_rejects_a_failing_branch_check_without_waiting_for_the_lock(
    capfd, real_repo, monkeypatch
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "scripts/commit_check.py", FAILING_BRANCH_CHECK, "branch check")
    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    lock_path = module._merge_lock_paths()[0]

    # Another merge holds the lock; the branch's tests run before the merge waits for it.
    with lock_path.open("a+b") as handle:
        assert worktree_lock._acquire_file_lock(handle)
        result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=0.3))
        worktree_lock._release_file_lock(handle)
    captured = capfd.readouterr()

    assert result == module.MERGE_CONFLICT_EXIT_CODE
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert worktree.exists()
    assert "FAIL: tests" in captured.out
    assert "merge lock stayed busy" not in captured.out
    assert "python scripts/worktree.py merge task-a" in captured.out


def test_cmd_merge_rolls_back_an_unfinished_merge_left_in_main(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "one\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "other-a.txt", "a\n", "a file")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "other-b.txt", "b\n", "b file")

    # A killed merge of an older version of this script leaves its merge staged in main.
    subprocess.run(
        ["git", "-C", str(real_repo), "merge", "task-b", "--no-commit", "--no-ff"],
        check=False,
        capture_output=True,
    )
    assert (real_repo / ".git" / "MERGE_HEAD").exists()

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))

    assert result == 0
    assert (real_repo / "other-a.txt").read_text(encoding="utf-8") == "a\n"
    # The aborted merge's staged content is rolled back together with it.
    assert not (real_repo / "other-b.txt").exists()
    assert not (real_repo / ".git" / "MERGE_HEAD").exists()
    assert _list_porcelain(real_repo) == []

    # The interrupted task merges normally afterwards.
    assert module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60)) == 0
    assert (real_repo / "other-b.txt").read_text(encoding="utf-8") == "b\n"


@pytest.mark.parametrize("edited", ["other files", "a merged file"])
def test_cmd_merge_keeps_work_done_since_an_unfinished_merge(
    capsys, real_repo, monkeypatch, edited
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "one\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "other-a.txt", "a\n", "a file")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "other-b.txt", "b\n", "b file")
    subprocess.run(
        ["git", "-C", str(real_repo), "merge", "task-b", "--no-commit", "--no-ff"],
        check=False,
        capture_output=True,
    )
    # Other sessions work in main meanwhile.
    if edited == "other files":
        (real_repo / "session.txt").write_text("staged\n", encoding="utf-8")
        _git(real_repo, "add", "session.txt")
        (real_repo / "shared.txt").write_text("unstaged\n", encoding="utf-8")
    else:
        (real_repo / "other-b.txt").write_text("edited\n", encoding="utf-8")
    main_head = _git_output(real_repo, "rev-parse", "HEAD")

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))
    output = capsys.readouterr().out

    assert result == 1
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert worktree_a.exists()
    if edited == "other files":
        # The merge is rolled back; the session's work stays and blocks this merge.
        assert "recovered: rolled back an unfinished merge" in output
        assert not (real_repo / ".git" / "MERGE_HEAD").exists()
        assert not (real_repo / "other-b.txt").exists()
        assert set(_list_porcelain(real_repo)) == {"A  session.txt", " M shared.txt"}
        assert (real_repo / "shared.txt").read_text(encoding="utf-8") == "unstaged\n"
        assert "uncommitted: A  session.txt" in output
    else:
        # A file the merge changed was changed again: the merge stays as it is.
        assert "changed-since-merge: other-b.txt" in output
        assert (real_repo / ".git" / "MERGE_HEAD").exists()
        assert (real_repo / "other-b.txt").read_text(encoding="utf-8") == "edited\n"


def _poll_the_merge_lock_quickly(monkeypatch):
    # A waiting merger retries every 0.4-1.2 s; these tests wait for less than that.
    monkeypatch.setattr(worktree_lock, "MERGE_LOCK_POLL_MIN_SECONDS", 0.01)
    monkeypatch.setattr(worktree_lock, "MERGE_LOCK_POLL_MAX_SECONDS", 0.02)


def test_merge_lock_blocks_second_merger_until_release(real_repo, monkeypatch, capsys):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _poll_the_merge_lock_quickly(monkeypatch)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "feature-b.txt", "b\n", "b file")
    lock_path = module._merge_lock_paths()[0]

    with lock_path.open("a+b") as handle:
        assert worktree_lock._acquire_file_lock(handle)
        # main is checked for uncommitted work only under the lock: an older merge
        # holding it stages its result in main.
        (real_repo / "other.txt").write_text("other\n", encoding="utf-8")
        _git_output(real_repo, "add", "other.txt")
        blocked = module.cmd_merge(
            argparse.Namespace(name="task-b", message=None, wait_timeout=0.3)
        )
        _git_output(real_repo, "commit", "-m", "other merge")
        worktree_lock._release_file_lock(handle)
    merged_without_lock = (real_repo / "feature-b.txt").exists()
    released = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))

    assert blocked == 1
    assert "merge lock stayed busy" in capsys.readouterr().out
    assert not merged_without_lock
    assert released == 0
    assert (real_repo / "feature-b.txt").exists()


def _start_keeper(directory, monkeypatch, deadline):
    """Run a repair keeper for task-a with its files in *directory*, on a thread."""
    monkeypatch.setattr(worktree_lock, "KEEPER_POLL_SECONDS", 0.02)
    paths = {
        "lock_path": directory / "vbot-merge.lock",
        "holder_path": directory / "vbot-merge.lock.holder.json",
        "release_path": directory / "vbot-merge.lock.release",
    }
    arguments = {name: str(path) for name, path in paths.items()}
    keeper = threading.Thread(
        target=worktree_lock.cmd_keeper_hold,
        args=(argparse.Namespace(task="task-a", deadline=deadline, **arguments),),
    )
    keeper.start()
    return keeper, paths["lock_path"], paths["holder_path"], paths["release_path"]


def test_keeper_hold_releases_on_signal(tmp_path, monkeypatch):
    keeper, lock_path, holder_path, release_path = _start_keeper(
        tmp_path, monkeypatch, time.time() + 30
    )

    opened = False
    for _ in range(200):
        if worktree_lock._own_repair_window_is_active(holder_path, "task-a"):
            opened = True
            break
        time.sleep(0.02)
    assert opened

    release_path.write_text("release\n", encoding="utf-8")
    keeper.join(timeout=10)

    assert not keeper.is_alive()
    assert not holder_path.exists()
    assert worktree_lock._probe_lock_is_busy(lock_path) is False


def test_keeper_hold_expires_at_deadline(tmp_path, monkeypatch):
    deadline = time.time() + 0.5
    keeper, lock_path, holder_path, release_path = _start_keeper(tmp_path, monkeypatch, deadline)

    keeper.join(timeout=15)

    assert not keeper.is_alive()
    assert time.time() >= deadline
    assert not holder_path.exists()
    assert worktree_lock._probe_lock_is_busy(lock_path) is False
    assert not release_path.exists()


def test_merge_takes_the_lock_when_its_window_ended_during_the_branch_check(
    capsys, real_repo, monkeypatch
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _poll_the_merge_lock_quickly(monkeypatch)
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "feature.txt", "a\n", "a file")
    keeper, lock_path, holder_path, release_path = _start_keeper(
        real_repo / ".git", monkeypatch, time.time() + 30
    )
    assert _wait_until(lambda: worktree_lock._own_repair_window_is_active(holder_path, "task-a"))
    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    other_merge = lock_path.open("a+b")

    def window_ends_and_another_merge_starts(_worktree_path):
        release_path.write_text("release\n", encoding="utf-8")
        keeper.join(timeout=10)
        assert worktree_lock._acquire_file_lock(other_merge)
        return 0

    monkeypatch.setattr(module, "_check_branch", window_ends_and_another_merge_starts)
    try:
        result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=0.3))
    finally:
        worktree_lock._release_file_lock(other_merge)
        other_merge.close()

    assert result == 1
    assert "merge lock stayed busy" in capsys.readouterr().out
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert worktree.exists()


class _Clock:
    """A wall clock for the keeper that a test moves forward."""

    sleep = staticmethod(time.sleep)
    monotonic = staticmethod(time.monotonic)

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return time.time() + self.offset


@pytest.mark.parametrize("event", ["deadline passes", "keeper stops"])
def test_a_repair_window_protects_its_merge_until_main_has_it(
    capsys, real_repo, monkeypatch, event
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    clock = _Clock()
    monkeypatch.setattr(worktree_lock, "time", clock)
    worktree = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree, "feature.txt", "a\n", "a file")
    keeper, _lock_path, holder_path, release_path = _start_keeper(
        real_repo / ".git", monkeypatch, clock.time() + 30
    )
    assert _wait_until(lambda: worktree_lock._own_repair_window_is_active(holder_path, "task-a"))
    main_head = _git_output(real_repo, "rev-parse", "HEAD")

    def during_the_check(_landing):
        if event == "deadline passes":
            clock.offset += 60
            passed = clock.time()
            # The keeper sees its deadline pass while the merge holds the lease.
            assert _wait_until(
                lambda: (
                    (worktree_lock._read_holder_record(holder_path) or {}).get("heartbeat", 0)
                    >= passed
                )
            )
            assert keeper.is_alive()
        else:
            release_path.write_text("release\n", encoding="utf-8")
            keeper.join(timeout=10)

    _around_the_merge_check(monkeypatch, module, during_the_check)

    result = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=0.3))
    keeper.join(timeout=10)
    output = capsys.readouterr().out

    assert not keeper.is_alive()
    if event == "deadline passes":
        assert result == 0
        assert _git_output(real_repo, "log", "-1", "--format=%s") == "merge: task-a"
    else:
        # Another merge may hold the lock by now: main must not move.
        assert result == 1
        assert "your repair window ended while the merge commit was checked" in output
        assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
        assert worktree.exists()


def test_repair_start_blocks_others_and_lets_own_merge_win(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _poll_the_merge_lock_quickly(monkeypatch)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "feature-a.txt", "a\n", "a file")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "feature-b.txt", "b\n", "b file")

    started = module.cmd_repair_start(argparse.Namespace(name="task-a", window=20, wait_timeout=15))
    assert started == 0

    blocked = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=0.3))
    assert blocked == 1

    own_merge = module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=30))
    assert own_merge == 0
    assert (real_repo / "feature-a.txt").read_text(encoding="utf-8") == "a\n"
    assert "merge: task-a" in _git_output(real_repo, "log", "--format=%s", "-1")

    holder_path = module._merge_lock_paths()[1]
    window_closed = False
    for _ in range(50):
        if not worktree_lock._own_repair_window_is_active(holder_path, "task-a"):
            window_closed = True
            break
        time.sleep(0.2)
    assert window_closed

    follow_up = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))
    assert follow_up == 0
    assert (real_repo / "feature-b.txt").exists()


def test_repair_finish_closes_window_for_other_tasks(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    _create_task_worktree(module, real_repo, "task-a")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "feature-b.txt", "b\n", "b file")

    assert (
        module.cmd_repair_start(argparse.Namespace(name="task-a", window=60, wait_timeout=15)) == 0
    )
    assert module.cmd_repair_finish(argparse.Namespace(name="task-a")) == 0

    holder_path = module._merge_lock_paths()[1]
    assert not worktree_lock._own_repair_window_is_active(holder_path, "task-a")

    follow_up = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))
    assert follow_up == 0


def test_repair_finish_without_window_reports_already_closed(capsys, real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)

    result = module.cmd_repair_finish(argparse.Namespace(name="quiet-task"))
    captured = capsys.readouterr()

    assert result == 0
    assert "already-closed" in captured.out
