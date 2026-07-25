import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from core.storage.layout import DATA_DIRECTORY_RELATIVE_PATHS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "worktree.py"


def _load_worktree_module():
    spec = importlib.util.spec_from_file_location("worktree", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worktree_source_uses_canonical_initializer_without_local_template() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert "initialize_data_directory" in source
    assert ".data-dir-base" not in source
    assert "OPENAI_API_KEY" not in source


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


def test_find_free_port_starts_after_main_dev_port(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "scan_used_ports", lambda _worktrees_dir: set())
    monkeypatch.setattr(module, "is_port_bound", lambda _port: False)

    assert module.MAIN_DEV_PORT == 8421
    assert module.FIRST_WORKTREE_PORT == 8422
    assert module.find_free_port(tmp_path) == 8422
    assert module.find_free_port(tmp_path, start=8421) == 8422


def test_run_command_defaults_to_project_root(monkeypatch):
    module = _load_worktree_module()
    calls = []

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(command, *, capture_output, text, cwd, check):
        calls.append(
            {
                "command": command,
                "capture_output": capture_output,
                "text": text,
                "cwd": cwd,
                "check": check,
            }
        )
        return FakeResult()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._run_command(["git", "status"])

    assert result == (0, "")
    assert calls == [
        {
            "command": ["git", "status"],
            "capture_output": True,
            "text": True,
            "cwd": module.PROJECT_ROOT,
            "check": False,
        }
    ]


def test_cmd_create_rejects_unsafe_name(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    result = module.cmd_create(argparse.Namespace(name="../outside", from_branch=None))

    assert result == 1
    assert commands == []


def test_cmd_create_runs_npm_install_then_build(tmp_path, monkeypatch):
    module = _load_worktree_module()

    name = "fresh-worktree"
    worktrees_dir = tmp_path / ".worktrees"
    worktree_path = worktrees_dir / name
    webui_path = worktree_path / "webui"

    monkeypatch.setattr(module, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(module, "find_free_port", lambda _worktrees_dir: 8422)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "npm")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    commands: list[tuple[list[str], Path | None]] = []

    def fake_run_command(command, *, cwd=None):
        commands.append((command, cwd))
        if command[:3] == ["git", "worktree", "add"]:
            webui_path.mkdir(parents=True, exist_ok=True)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    result = module.cmd_create(argparse.Namespace(name=name, from_branch="main"))

    assert result == 0
    assert commands[-2:] == [
        (["npm", "install"], webui_path),
        (["npm", "run", "build"], webui_path),
    ]
    assert not (worktree_path / ".vorch" / "WORKTREE.md").exists()


@pytest.mark.parametrize(
    ("from_branch", "expected_branch"),
    [(None, "fresh-worktree"), ("main", "main")],
)
def test_cmd_create_reports_branch_in_output(
    tmp_path, monkeypatch, capsys, from_branch, expected_branch
):
    module = _load_worktree_module()

    name = "fresh-worktree"
    worktrees_dir = tmp_path / ".worktrees"
    worktree_path = worktrees_dir / name
    webui_path = worktree_path / "webui"

    monkeypatch.setattr(module, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(module, "find_free_port", lambda _worktrees_dir: 8422)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "npm")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return 1, ""
        if command[:3] == ["git", "worktree", "add"]:
            webui_path.mkdir(parents=True, exist_ok=True)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    result = module.cmd_create(argparse.Namespace(name=name, from_branch=from_branch))

    assert result == 0
    assert f"branch: {expected_branch}" in capsys.readouterr().out


def test_cmd_create_initializes_canonical_data_dir_without_agent(tmp_path, monkeypatch):
    module = _load_worktree_module()

    name = "seeded-worktree"
    worktrees_dir = tmp_path / ".worktrees"
    worktree_path = worktrees_dir / name
    webui_path = worktree_path / "webui"
    data_dir = tmp_path / "home" / f".vbot-{name}"

    monkeypatch.setattr(module, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(module, "find_free_port", lambda _worktrees_dir: 8422)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "npm")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "add"]:
            webui_path.mkdir(parents=True, exist_ok=True)
        return 0, ""

    monkeypatch.setattr(module, "_run_command", fake_run_command)

    result = module.cmd_create(argparse.Namespace(name=name, from_branch="main"))

    assert result == 0
    assert (data_dir / ".env").read_bytes() == (
        PROJECT_ROOT / "resources" / "data-dir" / ".env.example"
    ).read_bytes()
    expected_settings = json.loads(
        (PROJECT_ROOT / "tests" / "e2e" / "fake-provider-settings.json").read_text(encoding="utf-8")
    )
    expected_settings["providers"]["custom"]["fake"]["base_url"] = "http://127.0.0.1:18422/v1"
    expected_settings["server_port"] = 8422
    assert json.loads((data_dir / "settings.json").read_text(encoding="utf-8")) == expected_settings
    assert all((data_dir / path).is_dir() for path in DATA_DIRECTORY_RELATIVE_PATHS)
    assert not (data_dir / "agents" / "main").exists()


def test_find_free_port_skips_server_when_paired_provider_port_is_bound(tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "scan_used_ports", lambda _worktrees_dir: set())
    monkeypatch.setattr(module, "is_port_bound", lambda port: port == 18_422)

    assert module.find_free_port(tmp_path) == 8423


def test_seed_worktree_settings_preserves_existing_user_values(tmp_path):
    module = _load_worktree_module()
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "defaults": {"agent": {"model": "existing/model::connection"}},
                "providers": {"custom": {"private": {"name": "Private"}}},
            }
        ),
        encoding="utf-8",
    )

    module.seed_worktree_settings(settings_path, server_port=8422)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["server_port"] == 8422
    assert settings["defaults"]["agent"]["model"] == "existing/model::connection"
    assert settings["defaults"]["agent"]["fallback_model"] == "fake/e2e-fallback::default"
    assert settings["providers"]["custom"]["private"] == {"name": "Private"}
    assert settings["providers"]["custom"]["fake"]["base_url"] == ("http://127.0.0.1:18422/v1")


