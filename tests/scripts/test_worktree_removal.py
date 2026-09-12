"""worktree removal coverage."""

from __future__ import annotations

import argparse
import json

import pytest

from tests.scripts.worktree_helpers import _load_worktree_module


def test_parse_args_accepts_create_delete_and_list():
    module = _load_worktree_module()

    assert module.parse_args(["create", "task"]).command == "create"
    assert module.parse_args(["delete", "task"]).command == "delete"
    assert module.parse_args(["list"]).command == "list"


def test_cmd_delete_rejects_unsafe_name(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    result = module.cmd_delete(argparse.Namespace(name="nested/task", force=False))

    assert result == 1
    assert commands == []


def test_cmd_delete_uses_expected_data_dir_when_marker_is_tampered(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    name = "safe-delete"
    worktree_path = module.WORKTREES_DIR / name
    worktree_path.mkdir(parents=True)
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
    assert not expected_data_dir.exists()
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
    (worktree_path / "scripts").mkdir(parents=True)
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
    (worktree_path / "scripts").mkdir(parents=True)
    (worktree_path / "scripts" / "test-env.py").write_text("", encoding="utf-8")
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text("{}", encoding="utf-8")
    calls = []

    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)

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
    worktree_path.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module, "_run_command", lambda _command, *, cwd=None: (0, ""))
    monkeypatch.setattr(module, "_remove_directory_tree", lambda _path: "locked")

    assert module.cmd_delete(argparse.Namespace(name=name, force=True)) == 1
    assert data_dir.exists()


def test_cmd_delete_missing_marker_same_name_branch_skips_branch_delete(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "missing-marker"
    worktree_path = module.WORKTREES_DIR / name
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)
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
    worktree_path.mkdir(parents=True)
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
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)

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
    worktree_path.mkdir(parents=True)
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
    worktree_path.mkdir(parents=True)
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
