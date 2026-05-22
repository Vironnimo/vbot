import argparse
import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "worktree.py"


def _load_worktree_module():
    spec = importlib.util.spec_from_file_location("worktree", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_used_ports_tolerates_non_object_marker_and_settings_json(tmp_path):
    module = _load_worktree_module()
    worktrees_dir = tmp_path / ".worktrees"
    worktrees_dir.mkdir(parents=True)

    non_object_marker_worktree = worktrees_dir / "non-object-marker"
    non_object_marker_worktree.mkdir()
    (non_object_marker_worktree / module.WORKTREE_FILE_NAME).write_text(
        json.dumps(["not", "an", "object"]),
        encoding="utf-8",
    )

    non_object_settings_data_dir = tmp_path / "non-object-settings"
    non_object_settings_data_dir.mkdir()
    (non_object_settings_data_dir / "settings.json").write_text(
        json.dumps(["not", "an", "object"]),
        encoding="utf-8",
    )
    non_object_settings_worktree = worktrees_dir / "non-object-settings-wt"
    non_object_settings_worktree.mkdir()
    (non_object_settings_worktree / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(non_object_settings_data_dir)}),
        encoding="utf-8",
    )

    valid_data_dir = tmp_path / "valid-data"
    valid_data_dir.mkdir()
    (valid_data_dir / "settings.json").write_text(
        json.dumps({"server_port": 8455}),
        encoding="utf-8",
    )
    valid_worktree = worktrees_dir / "valid"
    valid_worktree.mkdir()
    (valid_worktree / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(valid_data_dir)}),
        encoding="utf-8",
    )

    ports = module.scan_used_ports(worktrees_dir)

    assert ports == {8455}


def test_cmd_remove_uses_expected_data_dir_when_marker_is_tampered(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    name = "safe-remove"
    worktree_path = module.WORKTREES_DIR / name
    worktree_path.mkdir(parents=True)

    (worktree_path / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(tmp_path / "malicious-target")}),
        encoding="utf-8",
    )

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    removed_paths = []

    def fake_rmtree(path, ignore_errors):
        removed_paths.append((Path(path), ignore_errors))

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda _path: name)
    monkeypatch.setattr(module.shutil, "rmtree", fake_rmtree)

    result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert removed_paths == [(Path.home() / f".vbot-{name}", True)]
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_remove_missing_marker_same_name_branch_skips_branch_delete(tmp_path, monkeypatch):
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

    result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [["git", "worktree", "remove", str(worktree_path)]]


def test_cmd_remove_tolerates_non_object_marker_and_skips_branch_delete(tmp_path, monkeypatch):
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

    result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_remove_malformed_marker_same_name_branch_skips_branch_delete(tmp_path, monkeypatch):
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

    result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_remove_force_skips_marker_cleanup(tmp_path, monkeypatch):
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

    result = module.cmd_remove(argparse.Namespace(name=name, force=True))

    assert result == 0
    assert commands == [["git", "worktree", "remove", "--force", str(worktree_path)]]


def test_cmd_remove_skips_branch_delete_when_marker_declares_unmanaged_branch(
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

    result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


def test_cmd_remove_deletes_branch_when_marker_declares_managed_branch(tmp_path, monkeypatch):
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

    result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "branch", "-d", name],
    ]


def test_cmd_remove_restores_marker_after_failed_remove_for_retry(tmp_path, monkeypatch):
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
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    first_result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert first_result == 1
    assert marker_path.exists()

    second_result = module.cmd_remove(argparse.Namespace(name=name, force=False))

    assert second_result == 0
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
        ["git", "branch", "-d", name],
    ]