def test_cmd_create_cleans_up_worktree_data_dir_and_branch_after_build_failure(
    tmp_path, monkeypatch
):
    module = _load_worktree_module()

    name = "failing-worktree"
    worktrees_dir = tmp_path / ".worktrees"
    worktree_path = worktrees_dir / name
    webui_path = worktree_path / "webui"
    data_dir = tmp_path / "home" / f".vbot-{name}"

    monkeypatch.setattr(module, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(module, "find_free_port", lambda _worktrees_dir: 8422)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "npm")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append((command, cwd))
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return 1, ""
        if command[:3] == ["git", "worktree", "add"]:
            webui_path.mkdir(parents=True, exist_ok=True)
            return 0, ""
        if command == ["npm", "run", "build"]:
            return 1, "build failed"
        return 0, ""

    removed_paths = []

    def fake_rmtree(path, ignore_errors):
        removed_paths.append((Path(path), ignore_errors))

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module.shutil, "rmtree", fake_rmtree)

    result = module.cmd_create(argparse.Namespace(name=name, from_branch=None))

    assert result == 1
    assert (["git", "worktree", "remove", "--force", str(worktree_path)], None) in commands
    assert (["git", "branch", "-D", name], None) in commands
    assert removed_paths == [(data_dir, True)]


def test_cmd_create_preserves_preexisting_data_dir_after_build_failure(tmp_path, monkeypatch):
    module = _load_worktree_module()

    name = "preexisting-data"
    worktrees_dir = tmp_path / ".worktrees"
    worktree_path = worktrees_dir / name
    webui_path = worktree_path / "webui"
    data_dir = tmp_path / "home" / f".vbot-{name}"
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(module, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(module, "find_free_port", lambda _worktrees_dir: 8422)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "npm")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    commands = []

    def fake_run_command(command, *, cwd=None):
        commands.append((command, cwd))
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return 1, ""
        if command[:3] == ["git", "worktree", "add"]:
            webui_path.mkdir(parents=True, exist_ok=True)
            return 0, ""
        if command == ["npm", "run", "build"]:
            return 1, "build failed"
        return 0, ""

    removed_paths = []

    def fake_rmtree(path, ignore_errors):
        removed_paths.append((Path(path), ignore_errors))

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module.shutil, "rmtree", fake_rmtree)

    result = module.cmd_create(argparse.Namespace(name=name, from_branch=None))

    assert result == 1
    assert (["git", "worktree", "remove", "--force", str(worktree_path)], None) in commands
    assert removed_paths == []


