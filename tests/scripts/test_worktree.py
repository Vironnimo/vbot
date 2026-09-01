import argparse
import importlib.util
import json
import subprocess
import threading
import time
from contextlib import contextmanager
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


def test_cmd_create_holds_port_lock_until_marker_and_settings_are_durable(tmp_path, monkeypatch):
    module = _load_worktree_module()
    name = "locked-allocation"
    worktrees_dir = tmp_path / ".worktrees"
    worktree_path = worktrees_dir / name
    webui_path = worktree_path / "webui"
    data_dir = tmp_path / "home" / f".vbot-{name}"
    observed = []

    monkeypatch.setattr(module, "WORKTREES_DIR", worktrees_dir)
    monkeypatch.setattr(module, "find_free_port", lambda _worktrees_dir: 8422)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "npm")
    monkeypatch.setattr(module.Path, "home", staticmethod(lambda: tmp_path / "home"))

    @contextmanager
    def recording_lock():
        observed.append("entered")
        yield
        assert (
            json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))["server_port"]
            == 8422
        )
        assert (worktree_path / module.WORKTREE_FILE_NAME).is_file()
        observed.append("released")

    def fake_run_command(command, *, cwd=None):
        if command[:3] == ["git", "worktree", "add"]:
            webui_path.mkdir(parents=True, exist_ok=True)
        return 0, ""

    monkeypatch.setattr(module, "_port_allocation_lock", recording_lock)
    monkeypatch.setattr(module, "_run_command", fake_run_command)

    assert module.cmd_create(argparse.Namespace(name=name, from_branch="main")) == 0
    assert observed == ["entered", "released"]


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
    assert settings["defaults"]["agent"]["fallback_models"] == ["fake/e2e-fallback::default"]
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


def test_parse_args_merge_and_repair_defaults():
    module = _load_worktree_module()

    merge_args = module.parse_args(["merge", "task"])
    assert merge_args.command == "merge"
    assert merge_args.name == "task"
    assert merge_args.message is None
    assert merge_args.wait_timeout == 1800
    assert module.DEFAULT_MERGE_WAIT_TIMEOUT_SECONDS == 1800

    repair_args = module.parse_args(["repair-start", "task"])
    assert repair_args.command == "repair-start"
    assert repair_args.window == 900
    assert module.DEFAULT_REPAIR_WINDOW_SECONDS == 900

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


def _list_porcelain(repo: Path) -> list[str]:
    output = _git_output(repo, "status", "--porcelain")
    return [line for line in output.splitlines() if line.strip()]


def test_merge_lock_blocks_second_merger_until_release(real_repo, monkeypatch):
    module = _load_worktree_module()
    _patch_repo_globals(monkeypatch, module, real_repo)
    _commit_file(real_repo, "shared.txt", "base\n", "base file")
    _create_task_worktree(module, real_repo, "task-a")
    worktree_b = _create_task_worktree(module, real_repo, "task-b")
    _commit_file(worktree_b, "feature-b.txt", "b\n", "b file")

    lock_path, _, _ = module._merge_lock_paths()
    handle = lock_path.open("a+b")
    assert module._acquire_file_lock(handle)

    blocked = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=1.5))
    captured_after_block = blocked

    module._release_file_lock(handle)
    handle.close()

    released = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))

    assert captured_after_block == 1
    assert released == 0
    assert (real_repo / "feature-b.txt").exists()


def test_keeper_hold_releases_on_signal(tmp_path):
    module = _load_worktree_module()
    lock_path = tmp_path / "vbot-merge.lock"
    holder_path = tmp_path / "vbot-merge.lock.holder.json"
    release_path = tmp_path / "vbot-merge.lock.release"

    keeper = threading.Thread(
        target=module.cmd_keeper_hold,
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
        if module._own_repair_window_is_active(holder_path, "task-a"):
            opened = True
            break
        time.sleep(0.05)
    assert opened

    release_path.write_text("release\n", encoding="utf-8")
    keeper.join(timeout=10)

    assert not keeper.is_alive()
    assert not holder_path.exists()
    assert module._probe_lock_is_busy(lock_path) is False


def test_keeper_hold_expires_at_deadline(tmp_path):
    module = _load_worktree_module()
    lock_path = tmp_path / "vbot-merge.lock"
    holder_path = tmp_path / "vbot-merge.lock.holder.json"
    release_path = tmp_path / "vbot-merge.lock.release"

    keeper = threading.Thread(
        target=module.cmd_keeper_hold,
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
    assert module._probe_lock_is_busy(lock_path) is False
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
        if not module._own_repair_window_is_active(holder_path, "task-a"):
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
    assert not module._own_repair_window_is_active(holder_path, "task-a")

    follow_up = module.cmd_merge(argparse.Namespace(name="task-b", message=None, wait_timeout=60))
    assert follow_up == 0


def test_repair_finish_without_window_reports_already_closed(capsys, tmp_path, monkeypatch):
    module = _load_worktree_module()
    monkeypatch.setattr(module, "WORKTREES_DIR", tmp_path / ".worktrees")

    result = module.cmd_repair_finish(argparse.Namespace(name="quiet-task"))
    captured = capsys.readouterr()

    assert result == 0
    assert "already-closed" in captured.out
