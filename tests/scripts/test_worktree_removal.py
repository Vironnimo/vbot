"""worktree removal coverage."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.scripts.worktree_helpers import (
    _commit_file,
    _create_task_worktree,
    _git,
    _git_output,
    _load_worktree_module,
    _patch_repo_globals,
    _record_owned_data,
    real_repo,
)

__all__ = ["real_repo"]


def _make_checkout(worktree_path: Path) -> Path:
    """Create a fake linked checkout; its `.git` file marks the checkout as present."""
    worktree_path.mkdir(parents=True)
    (worktree_path / ".git").write_text("gitdir: missing-admin-dir\n", encoding="utf-8")
    return worktree_path


def test_parse_args_accepts_create_delete_and_list():
    module = _load_worktree_module()

    assert module.parse_args(["create", "task"]).command == "create"
    assert module.parse_args(["delete", "task"]).command == "delete"
    assert module.parse_args(["list"]).command == "list"


@pytest.mark.parametrize("name", ["nested/task", "dev", "DEV", "dev."])
def test_cmd_delete_rejects_unsafe_name(tmp_path, monkeypatch, name):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 1
    assert commands == []


def test_cmd_delete_preserves_both_data_dirs_when_marker_is_tampered(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    name = "safe-delete"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)
    expected_data_dir = tmp_path / "home" / f".vbot-{name}"
    expected_data_dir.mkdir(parents=True)
    malicious_target = tmp_path / "malicious-target"
    malicious_target.mkdir()

    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(malicious_target)}),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert expected_data_dir.exists()
    assert malicious_target.exists()
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_delete_stops_managed_services_before_removing_worktree(tmp_path, monkeypatch):
    module = _load_worktree_module()
    name = "running-worktree"
    worktree_path = tmp_path / ".worktrees" / name
    data_dir = tmp_path / "home" / f".vbot-{name}"
    _make_checkout(worktree_path)
    (worktree_path / "scripts").mkdir()
    (worktree_path / "scripts" / "test-env.py").write_text("", encoding="utf-8")
    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": f"~/.vbot-{name}", "managed_branch": False}),
        encoding="utf-8",
    )
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(json.dumps({"server_port": 8422}), encoding="utf-8")
    calls = []

    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)

    _record_owned_data(module, worktree_path, data_dir)

    def fake_run_command(command, *, cwd=None):
        calls.append((command, cwd))
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    assert module.cmd_delete(argparse.Namespace(name=name, force=False)) == 0
    assert calls[0] == (
        [
            module.sys.executable,
            str(worktree_path / "scripts" / "test-env.py"),
            "stop",
            "--host",
            "127.0.0.1",
            "--data-dir",
            str(data_dir),
            "--port",
            "8422",
        ],
        worktree_path,
    )
    assert calls[1][0][:3] == ["git", "-C", str(worktree_path)]
    assert not data_dir.exists()


def test_cmd_delete_reports_stop_failure_without_removing_anything(tmp_path, monkeypatch):
    module = _load_worktree_module()
    name = "unstoppable-worktree"
    worktree_path = tmp_path / ".worktrees" / name
    data_dir = tmp_path / "home" / f".vbot-{name}"
    _make_checkout(worktree_path)
    (worktree_path / "scripts").mkdir()
    (worktree_path / "scripts" / "test-env.py").write_text("", encoding="utf-8")
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text("{}", encoding="utf-8")
    calls = []

    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)

    _record_owned_data(module, worktree_path, data_dir)

    def fake_run_command(command, *, cwd=None):
        calls.append(command)
        return 1, "still running"

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    assert module.cmd_delete(argparse.Namespace(name=name, force=True)) == 1
    assert len(calls) == 1
    assert calls[0][2] == "stop"
    assert worktree_path.exists()
    assert data_dir.exists()


def test_cmd_delete_reports_data_directory_removal_failure(tmp_path, monkeypatch):
    module = _load_worktree_module()
    name = "locked-data"
    worktree_path = tmp_path / ".worktrees" / name
    data_dir = tmp_path / "home" / f".vbot-{name}"
    _make_checkout(worktree_path)
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module, "_run_command", lambda _command, *, cwd=None: (0, ""))
    monkeypatch.setattr(module, "_remove_directory_tree", lambda _path: "locked")
    _record_owned_data(module, worktree_path, data_dir)

    assert module.cmd_delete(argparse.Namespace(name=name, force=True)) == 1
    assert data_dir.exists()


@pytest.mark.parametrize("ownership", ["legacy", "different-token", "different-repository"])
@pytest.mark.parametrize("force", [False, True])
def test_cmd_delete_preserves_unowned_data_without_stopping_services(
    real_repo, monkeypatch, capsys, ownership, force
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "unowned"
    worktree_path = _create_task_worktree(module, real_repo, name)
    data_dir = real_repo.parent / "home" / f".vbot-{name}"
    data_dir.mkdir(parents=True)
    sentinel = data_dir / "settings.json"
    sentinel.write_text('{"server_port": 8421}', encoding="utf-8")
    if ownership != "legacy":
        _record_owned_data(module, worktree_path, data_dir)
        owner_path = data_dir / module.DATA_OWNER_FILE_NAME
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        field = "data_owner" if ownership == "different-token" else "repository"
        owner[field] = "somewhere-else"
        owner_path.write_text(json.dumps(owner), encoding="utf-8")
    stop_calls = []
    monkeypatch.setattr(module, "_stop_worktree_services", lambda *args: stop_calls.append(args))

    assert module.cmd_delete(argparse.Namespace(name=name, force=force)) == 0

    assert not worktree_path.exists()
    assert sentinel.read_text(encoding="utf-8") == '{"server_port": 8421}'
    assert stop_calls == []
    assert "data-status: preserved (ownership unverified)" in capsys.readouterr().out


def test_cmd_delete_preserves_redirected_data_root_even_with_matching_records(
    real_repo, monkeypatch, capsys
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "redirected"
    worktree_path = _create_task_worktree(module, real_repo, name)
    data_dir = real_repo.parent / "home" / f".vbot-{name}"
    data_dir.mkdir(parents=True)
    _record_owned_data(module, worktree_path, data_dir)
    (data_dir / "sentinel.txt").write_text("keep", encoding="utf-8")
    foreign_root = data_dir.with_name(".vbot-dev")
    data_dir.rename(foreign_root)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(data_dir), str(foreign_root)],
            check=True,
            capture_output=True,
        )
    else:
        data_dir.symlink_to(foreign_root, target_is_directory=True)
    stop_calls = []
    monkeypatch.setattr(module, "_stop_worktree_services", lambda *args: stop_calls.append(args))

    assert module.cmd_delete(argparse.Namespace(name=name, force=True)) == 0

    assert (foreign_root / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert data_dir.exists()
    assert stop_calls == []
    assert "data-status: preserved (ownership unverified)" in capsys.readouterr().out


def test_cmd_delete_rechecks_ownership_after_git_removal(real_repo, monkeypatch, capsys):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "replaced-root"
    worktree_path = _create_task_worktree(module, real_repo, name)
    data_dir = real_repo.parent / "home" / f".vbot-{name}"
    data_dir.mkdir(parents=True)
    _record_owned_data(module, worktree_path, data_dir)
    real_run_command = module._run_command

    def run_command(command, *, cwd=None):
        result = real_run_command(command, cwd=cwd)
        if command[:3] == ["git", "worktree", "remove"]:
            (data_dir / module.DATA_OWNER_FILE_NAME).unlink()
            (data_dir / "foreign.txt").write_text("replacement", encoding="utf-8")
        return result

    monkeypatch.setattr(module, "_run_command", run_command)

    assert module.cmd_delete(argparse.Namespace(name=name, force=True)) == 0

    assert (data_dir / "foreign.txt").read_text(encoding="utf-8") == "replacement"
    assert "data-status: preserved (ownership unverified)" in capsys.readouterr().out


def test_cmd_delete_missing_marker_same_name_branch_skips_branch_delete(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "missing-marker"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [["git", "worktree", "remove", str(worktree_path)]]


def test_cmd_delete_tolerates_non_object_marker_and_skips_branch_delete(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "existing-branch"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps(["not", "an", "object"]),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_delete_malformed_marker_same_name_branch_skips_branch_delete(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "bad-marker"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    (worktree_path / module.WORKTREE_FILE_NAME).write_text("{not-json", encoding="utf-8")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_delete_force_skips_marker_cleanup(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "force-remove"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": f"~/.vbot-{name}", "managed_branch": False}),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: "main")
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=True))

    assert result == 0
    assert commands == [["git", "worktree", "remove", "--force", str(worktree_path)]]


def test_cmd_delete_skips_branch_delete_when_marker_declares_unmanaged_branch(
    tmp_path, monkeypatch
):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "from-existing"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": f"~/.vbot-{name}", "managed_branch": False}),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_delete_deletes_branch_when_marker_declares_managed_branch(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "managed"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": f"~/.vbot-{name}", "managed_branch": True}),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "branch", "-d", name],
    ]


def test_remove_directory_tree_clears_readonly_files(tmp_path):
    module = _load_worktree_module()

    tree = tmp_path / "tree"
    tree.mkdir()
    locked_file = tree / "readonly.txt"
    locked_file.write_text("x", encoding="utf-8")
    locked_file.chmod(0o444)

    result = module._remove_directory_tree(tree)

    assert result is None
    assert not tree.exists()


def test_sweep_trash_directories_removes_only_trash_dirs(tmp_path):
    module = _load_worktree_module()

    worktrees_dir = tmp_path / ".worktrees"
    trash_dir = worktrees_dir / f"{module.TRASH_DIR_PREFIX}old-task-123"
    trash_dir.mkdir(parents=True)
    (trash_dir / "leftover.txt").write_text("x", encoding="utf-8")
    kept_dir = worktrees_dir / "active-task"
    kept_dir.mkdir()

    module.sweep_trash_directories(worktrees_dir)

    assert not trash_dir.exists()
    assert kept_dir.exists()


def test_cmd_delete_finishes_removal_when_git_fails_on_locked_files(tmp_path, monkeypatch, capsys):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "locked-worktree"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)
    (worktree_path / "leftover.txt").write_text("x", encoding="utf-8")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, f"error: failed to delete '{worktree_path}': Invalid argument"
        return 0, ""

    terminate_calls = []

    def fake_terminate(path):
        terminate_calls.append(path)
        return [str(path / "webui" / "node_modules" / "esbuild.exe")]

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_terminate_worktree_processes", fake_terminate)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)

    result = module.cmd_delete(argparse.Namespace(name=name, force=True))

    captured = capsys.readouterr()
    assert result == 0
    assert terminate_calls == [worktree_path]
    assert not worktree_path.exists()
    assert ["git", "worktree", "prune"] in commands
    assert "terminated:" in captured.out
    assert "status: deleted" in captured.out


def test_cmd_delete_moves_stuck_worktree_to_trash(tmp_path, monkeypatch, capsys):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "stuck-worktree"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)
    (worktree_path / "held-by-editor.node").write_text("x", encoding="utf-8")

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, f"error: failed to delete '{worktree_path}': Invalid argument"
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_terminate_worktree_processes", lambda _path: [])
    monkeypatch.setattr(module, "_remove_directory_tree", lambda _path: "still locked")
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=True))

    captured = capsys.readouterr()
    assert result == 0
    assert not worktree_path.exists()
    trash_dirs = [
        path
        for path in module.WORKTREES_DIR.iterdir()
        if path.name.startswith(module.TRASH_DIR_PREFIX)
    ]
    assert len(trash_dirs) == 1
    assert "leftover:" in captured.out
    assert "status: deleted" in captured.out


def test_cmd_delete_lists_uncommitted_files_when_non_force_remove_fails(
    tmp_path, monkeypatch, capsys
):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "dirty-worktree"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, f"fatal: '{worktree_path}' contains modified or untracked files"
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module, "_worktree_registration_state", lambda _path: True)
    monkeypatch.setattr(
        module,
        "_list_uncommitted_paths",
        lambda _path: ["?? docs/plans/task.md", " M webui/src/App.svelte"],
    )
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    captured = capsys.readouterr()
    assert result == 1
    assert "uncommitted: ?? docs/plans/task.md" in captured.out
    assert "uncommitted:  M webui/src/App.svelte" in captured.out


def test_cmd_delete_restores_marker_after_failed_remove_for_retry(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "managed-retry"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)

    marker_path = worktree_path / module.WORKTREE_FILE_NAME
    marker_path.write_text(
        json.dumps({"data_dir": f"~/.vbot-{name}", "managed_branch": True}),
        encoding="utf-8",
    )

    commands = []
    remove_calls = 0

    def fake_run_command(command, *, cwd=None):
        nonlocal remove_calls
        commands.append(command)

        if command[:4] == ["git", "-C", str(worktree_path), "clean"]:
            marker_path.unlink(missing_ok=True)
            return 0, ""

        if command[:3] == ["git", "worktree", "remove"]:
            remove_calls += 1
            if remove_calls == 1:
                return 1, "dirty state"
            return 0, ""

        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module, "_worktree_registration_state", lambda _path: True)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    first_result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert first_result == 1
    assert marker_path.exists()

    second_result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert second_result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "branch", "-d", name],
    ]


def test_cmd_delete_non_force_fails_closed_for_localized_remove_error(
    tmp_path, monkeypatch, capsys
):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "localized-error"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)
    protected = worktree_path / "important.txt"
    protected.write_text("keep", encoding="utf-8")

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, "Fehler: Arbeitsverzeichnis enthalt nicht gespeicherte Anderungen"
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module, "_worktree_registration_state", lambda _path: True)
    monkeypatch.setattr(module, "_terminate_worktree_processes", pytest.fail)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 1
    assert protected.read_text(encoding="utf-8") == "keep"
    assert "Fehler:" in capsys.readouterr().out


def test_cmd_delete_non_force_finishes_only_after_verified_deregistration(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "deregistered"
    worktree_path = module.WORKTREES_DIR / name
    _make_checkout(worktree_path)
    (worktree_path / "leftover.txt").write_text("x", encoding="utf-8")

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, "could not delete locked files"
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module, "_worktree_registration_state", lambda _path: False)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert not worktree_path.exists()


def _strip_to_marker_only(module, worktree_path: Path) -> None:
    """Leave only the marker, as a merge whose directory removal failed does."""
    for entry in worktree_path.iterdir():
        if entry.name == module.WORKTREE_FILE_NAME:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _seed_leftover_data_dir(repo: Path, name: str, port: int) -> Path:
    data_dir = repo.parent / "home" / f".vbot-{name}"
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(json.dumps({"server_port": port}), encoding="utf-8")
    return data_dir


def _intercept_service_stop(module, monkeypatch) -> list[tuple[list[str], Path | None]]:
    """Record `test-env.py stop` calls; every other command runs for real."""
    stop_calls: list[tuple[list[str], Path | None]] = []
    real_run_command = module._run_command

    def run_command(command, *, cwd=None):
        if command[1:2] and command[1].endswith("test-env.py"):
            stop_calls.append((command, cwd))
            return 0, ""
        return real_run_command(command, cwd=cwd)

    monkeypatch.setattr(module, "_run_command", run_command)
    monkeypatch.setattr(module, "_terminate_worktree_processes", lambda _path: [])
    return stop_calls


def _branch_exists(repo: Path, name: str) -> bool:
    return bool(_git_output(repo, "branch", "--list", name))


@pytest.mark.parametrize("force", [False, True])
def test_cmd_delete_finishes_marker_only_leftover(real_repo, monkeypatch, capsys, force):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "leftover"
    worktree_path = _create_task_worktree(module, real_repo, name)
    _commit_file(worktree_path, "task.txt", "done\n", "task work")
    _git(real_repo, "merge", "--no-ff", name, "-m", f"merge: {name}")
    _strip_to_marker_only(module, worktree_path)
    data_dir = _seed_leftover_data_dir(real_repo, name, 8433)
    _record_owned_data(module, worktree_path, data_dir)
    stop_calls = _intercept_service_stop(module, monkeypatch)
    # The enclosing repository is on main; the leftover must not resolve to it.
    assert module._read_worktree_branch_name(worktree_path) is None

    assert module.cmd_delete(argparse.Namespace(name=name, force=force)) == 0

    assert "status: deleted" in capsys.readouterr().out
    assert not worktree_path.exists()
    assert not data_dir.exists()
    assert _git_output(real_repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not _branch_exists(real_repo, name)
    checkout_root = module._script_checkout_root()
    assert stop_calls == [
        (
            [
                module.sys.executable,
                str(checkout_root / "scripts" / "test-env.py"),
                "stop",
                "--host",
                "127.0.0.1",
                "--data-dir",
                str(data_dir),
                "--port",
                "8433",
            ],
            checkout_root,
        )
    ]


@pytest.mark.parametrize("force", [False, True])
def test_cmd_delete_leftover_deletes_unmerged_branch_only_with_force(
    real_repo, monkeypatch, capsys, force
):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "unmerged-leftover"
    worktree_path = _create_task_worktree(module, real_repo, name)
    _commit_file(worktree_path, "task.txt", "unmerged\n", "unmerged work")
    _strip_to_marker_only(module, worktree_path)
    _intercept_service_stop(module, monkeypatch)

    result = module.cmd_delete(argparse.Namespace(name=name, force=force))

    assert not worktree_path.exists()
    assert _git_output(real_repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    if force:
        assert result == 0
        assert not _branch_exists(real_repo, name)
    else:
        # `git branch -d` keeps the unmerged commits reachable.
        assert result == 1
        assert "not fully merged" in capsys.readouterr().out
        assert _branch_exists(real_repo, name)


def test_read_registered_branch_name_reads_git_registration(real_repo):
    module = _load_worktree_module()
    worktree_path = _create_task_worktree(module, real_repo, "registered")
    unregistered = real_repo / ".worktrees" / "unregistered"
    unregistered.mkdir()

    assert module._read_registered_branch_name(real_repo, worktree_path) == "registered"
    assert module._read_registered_branch_name(real_repo, unregistered) is None
    assert module._read_worktree_branch_name(unregistered) is None