def test_iter_worktree_entries_lists_marker_backed_worktrees(tmp_path, monkeypatch):
    module = _load_worktree_module()
    worktrees_dir = tmp_path / ".worktrees"
    monkeypatch.setattr(module, "_read_worktree_branch_name", lambda path: f"{path.name}-branch")

    first_data_dir = tmp_path / "home" / ".vbot-alpha"
    first_data_dir.mkdir(parents=True)
    (first_data_dir / "settings.json").write_text(
        json.dumps({"server_port": 8421}),
        encoding="utf-8",
    )
    first_worktree = worktrees_dir / "alpha"
    first_worktree.mkdir(parents=True)
    (first_worktree / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(first_data_dir), "managed_branch": True}),
        encoding="utf-8",
    )

    second_worktree = worktrees_dir / "beta"
    second_worktree.mkdir()
    (second_worktree / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(tmp_path / "missing"), "managed_branch": False}),
        encoding="utf-8",
    )

    ignored_worktree = worktrees_dir / "no-marker"
    ignored_worktree.mkdir()

    trash_worktree = worktrees_dir / f"{module.TRASH_DIR_PREFIX}gone-123"
    trash_worktree.mkdir()
    (trash_worktree / module.WORKTREE_FILE_NAME).write_text(
        json.dumps({"data_dir": str(tmp_path / "gone"), "managed_branch": True}),
        encoding="utf-8",
    )

    entries = module.iter_worktree_entries(worktrees_dir)

    assert entries == [
        {
            "name": "alpha",
            "path": first_worktree,
            "branch": "alpha-branch",
            "data-dir": str(first_data_dir),
            "port": 8421,
            "managed-branch": "true",
        },
        {
            "name": "beta",
            "path": second_worktree,
            "branch": "beta-branch",
            "data-dir": str(tmp_path / "missing"),
            "port": "unknown",
            "managed-branch": "false",
        },
    ]


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

    name = "safe-delete"
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

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    assert result == 0
    assert removed_paths == [(Path.home() / f".vbot-{name}", True)]
    assert commands == [
        ["git", "-C", str(worktree_path), "clean", "-f", "--", module.WORKTREE_FILE_NAME],
        ["git", "worktree", "remove", str(worktree_path)],
    ]


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


def test_list_uncommitted_paths_returns_porcelain_lines(monkeypatch):
    module = _load_worktree_module()

    class FakeResult:
        returncode = 0
        stdout = " M webui/src/App.svelte\n?? docs/plans/task.md\n\n"

    calls = []

    def fake_run(command, *, capture_output, text, check):
        calls.append(command)
        return FakeResult()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    lines = module._list_uncommitted_paths(Path("C:/repo/.worktrees/task"))

    assert lines == [" M webui/src/App.svelte", "?? docs/plans/task.md"]
    assert calls == [["git", "-C", str(Path("C:/repo/.worktrees/task")), "status", "--porcelain"]]


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
    monkeypatch.setattr(
        module,
        "_list_uncommitted_paths",
        lambda _path: ["?? docs/plans/task.md", " M webui/src/App.svelte"],
    )
    monkeypatch.setattr(module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    result = module.cmd_delete(argparse.Namespace(name=name, force=False))

    captured = capsys.readouterr()
    assert result == 1
    assert "error: worktree has uncommitted changes, use --force to override" in captured.out
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
