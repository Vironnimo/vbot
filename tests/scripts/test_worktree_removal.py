"""worktree removal coverage."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from scripts import _worktree_args as worktree_args
from scripts import _worktree_context as worktree_context
from scripts import _worktree_records as worktree_records
from scripts import _worktree_removal as worktree_removal
from tests.directory_links import link_directory
from tests.scripts.worktree_helpers import (
    _commit_file,
    _create_task_worktree,
    _git,
    _git_output,
    _patch_repo_globals,
    _record_owned_data,
    _worktree_manager,
    real_repo,
)

__all__ = ["real_repo"]


def _make_checkout(worktree_path: Path) -> Path:
    """Create a fake linked checkout; its `.git` file marks the checkout as present."""
    worktree_path.mkdir(parents=True)
    (worktree_path / ".git").write_text("gitdir: missing-admin-dir\n", encoding="utf-8")
    return worktree_path


def test_parse_args_accepts_create_delete_and_list():

    assert worktree_args.parse_args(["create", "task"]).command == "create"
    assert worktree_args.parse_args(["delete", "task"]).command == "delete"
    assert worktree_args.parse_args(["list"]).command == "list"


@pytest.mark.parametrize("name", ["nested/task", "dev", "DEV", "dev."])
def test_cmd_delete_rejects_unsafe_name(tmp_path, monkeypatch, name):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)

    result = module.removal.delete(argparse.Namespace(name=name, force=False))

    assert result == 1
    assert commands == []


def test_cmd_delete_preserves_both_data_dirs_when_marker_is_tampered(tmp_path, monkeypatch):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")
    monkeypatch.setattr(worktree_context.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module.context, "data_root", tmp_path / "data")

    name = "safe-delete"
    worktree_path = module.context.worktrees_dir / name
    _make_checkout(worktree_path)
    expected_data_dir = tmp_path / "data" / name
    expected_data_dir.mkdir(parents=True)
    malicious_target = tmp_path / "malicious-target"
    malicious_target.mkdir()

    (worktree_path / worktree_records.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(malicious_target)}),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)

    result = module.removal.delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert expected_data_dir.exists()
    assert malicious_target.exists()
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", worktree_records.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


@pytest.mark.parametrize(
    ("settings", "marker", "port", "data_root"),
    [
        pytest.param(
            {"server_port": 8422}, {"server_port": 8423}, "8422", "data", id="settings-port"
        ),
        pytest.param({}, {"server_port": 8422}, "8422", "data", id="port-recorded-at-creation"),
        pytest.param(None, {"server_port": 8422}, "8422", "data", id="missing-settings"),
        pytest.param({}, {}, None, "data", id="unknown-port-stops-nothing"),
        pytest.param(None, {}, None, "data", id="missing-settings-and-port"),
        *[
            pytest.param(
                {"server_port": value},
                {"server_port": 8422},
                "8422",
                "data",
                id=f"invalid-settings-{value}",
            )
            for value in (True, False, 0, -1, 65_536)
        ],
        *[
            pytest.param(None, {"server_port": value}, None, "data", id=f"invalid-marker-{value}")
            for value in (True, False, 0, -1, 65_536)
        ],
        # Worktrees created before the sibling data root keep ~/.vbot-<name>.
        pytest.param({"server_port": 8422}, {}, "8422", "home", id="legacy-home-data-dir"),
    ],
)
def test_cmd_delete_stops_managed_services_before_removing_worktree(
    tmp_path, monkeypatch, settings, marker, port, data_root
):
    module = _worktree_manager()
    name = "running-worktree"
    worktree_path = tmp_path / ".worktrees" / name
    data_dir = (
        tmp_path / "data" / name if data_root == "data" else tmp_path / "home" / f".vbot-{name}"
    )
    _make_checkout(worktree_path)
    (worktree_path / "scripts").mkdir()
    (worktree_path / "scripts" / "test-env.py").write_text("", encoding="utf-8")
    (worktree_path / worktree_records.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(data_dir), "managed_branch": False, **marker}),
        encoding="utf-8",
    )
    data_dir.mkdir(parents=True)
    if settings is not None:
        (data_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    calls = []

    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")
    monkeypatch.setattr(worktree_context.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module.context, "data_root", tmp_path / "data")
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)

    _record_owned_data(module, worktree_path, data_dir)

    def fake_run_command(command, *, cwd=None):
        calls.append((command, cwd))
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)

    result = module.removal.delete(argparse.Namespace(name=name, force=False))
    if port is None:
        # Without a known port, stop would target vBot's default port, where
        # another installation may run.
        assert all("stop" not in command for command, _cwd in calls)
        assert result == 1
        assert data_dir.is_dir() and worktree_path.is_dir()
        return
    assert result == 0
    assert not data_dir.exists()
    assert calls[0] == (
        [
            worktree_removal.sys.executable,
            str(worktree_path / "scripts" / "test-env.py"),
            "stop",
            "--host",
            "127.0.0.1",
            "--data-dir",
            str(data_dir),
            "--port",
            port,
        ],
        worktree_path,
    )
    assert calls[1][0][:3] == ["git", "-C", str(worktree_path)]


@pytest.mark.parametrize("failure", ["stop", "remaining-claim", "unreadable-claims"])
def test_cmd_delete_reports_stop_failure_without_removing_anything(tmp_path, monkeypatch, failure):
    module = _worktree_manager()
    name = "unstoppable-worktree"
    worktree_path = tmp_path / ".worktrees" / name
    data_dir = tmp_path / "data" / name
    _make_checkout(worktree_path)
    (worktree_path / "scripts").mkdir()
    (worktree_path / "scripts" / "test-env.py").write_text("", encoding="utf-8")
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(json.dumps({"server_port": 8422}), encoding="utf-8")
    calls = []

    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")
    monkeypatch.setattr(worktree_context.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module.context, "data_root", tmp_path / "data")
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)

    _record_owned_data(module, worktree_path, data_dir)

    def fake_run_command(command, *, cwd=None):
        calls.append(command)
        return (1, "still running") if failure == "stop" else (0, "")

    def claims(_data_dir):
        if failure == "unreadable-claims":
            raise PermissionError("cannot enumerate claims")
        return (8423,)

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_removal, "live_server_ports", claims)

    assert module.removal.delete(argparse.Namespace(name=name, force=True)) == 1
    assert len(calls) == 1
    assert calls[0][2] == "stop"
    assert worktree_path.exists()
    assert data_dir.exists()


def test_cmd_delete_reports_data_directory_removal_failure(tmp_path, monkeypatch):
    module = _worktree_manager()
    name = "locked-data"
    worktree_path = tmp_path / ".worktrees" / name
    data_dir = tmp_path / "data" / name
    _make_checkout(worktree_path)
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")
    monkeypatch.setattr(worktree_context.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module.context, "data_root", tmp_path / "data")
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.context, "run_command", lambda _command, *, cwd=None: (0, ""))
    monkeypatch.setattr(module.removal, "remove_directory_tree", lambda _path: "locked")
    _record_owned_data(module, worktree_path, data_dir)

    assert module.removal.delete(argparse.Namespace(name=name, force=True)) == 1
    assert data_dir.exists()


@pytest.mark.parametrize("ownership", ["legacy", "different-token", "different-repository"])
def test_cmd_delete_preserves_unowned_data_without_stopping_services(
    real_repo, monkeypatch, capsys, ownership
):
    module = _worktree_manager()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "unowned"
    worktree_path = _create_task_worktree(module, real_repo, name)
    data_dir = real_repo.parent / "data" / name
    data_dir.mkdir(parents=True)
    sentinel = data_dir / "settings.json"
    sentinel.write_text('{"server_port": 8421}', encoding="utf-8")
    if ownership != "legacy":
        _record_owned_data(module, worktree_path, data_dir)
        owner_path = data_dir / worktree_records.DATA_OWNER_FILE_NAME
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        field = "data_owner" if ownership == "different-token" else "repository"
        owner[field] = "somewhere-else"
        owner_path.write_text(json.dumps(owner), encoding="utf-8")
    stop_calls = []
    monkeypatch.setattr(
        module.removal, "stop_worktree_services", lambda *args: stop_calls.append(args)
    )

    # Even a forced delete, which discards uncommitted work, keeps the data.
    assert module.removal.delete(argparse.Namespace(name=name, force=True)) == 0

    assert not worktree_path.exists()
    assert sentinel.read_text(encoding="utf-8") == '{"server_port": 8421}'
    assert stop_calls == []
    assert "data-status: preserved (ownership unverified)" in capsys.readouterr().out


def test_cmd_delete_preserves_redirected_data_root_even_with_matching_records(
    real_repo, monkeypatch, capsys
):
    module = _worktree_manager()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "redirected"
    worktree_path = _create_task_worktree(module, real_repo, name)
    data_dir = real_repo.parent / "data" / name
    data_dir.mkdir(parents=True)
    _record_owned_data(module, worktree_path, data_dir)
    (data_dir / "sentinel.txt").write_text("keep", encoding="utf-8")
    foreign_root = data_dir.with_name("dev")
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
    monkeypatch.setattr(
        module.removal, "stop_worktree_services", lambda *args: stop_calls.append(args)
    )

    assert module.removal.delete(argparse.Namespace(name=name, force=True)) == 0

    assert (foreign_root / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert data_dir.exists()
    assert stop_calls == []
    assert "data-status: preserved (ownership unverified)" in capsys.readouterr().out


def test_cmd_delete_rechecks_ownership_after_git_removal(real_repo, monkeypatch, capsys):
    module = _worktree_manager()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "replaced-root"
    worktree_path = _create_task_worktree(module, real_repo, name)
    data_dir = real_repo.parent / "data" / name
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text('{"server_port": 8422}', encoding="utf-8")
    _record_owned_data(module, worktree_path, data_dir)
    real_run_command = module.context.run_command

    def run_command(command, *, cwd=None):
        if command[1:2] and command[1].endswith("test-env.py"):
            return 0, ""
        result = real_run_command(command, cwd=cwd)
        if command[:3] == ["git", "worktree", "remove"]:
            (data_dir / worktree_records.DATA_OWNER_FILE_NAME).unlink()
            (data_dir / "foreign.txt").write_text("replacement", encoding="utf-8")
        return result

    monkeypatch.setattr(module.context, "run_command", run_command)

    assert module.removal.delete(argparse.Namespace(name=name, force=True)) == 0

    assert (data_dir / "foreign.txt").read_text(encoding="utf-8") == "replacement"
    assert "data-status: preserved (ownership unverified)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("marker", "branch", "force", "expected"),
    [
        pytest.param(None, "task", False, ["remove"], id="missing-marker"),
        pytest.param(
            '["not", "an", "object"]', "task", False, ["clean", "remove"], id="non-object"
        ),
        pytest.param("{not-json", "task", False, ["clean", "remove"], id="malformed"),
        pytest.param(
            '{"managed_branch": false}', "task", False, ["clean", "remove"], id="unmanaged-branch"
        ),
        pytest.param(
            '{"managed_branch": true}', "other", False, ["clean", "remove"], id="renamed-branch"
        ),
        pytest.param(
            '{"managed_branch": true}',
            "task",
            False,
            ["clean", "remove", "delete-branch"],
            id="managed-branch",
        ),
        pytest.param('{"managed_branch": false}', "task", True, ["force-remove"], id="force"),
    ],
)
def test_cmd_delete_deletes_only_the_branch_a_valid_marker_manages(
    tmp_path, monkeypatch, marker, branch, force, expected
):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")
    monkeypatch.setattr(worktree_context.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module.context, "data_root", tmp_path / "data")
    worktree_path = _make_checkout(module.context.worktrees_dir / "task")
    if marker is not None:
        (worktree_path / worktree_records.WORKTREE_FILE_NAME).write_text(marker, encoding="utf-8")
    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: branch)
    monkeypatch.setattr(worktree_context.shutil, "rmtree", lambda *_args, **_kwargs: None)

    assert module.removal.delete(argparse.Namespace(name="task", force=force)) == 0

    known = {
        "clean": [
            "git",
            "-C",
            str(worktree_path),
            "clean",
            "-f",
            "--",
            worktree_records.WORKTREE_FILE_NAME,
        ],
        "remove": ["git", "worktree", "remove", str(worktree_path)],
        "force-remove": ["git", "worktree", "remove", "--force", str(worktree_path)],
        "delete-branch": ["git", "branch", "-d", "task"],
    }
    assert commands == [known[step] for step in expected]


def test_remove_directory_tree_clears_readonly_files(tmp_path, monkeypatch):
    module = _worktree_manager()

    tree = tmp_path / "tree"
    tree.mkdir()
    locked_file = tree / "readonly.txt"
    locked_file.write_text("x", encoding="utf-8")
    locked_file.chmod(0o444)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("kept", encoding="utf-8")
    link_directory(tree / "link", outside)
    # The link refuses its first removal, as a read-only link does on Windows; clearing
    # that attribute must never reach the read-only directory the link points at.
    outside.chmod(stat.S_IREAD | stat.S_IEXEC)
    real_unlink, refused = os.unlink, []

    def unlink(path, *args, **kwargs):
        if Path(path).name == "link" and not refused:
            refused.append(path)
            raise PermissionError(errno.EACCES, "read-only link", str(path))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)

    result = module.removal.remove_directory_tree(tree)

    assert result is None
    assert not tree.exists()
    assert len(refused) == 1
    assert not os.stat(outside).st_mode & stat.S_IWRITE
    assert (outside / "kept.txt").read_text(encoding="utf-8") == "kept"
    outside.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.name != "nt", reason="Windows process cleanup uses PowerShell")
def test_process_cleanup_matches_only_literal_worktree_paths(monkeypatch):
    module = _worktree_manager()
    worktree = Path(r"C:\dev\[ab]vbot's\.worktrees\audit")
    owned = [str(worktree / "tool.exe"), str(worktree / "other.exe").swapcase()]
    paths = [
        *owned,
        str(worktree / "unrelated.exe").replace("[ab]", "a"),
        str(worktree) + r"-other\tool.exe",
    ]
    records = json.dumps([{"Path": path, "Id": index} for index, path in enumerate(paths)])
    # Exercise the generated PowerShell predicate without enumerating or stopping
    # any real process. The subprocess is the Windows-only cleanup interpreter.
    setup = (
        "function Get-Process { foreach ($record in (ConvertFrom-Json '"
        + records.replace("'", "''")
        + "')) { $record } }; "
        "function Stop-Process { param($Id, [switch]$Force, $ErrorAction) }; "
    )
    run = subprocess.run

    def fake_processes(command, **kwargs):
        return run([*command[:-1], setup + command[-1]], **kwargs)

    monkeypatch.setattr(worktree_removal.subprocess, "run", fake_processes)

    assert module.removal._terminate_worktree_processes(worktree) == owned


def test_sweep_trash_directories_removes_only_trash_dirs(tmp_path):
    module = _worktree_manager()

    worktrees_dir = tmp_path / ".worktrees"
    trash_dir = worktrees_dir / f"{worktree_removal.TRASH_DIR_PREFIX}old-task-123"
    trash_dir.mkdir(parents=True)
    (trash_dir / "leftover.txt").write_text("x", encoding="utf-8")
    kept_dir = worktrees_dir / "active-task"
    kept_dir.mkdir()

    module.removal.sweep_trash_directories(worktrees_dir)

    assert not trash_dir.exists()
    assert kept_dir.exists()


def test_cmd_delete_finishes_removal_when_git_fails_on_locked_files(tmp_path, monkeypatch, capsys):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    name = "locked-worktree"
    worktree_path = module.context.worktrees_dir / name
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

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(module.removal, "_terminate_worktree_processes", fake_terminate)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)

    result = module.removal.delete(argparse.Namespace(name=name, force=True))

    captured = capsys.readouterr()
    assert result == 0
    assert terminate_calls == [worktree_path]
    assert not worktree_path.exists()
    assert ["git", "worktree", "prune"] in commands
    assert "terminated:" in captured.out
    assert "status: deleted" in captured.out


def test_cmd_delete_moves_stuck_worktree_to_trash(tmp_path, monkeypatch, capsys):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    name = "stuck-worktree"
    worktree_path = module.context.worktrees_dir / name
    _make_checkout(worktree_path)
    (worktree_path / "held-by-editor.node").write_text("x", encoding="utf-8")

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, f"error: failed to delete '{worktree_path}': Invalid argument"
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(module.removal, "_terminate_worktree_processes", lambda _path: [])
    monkeypatch.setattr(module.removal, "remove_directory_tree", lambda _path: "still locked")
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(worktree_context.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.removal.delete(argparse.Namespace(name=name, force=True))

    captured = capsys.readouterr()
    assert result == 0
    assert not worktree_path.exists()
    trash_dirs = [
        path
        for path in module.context.worktrees_dir.iterdir()
        if path.name.startswith(worktree_removal.TRASH_DIR_PREFIX)
    ]
    assert len(trash_dirs) == 1
    assert "leftover:" in captured.out
    assert "status: deleted" in captured.out


@pytest.mark.parametrize("remaining_file", [None, "held-by-editor.node"])
def test_cmd_delete_hands_only_a_held_empty_directory_to_the_background_remover(
    tmp_path, monkeypatch, capsys, remaining_file
):
    """The shell that ran the delete holds its start directory until the command ends."""
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    name = "held-by-shell"
    worktree_path = module.context.worktrees_dir / name
    _make_checkout(worktree_path)
    (worktree_path / worktree_records.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(module.context.data_root / name), "managed_branch": True}),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, f"error: failed to delete '{worktree_path}': Permission denied"
        return 0, ""

    def empty_but_keep_directory(path):
        for child in path.iterdir():
            child.unlink()
        if remaining_file:
            (path / remaining_file).write_text("still held", encoding="utf-8")
        return "the directory is in use"

    handed_over = []
    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.removal, "_worktree_registration_state", lambda _path: False)
    monkeypatch.setattr(worktree_records, "_list_uncommitted_paths", lambda _path: [])
    monkeypatch.setattr(module.removal, "_terminate_worktree_processes", lambda _path: [])
    monkeypatch.setattr(module.removal, "remove_directory_tree", empty_but_keep_directory)
    monkeypatch.setattr(module.removal, "_move_to_trash", lambda _path: None)
    monkeypatch.setattr(
        module.removal, "_remove_when_released", lambda n: handed_over.append(n) or True
    )

    result = module.removal.delete(argparse.Namespace(name=name, force=False))

    captured = capsys.readouterr()
    if remaining_file:
        assert result == 1
        assert handed_over == []
        assert ["git", "branch", "-d", name] not in commands
        assert (worktree_path / remaining_file).read_text(encoding="utf-8") == "still held"
        return
    assert result == 0
    assert handed_over == [name]
    assert ["git", "branch", "-d", name] in commands
    assert "status: deleted" in captured.out
    assert "background process removes it" in captured.out


@pytest.mark.parametrize("new_file", [".git", "new-work.txt"])
def test_cmd_remove_released_waits_for_release_and_preserves_new_contents(
    tmp_path, monkeypatch, new_file
):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")
    monkeypatch.setattr(worktree_removal.time, "sleep", lambda _seconds: None)

    released = module.context.worktrees_dir / "released"
    released.mkdir(parents=True)
    attempts = []
    rmdir = Path.rmdir

    def held_twice(path):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("the directory is in use")
        rmdir(path)

    monkeypatch.setattr(worktree_removal.Path, "rmdir", held_twice)

    assert module.removal.remove_released(argparse.Namespace(name="released")) == 0
    assert not released.exists()
    assert len(attempts) == 3

    recreated = module.context.worktrees_dir / "recreated"
    recreated.mkdir()
    (recreated / new_file).write_text("new content", encoding="utf-8")

    assert module.removal.remove_released(argparse.Namespace(name="recreated")) == 1
    assert (recreated / new_file).read_text(encoding="utf-8") == "new content"
    assert len(attempts) == 3


def test_cmd_delete_lists_uncommitted_files_when_non_force_remove_fails(
    tmp_path, monkeypatch, capsys
):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    name = "dirty-worktree"
    worktree_path = module.context.worktrees_dir / name
    _make_checkout(worktree_path)

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, f"fatal: '{worktree_path}' contains modified or untracked files"
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.removal, "_worktree_registration_state", lambda _path: True)
    monkeypatch.setattr(
        worktree_records,
        "_list_uncommitted_paths",
        lambda _path: ["?? docs/plans/task.md", " M webui/src/App.svelte"],
    )
    monkeypatch.setattr(worktree_context.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.removal.delete(argparse.Namespace(name=name, force=False))

    captured = capsys.readouterr()
    assert result == 1
    assert "uncommitted: ?? docs/plans/task.md" in captured.out
    assert "uncommitted:  M webui/src/App.svelte" in captured.out


def test_cmd_delete_restores_marker_after_failed_remove_for_retry(tmp_path, monkeypatch):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    name = "managed-retry"
    worktree_path = module.context.worktrees_dir / name
    _make_checkout(worktree_path)

    marker_path = worktree_path / worktree_records.WORKTREE_FILE_NAME
    marker_path.write_text(
        json.dumps({"data_dir": str(module.context.data_root / name), "managed_branch": True}),
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

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.removal, "_worktree_registration_state", lambda _path: True)
    monkeypatch.setattr(worktree_context.shutil, "rmtree", lambda *_args, **_kwargs: None)

    first_result = module.removal.delete(argparse.Namespace(name=name, force=False))

    assert first_result == 1
    assert marker_path.exists()

    second_result = module.removal.delete(argparse.Namespace(name=name, force=False))

    assert second_result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", worktree_records.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "-C", str(worktree_path), "clean", "-f", "--", worktree_records.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "branch", "-d", name],
    ]


def test_cmd_delete_non_force_fails_closed_for_localized_remove_error(
    tmp_path, monkeypatch, capsys
):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    name = "localized-error"
    worktree_path = module.context.worktrees_dir / name
    _make_checkout(worktree_path)
    protected = worktree_path / "important.txt"
    protected.write_text("keep", encoding="utf-8")

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, "Fehler: Arbeitsverzeichnis enthalt nicht gespeicherte Anderungen"
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.removal, "_worktree_registration_state", lambda _path: True)
    monkeypatch.setattr(module.removal, "_terminate_worktree_processes", pytest.fail)

    result = module.removal.delete(argparse.Namespace(name=name, force=False))

    assert result == 1
    assert protected.read_text(encoding="utf-8") == "keep"
    assert "Fehler:" in capsys.readouterr().out


def test_cmd_delete_non_force_finishes_only_after_verified_deregistration(tmp_path, monkeypatch):
    module = _worktree_manager()
    monkeypatch.setattr(module.context, "worktrees_dir", tmp_path / ".worktrees")

    name = "deregistered"
    worktree_path = module.context.worktrees_dir / name
    _make_checkout(worktree_path)
    (worktree_path / "leftover.txt").write_text("x", encoding="utf-8")

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "remove"]:
            return 1, "could not delete locked files"
        return 0, ""

    monkeypatch.setattr(module.context, "run_command", fake_run_command)
    monkeypatch.setattr(worktree_records, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.removal, "_worktree_registration_state", lambda _path: False)

    result = module.removal.delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert not worktree_path.exists()


def _strip_to_marker_only(module, worktree_path: Path) -> None:
    """Leave only the marker, as a merge whose directory removal failed does."""
    for entry in worktree_path.iterdir():
        if entry.name == worktree_records.WORKTREE_FILE_NAME:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _seed_leftover_data_dir(repo: Path, name: str, port: int) -> Path:
    data_dir = repo.parent / "data" / name
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(json.dumps({"server_port": port}), encoding="utf-8")
    return data_dir


def _intercept_service_stop(module, monkeypatch) -> list[tuple[list[str], Path | None]]:
    """Record `test-env.py stop` calls; every other command runs for real."""
    stop_calls: list[tuple[list[str], Path | None]] = []
    real_run_command = module.context.run_command

    def run_command(command, *, cwd=None):
        if command[1:2] and command[1].endswith("test-env.py"):
            stop_calls.append((command, cwd))
            return 0, ""
        return real_run_command(command, cwd=cwd)

    monkeypatch.setattr(module.context, "run_command", run_command)
    monkeypatch.setattr(module.removal, "_terminate_worktree_processes", lambda _path: [])
    return stop_calls


def _branch_exists(repo: Path, name: str) -> bool:
    return bool(_git_output(repo, "branch", "--list", name))


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("working_files", [False, True])
@pytest.mark.parametrize("registration", ["present", "pruned", "branch-removed"])
def test_cmd_delete_finishes_marker_only_leftover(
    real_repo, monkeypatch, capsys, force, working_files, registration
):
    module = _worktree_manager()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "leftover"
    worktree_path = _create_task_worktree(module, real_repo, name)
    _commit_file(worktree_path, "task.txt", "done\n", "task work")
    _git(real_repo, "merge", "--no-ff", name, "-m", f"merge: {name}")
    _strip_to_marker_only(module, worktree_path)
    if registration != "present":
        _git(real_repo, "worktree", "prune")
    if registration == "branch-removed":
        _git(real_repo, "branch", "-d", name)
    data_dir = _seed_leftover_data_dir(real_repo, name, 8433)
    _record_owned_data(module, worktree_path, data_dir)
    stop_calls = _intercept_service_stop(module, monkeypatch)
    sentinel = worktree_path / "unsaved.txt"
    if working_files:
        sentinel.write_text("keep my work", encoding="utf-8")
    # The enclosing repository is on main; the leftover must not resolve to it.
    assert worktree_records._read_worktree_branch_name(worktree_path) is None

    result = module.removal.delete(argparse.Namespace(name=name, force=force))
    if working_files and not force:
        assert result == 1
        assert sentinel.read_text(encoding="utf-8") == "keep my work"
        assert data_dir.is_dir()
        assert _branch_exists(real_repo, name) == (registration != "branch-removed")
        assert stop_calls == []
        return
    assert result == 0

    assert "status: deleted" in capsys.readouterr().out
    assert not worktree_path.exists()
    assert not data_dir.exists()
    assert _git_output(real_repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not _branch_exists(real_repo, name)
    checkout_root = module.context.checkout_root
    assert stop_calls == [
        (
            [
                worktree_removal.sys.executable,
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
@pytest.mark.parametrize("pruned", [False, True])
def test_cmd_delete_leftover_deletes_unmerged_branch_only_with_force(
    real_repo, monkeypatch, capsys, force, pruned
):
    module = _worktree_manager()
    _patch_repo_globals(monkeypatch, module, real_repo)
    name = "unmerged-leftover"
    worktree_path = _create_task_worktree(module, real_repo, name)
    _commit_file(worktree_path, "task.txt", "unmerged\n", "unmerged work")
    _strip_to_marker_only(module, worktree_path)
    if pruned:
        _git(real_repo, "worktree", "prune")
    _intercept_service_stop(module, monkeypatch)

    result = module.removal.delete(argparse.Namespace(name=name, force=force))

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
    module = _worktree_manager()
    worktree_path = _create_task_worktree(module, real_repo, "registered")
    unregistered = real_repo / ".worktrees" / "unregistered"
    unregistered.mkdir()

    assert worktree_records._read_registered_branch_name(real_repo, worktree_path) == "registered"
    assert worktree_records._read_registered_branch_name(real_repo, unregistered) is None
    assert worktree_records._read_worktree_branch_name(unregistered) is None
