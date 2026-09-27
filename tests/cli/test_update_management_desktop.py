"""``vbot update`` for Desktop installs and Windows launchers, shims and shortcuts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import cli.update_management as update_management
from cli._update_types import UpdateResult
from cli.update_management import CommandRun, _running_process_id, run_update
from tests.cli.update_management_test_support import (
    ScriptedRunner,
    _instance,
    _recording_restart,
    _upstream,
    _write_state,
    checkout,
    only_reads,
    write_webui_build,
)


def _write_pyproject(root: Path, content: str) -> None:
    (root / "pyproject.toml").write_text(content, encoding="utf-8")


def _windows_environment(root: Path, *launchers: str) -> tuple[Path, Path]:
    """Create ``.venv/Scripts`` with ``python.exe`` and the named launchers."""

    scripts_dir = root / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    for name in ("python.exe", *launchers):
        (scripts_dir / name).write_bytes(b"")
    return scripts_dir, scripts_dir / "python.exe"


def _no_running_launchers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_management, "_running_process_id", lambda *_args, **_kw: None)


def test_desktop_client_update_keeps_exact_shape_and_never_starts_server(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write_pyproject(tmp_path, "before")
    _write_state(tmp_path, revision="old", shape="desktop-client", groups=("cli", "desktop"))
    runner = ScriptedRunner(
        checkout(
            heads=["old", "new"],
            upstream=_upstream(behind=1),
            on_merge=lambda: _write_pyproject(tmp_path, "after"),
        )
    )
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(), runner=runner, root=tmp_path, stop=stop, start=start, platform_name="posix"
    )

    assert result.ok, result.message
    assert runner.ran("-m", "pip", "install", "-e", ".[cli,desktop]")
    assert not any("npm" in call for call in runner.calls)
    assert events == []


@pytest.mark.parametrize(
    ("launcher", "shape", "process_id"),
    [
        pytest.param("vbot-desktop.exe", "server-desktop", 4242, id="desktop"),
        pytest.param("vbot.exe", "server", 31337, id="package-launcher"),
    ],
)
def test_windows_update_refuses_a_running_launcher_before_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: str, shape: str, process_id: int
) -> None:
    (tmp_path / ".git").mkdir()
    scripts_dir, python_executable = _windows_environment(tmp_path, launcher)
    _write_state(tmp_path, shape=shape, python_executable=str(python_executable))
    manifest = tmp_path / ".vbot-install.json"
    manifest_before = manifest.read_bytes()
    running = (scripts_dir / launcher).resolve()
    lookups: list[tuple[Path, bool]] = []

    def running_process_id(executable: Path, *, include_current: bool = False) -> int | None:
        lookups.append((executable, include_current))
        return process_id if executable == running else None

    monkeypatch.setattr(update_management, "_running_process_id", running_process_id)
    runner = ScriptedRunner(checkout(answer=only_reads("symbolic-ref", "rev-parse")))
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(), runner=runner, root=tmp_path, stop=stop, start=start, platform_name="nt"
    )

    assert not result.ok
    assert f"process {process_id}" in result.message
    expected_recovery = (
        f"Set-Location -LiteralPath '{tmp_path.resolve()}'; "
        f"& '{python_executable}' -m cli.main update"
    )
    assert f"resume update: {expected_recovery}" in result.message
    assert manifest.read_bytes() == manifest_before
    assert events == []
    # The package launcher may be this very process; the Desktop launcher never is.
    package_lookup = ((scripts_dir / "vbot.exe").resolve(), True)
    desktop_lookup = ((scripts_dir / "vbot-desktop.exe").resolve(), False)
    assert lookups == ([package_lookup, desktop_lookup] if shape != "server" else [package_lookup])


def test_windows_update_rechecks_desktop_immediately_before_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    _write_pyproject(tmp_path, "before")
    scripts_dir, python_executable = _windows_environment(tmp_path, "vbot-desktop.exe")
    _write_state(
        tmp_path,
        revision="old",
        shape="server-desktop",
        python_executable=str(python_executable),
        webui_revision="old",
    )
    manifest = tmp_path / ".vbot-install.json"
    manifest_before = manifest.read_bytes()
    desktop_ids = iter([None, 4242])

    def running_process_id(executable: Path, *, include_current: bool = False) -> int | None:
        if executable == (scripts_dir / "vbot.exe").resolve():
            return None
        return next(desktop_ids)

    monkeypatch.setattr(update_management, "_running_process_id", running_process_id)
    runner = ScriptedRunner(
        checkout(
            heads=["old", "new"],
            upstream=_upstream(behind=1),
            on_merge=lambda: _write_pyproject(tmp_path, "after"),
            answer=only_reads("symbolic-ref", "status", "rev-parse", "fetch", "rev-list", "merge"),
        )
    )

    result = run_update(
        _instance(), runner=runner, root=tmp_path, restart=False, platform_name="nt"
    )

    assert not result.ok
    assert "process 4242" in result.message
    assert runner.ran("git", "merge")
    assert not runner.ran("pip")
    assert manifest.read_bytes() == manifest_before


def test_windows_update_migrates_installer_command_shim_to_python_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_running_launchers(monkeypatch)
    (tmp_path / ".git").mkdir()
    _write_pyproject(tmp_path, "same")
    write_webui_build(tmp_path)
    _scripts_dir, python_executable = _windows_environment(tmp_path)
    shim = tmp_path / "bin" / "vbot.cmd"
    shim.parent.mkdir()
    shim.write_bytes(b'@echo off\r\n"old\\vbot.exe" %*\r\n')
    _write_state(tmp_path, python_executable=str(python_executable), webui_revision="samesha")

    def search_runtime(command: list[str]) -> CommandRun | None:
        if command[1:] == ["-m", "cli.search_runtime"]:
            return None
        return only_reads("symbolic-ref", "rev-parse", "status", "fetch", "rev-list")(command)

    for _attempt in range(2):
        result = run_update(
            _instance(),
            runner=ScriptedRunner(checkout(answer=search_runtime)),
            root=tmp_path,
            restart=False,
            platform_name="nt",
        )

        assert isinstance(result, UpdateResult)
        assert result.ok, result.message
        assert result.restart_state == "unchanged"
        assert shim.read_bytes() == (
            f'@echo off\r\n"{python_executable}" -P -m cli.main %*\r\n'.encode()
        )


def test_running_launcher_lookup_matches_only_the_exact_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "owned" / "Scripts" / "vbot-desktop.exe"
    other_launcher = tmp_path / "other" / "Scripts" / "vbot-desktop.exe"
    package_launcher = tmp_path / "owned" / "Scripts" / "vbot.exe"

    class FakeProcess:
        def __init__(self, process_id: int, executable: Path | None) -> None:
            self.info = {"pid": process_id, "exe": None if executable is None else str(executable)}

    processes = [
        FakeProcess(1000, None),  # access denied: no executable path
        FakeProcess(1001, other_launcher),
        FakeProcess(1002, launcher),
        FakeProcess(os.getpid(), package_launcher),
    ]
    monkeypatch.setattr(
        update_management.psutil, "process_iter", lambda _attributes: iter(processes)
    )

    assert _running_process_id(launcher) == 1002
    assert _running_process_id(tmp_path / "missing" / "vbot-desktop.exe") is None
    # The update itself may run from the package launcher; only an explicit lookup counts it.
    assert _running_process_id(package_launcher) is None
    assert _running_process_id(package_launcher, include_current=True) == os.getpid()


def test_windows_desktop_update_refreshes_shortcut_to_gui_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_running_launchers(monkeypatch)
    (tmp_path / ".git").mkdir()
    _write_pyproject(tmp_path, "before")
    setup_script = tmp_path / "scripts" / "setup.ps1"
    setup_script.parent.mkdir()
    setup_script.write_text("# shortcut mode", encoding="utf-8")
    write_webui_build(tmp_path)
    scripts_dir, python_executable = _windows_environment(tmp_path, "vbot-desktop.exe")
    _write_state(
        tmp_path,
        revision="old",
        shape="server-desktop",
        python_executable=str(python_executable),
        webui_revision="old",
    )
    runner = ScriptedRunner(
        checkout(
            heads=["old", "new"],
            upstream=_upstream(behind=1),
            on_merge=lambda: _write_pyproject(tmp_path, "after"),
        )
    )
    events, stop, start = _recording_restart()

    result = run_update(
        _instance(), runner=runner, root=tmp_path, stop=stop, start=start, platform_name="nt"
    )

    assert result.ok, result.message
    powershell = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ]
    desktop_launcher = str((scripts_dir / "vbot-desktop.exe").resolve())
    assert any(
        call[:7] == powershell and call[-2:] == ["-DesktopShortcutTarget", desktop_launcher]
        for call in runner.calls
    )
    assert events == ["stop", "start"]
