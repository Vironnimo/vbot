"""Tests for update management desktop."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.update_management as update_management
from cli.update_management import (
    CommandRun,
    _running_process_id,
    run_update,
)
from tests.cli.update_management_test_support import (
    ScriptedRunner,
    _instance,
    _ok,
    _recording_restart,
    _write_state,
)


def test_desktop_client_update_keeps_exact_shape_and_never_starts_server(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    _write_state(
        tmp_path,
        revision="old",
        shape="desktop-client",
        groups=("cli", "desktop"),
    )
    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "pull"]:
            (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8")
        return _ok()

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        platform_name="posix",
    )

    assert result.ok, result.message
    assert runner.ran("-m", "pip", "install", "-e", ".[cli,desktop]")
    assert not any("npm" in call for call in runner.calls)
    assert events == []


def test_windows_update_refuses_running_owned_desktop_before_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    scripts_dir = tmp_path / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    python_executable = scripts_dir / "python.exe"
    python_executable.write_bytes(b"")
    desktop_launcher = scripts_dir / "vbot-desktop.exe"
    desktop_launcher.write_bytes(b"")
    _write_state(
        tmp_path,
        shape="server-desktop",
        python_executable=str(python_executable),
    )
    manifest = tmp_path / ".vbot-install.json"
    manifest_before = manifest.read_bytes()

    package_launcher = scripts_dir / "vbot.exe"

    def running_process_id(executable: Path, *, include_current: bool = False) -> int | None:
        if executable == package_launcher.resolve():
            assert include_current
            return None
        assert executable == desktop_launcher.resolve()
        assert not include_current
        return 4242

    monkeypatch.setattr(update_management, "_running_process_id", running_process_id)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("samesha")
        raise AssertionError(f"update mutated state after Desktop preflight failed: {command}")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        platform_name="nt",
    )

    assert not result.ok
    assert "process 4242" in result.message
    expected_recovery = (
        f"Set-Location -LiteralPath '{tmp_path.resolve()}'; "
        f"& '{python_executable}' -m cli.main update"
    )
    assert f"resume update: {expected_recovery}" in result.message
    assert manifest.read_bytes() == manifest_before
    assert not runner.ran("git", "status")
    assert not runner.ran("git", "pull")
    assert not runner.ran("pip")
    assert events == []


def test_windows_update_rechecks_desktop_immediately_before_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    scripts_dir = tmp_path / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    python_executable = scripts_dir / "python.exe"
    python_executable.write_bytes(b"")
    desktop_launcher = scripts_dir / "vbot-desktop.exe"
    desktop_launcher.write_bytes(b"")
    _write_state(
        tmp_path,
        revision="old",
        shape="server-desktop",
        python_executable=str(python_executable),
        webui_revision="old",
    )
    manifest = tmp_path / ".vbot-install.json"
    manifest_before = manifest.read_bytes()
    process_ids = iter([None, 4242])

    package_launcher = scripts_dir / "vbot.exe"

    def running_process_id(executable: Path, *, include_current: bool = False) -> int | None:
        if executable == package_launcher.resolve():
            assert include_current
            return None
        assert executable == desktop_launcher.resolve()
        assert not include_current
        return next(process_ids)

    monkeypatch.setattr(update_management, "_running_process_id", running_process_id)
    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:2] == ["git", "pull"]:
            (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8")
            return _ok("")
        raise AssertionError(f"dependency step continued while Desktop was running: {command}")

    runner = ScriptedRunner(handler)
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        restart=False,
        platform_name="nt",
    )

    assert not result.ok
    assert runner.ran("git", "pull")
    assert not runner.ran("pip")
    assert manifest.read_bytes() == manifest_before


def test_windows_update_refuses_active_package_launcher_before_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    scripts_dir = tmp_path / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    python_executable = scripts_dir / "python.exe"
    python_executable.write_bytes(b"")
    package_launcher = scripts_dir / "vbot.exe"
    package_launcher.write_bytes(b"")
    _write_state(tmp_path, python_executable=str(python_executable))
    manifest = tmp_path / ".vbot-install.json"
    manifest_before = manifest.read_bytes()

    def running_process_id(executable: Path, *, include_current: bool = False) -> int | None:
        assert executable == package_launcher.resolve()
        assert include_current
        return 31337

    monkeypatch.setattr(update_management, "_running_process_id", running_process_id)

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("samesha")
        raise AssertionError(f"update mutated state after launcher preflight failed: {command}")

    runner = ScriptedRunner(handler)
    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        restart=False,
        platform_name="nt",
    )

    assert not result.ok
    assert "process 31337" in result.message
    expected_recovery = (
        f"Set-Location -LiteralPath '{tmp_path.resolve()}'; "
        f"& '{python_executable}' -m cli.main update"
    )
    assert f"resume update: {expected_recovery}" in result.message
    assert manifest.read_bytes() == manifest_before
    assert not runner.ran("git", "status")
    assert not runner.ran("git", "pull")
    assert not runner.ran("pip")


def test_windows_update_migrates_installer_command_shim_to_python_module(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("same", encoding="utf-8")
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    scripts_dir = tmp_path / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    python_executable = scripts_dir / "python.exe"
    python_executable.write_bytes(b"")
    shim = tmp_path / "bin" / "vbot.cmd"
    shim.parent.mkdir()
    shim.write_bytes(b'@echo off\r\n"old\\vbot.exe" %*\r\n')
    _write_state(
        tmp_path,
        python_executable=str(python_executable),
        webui_revision="samesha",
    )

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok("samesha")
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "pull"]:
            return _ok("")
        raise AssertionError(f"unexpected command: {command}")

    result = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        restart=False,
        platform_name="nt",
    )

    assert result.ok, result.message
    assert shim.read_bytes() == (f'@echo off\r\n"{python_executable}" -m cli.main %*\r\n'.encode())

    repeated = run_update(
        _instance(),
        runner=ScriptedRunner(handler),
        root=tmp_path,
        restart=False,
        platform_name="nt",
    )

    assert repeated.ok, repeated.message


def test_running_desktop_lookup_matches_only_exact_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "owned" / "Scripts" / "vbot-desktop.exe"
    other_launcher = tmp_path / "other" / "Scripts" / "vbot-desktop.exe"

    class FakeProcess:
        def __init__(self, process_id: int, executable: Path) -> None:
            self.info = {"pid": process_id, "exe": str(executable)}

    processes = [
        FakeProcess(1001, other_launcher),
        FakeProcess(1002, launcher),
    ]
    monkeypatch.setattr(
        update_management.psutil,
        "process_iter",
        lambda _attributes: iter(processes),
    )

    assert _running_process_id(launcher) == 1002
    assert _running_process_id(tmp_path / "missing" / "vbot-desktop.exe") is None


def test_windows_desktop_update_refreshes_shortcut_to_gui_launcher(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("before", encoding="utf-8")
    setup_script = tmp_path / "scripts" / "setup.ps1"
    setup_script.parent.mkdir()
    setup_script.write_text("# shortcut mode", encoding="utf-8")
    dist = tmp_path / "webui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    scripts_dir = tmp_path / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    python_executable = scripts_dir / "python.exe"
    python_executable.write_bytes(b"")
    desktop_launcher = scripts_dir / "vbot-desktop.exe"
    desktop_launcher.write_bytes(b"")
    _write_state(
        tmp_path,
        revision="old",
        shape="server-desktop",
        python_executable=str(python_executable),
        webui_revision="old",
    )
    revisions = iter(["old", "new"])

    def handler(command: list[str]) -> CommandRun:
        if command[:2] == ["git", "symbolic-ref"]:
            return _ok("main")
        if command[:2] == ["git", "rev-parse"]:
            return _ok(next(revisions))
        if command[:2] == ["git", "status"]:
            return _ok("")
        if command[:2] == ["git", "pull"]:
            (tmp_path / "pyproject.toml").write_text("after", encoding="utf-8")
        return _ok("")

    runner = ScriptedRunner(handler)
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(),
        runner=runner,
        root=tmp_path,
        stop=stop,
        start=start,
        platform_name="nt",
    )

    assert result.ok, result.message
    assert any(
        call[:7]
        == [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ]
        and call[-2:] == ["-DesktopShortcutTarget", str(desktop_launcher.resolve())]
        for call in runner.calls
    )
    assert events == ["stop", "start"]
