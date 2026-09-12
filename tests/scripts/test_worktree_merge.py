"""worktree merge coverage."""

from __future__ import annotations

import argparse
import subprocess
import threading
import time

from scripts import _worktree_lock as worktree_lock
from tests.scripts.worktree_helpers import (
    _commit_file,
    _create_task_worktree,
    _git_output,
    _list_porcelain,
    _load_worktree_module,
    _patch_repo_globals,
    real_repo,
)

__all__ = ["real_repo"]


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


def test_cmd_merge_rejects_unsafe_name(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    assert module.cmd_merge(argparse.Namespace(name="../escape", message=None, wait_timeout=1)) == 1
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


def test_cmd_merge_merges_removes_worktree_and_branch(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "feature-a.txt", "a\n", "add feature a")

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


def test_cmd_merge_reports_conflict_and_keeps_main_intact(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "one\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "shared.txt", "from-a\n", "a edit")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "shared.txt", "from-b\n", "b edit")

    assert module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60)) == 0

    main_head = _git_output(real_repo, "rev-parse", "HEAD")
    result = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))

    assert result == module.MERGE_CONFLICT_EXIT_CODE
    assert _git_output(real_repo, "rev-parse", "HEAD") == main_head
    assert (real_repo / "shared.txt").read_text(encoding="utf-8") == "from-a\n"
    assert worktree_b.exists()


def test_cmd_merge_reports_conflict_hints(capsys, real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "one\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "shared.txt", "from-a\n", "a edit")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "shared.txt", "from-b\n", "b edit")

    module.cmd_merge(argparse.Namespace(name="task-a", message=None, wait_timeout=60))
    module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))
    captured = capsys.readouterr()

    assert "conflicted: shared.txt" in captured.out
    assert "python scripts/worktree.py repair-start task-b" in captured.out
    assert "python scripts/worktree.py merge task-b" in captured.out


def test_cmd_merge_recovers_unfinished_merge_state(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "one\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "other-a.txt", "a\n", "a file")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "other-b.txt", "b\n", "b file")

    # Simulate a hard kill in the middle of an earlier merge attempt.
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


def test_merge_lock_blocks_second_merger_until_release(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    _create_task_worktree(module, real_repo, "task-a")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "feature-b.txt", "b\n", "b file")

    lock_path, _, _ = module._merge_lock_paths()
    handle = lock_path.open("a+b")
    assert worktree_lock._acquire_file_lock(handle)

    blocked = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=1.5))
    captured_after_block = blocked

    worktree_lock._release_file_lock(handle)
    handle.close()

    released = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))

    assert captured_after_block == 1
    assert released == 0
    assert (real_repo / "feature-b.txt").exists()


def test_keeper_hold_releases_on_signal(tmp_path):
    lock_path = tmp_path / "vbot-merge.lock"
    holder_path = tmp_path / "vbot-merge.lock.holder.json"
    release_path = tmp_path / "vbot-merge.lock.release"

    keeper = threading.Thread(
        target=worktree_lock.cmd_keeper_hold,
        args=(
            argparse.Namespace(
                task="task-a",
                deadline=time.time() + 30,
                lock_path=str(lock_path),
                holder_path=str(holder_path),
                release_path=str(release_path),
            ),
        ),
    )
    keeper.start()

    opened = False
    for _ in range(80):
        if worktree_lock._own_repair_window_is_active(holder_path, "task-a"):
            opened = True
            break
        time.sleep(0.05)
    assert opened

    release_path.write_text("release\n", encoding="utf-8")
    keeper.join(timeout=10)

    assert not keeper.is_alive()
    assert not holder_path.exists()
    assert worktree_lock._probe_lock_is_busy(lock_path) is False


def test_keeper_hold_expires_at_deadline(tmp_path):
    lock_path = tmp_path / "vbot-merge.lock"
    holder_path = tmp_path / "vbot-merge.lock.holder.json"
    release_path = tmp_path / "vbot-merge.lock.release"

    keeper = threading.Thread(
        target=worktree_lock.cmd_keeper_hold,
        args=(
            argparse.Namespace(
                task="task-a",
                deadline=time.time() + 2,
                lock_path=str(lock_path),
                holder_path=str(holder_path),
                release_path=str(release_path),
            ),
        ),
    )
    keeper.start()
    keeper.join(timeout=15)

    assert not keeper.is_alive()
    assert worktree_lock._probe_lock_is_busy(lock_path) is False
    assert not release_path.exists()


def test_repair_start_blocks_others_and_lets_own_merge_win(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    worktree_a = _create_task_worktree(module, real_repo, "task-a")
    _commit_file(worktree_a, "feature-a.txt", "a\n", "a file")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "feature-b.txt", "b\n", "b file")

    started = module.cmd_repair_start(argparse.Namespace(name="task-a", window=20, wait_timeout=15))
    assert started == 0

    blocked = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=1.5))
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
