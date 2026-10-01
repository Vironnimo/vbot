from __future__ import annotations

import ctypes
import json
import sys
import uuid
from contextlib import nullcontext, suppress
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.application import integration
from cli.application.autostart import UNIT_NAME, CommandRun, autostart, owned_unit, user_unit_dir
from cli.application.integration import request_host_exit, uninstall
from cli.application.state import ApplicationError, Installation
from cli.server_management import ServerState


def _install(root: Path, shape: str = "server") -> Installation:
    root.mkdir(parents=True)
    (root / "vBot.exe").write_bytes(b"bootstrap")
    return Installation(
        root,
        shape,
        None if shape == "desktop-client" else "127.0.0.1",
        None if shape == "desktop-client" else 8420,
        None if shape == "desktop-client" else str((root.parent / "data").resolve()),
    )


def _short_path_alias(path: Path) -> str | None:
    """Return the Windows 8.3 alias of *path*, or None where none exists."""

    if sys.platform != "win32":
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    short_path = ctypes.windll.kernel32.GetShortPathNameW
    short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    short_path.restype = wintypes.DWORD
    length = short_path(str(path), buffer, len(buffer))
    if not length or length >= len(buffer) or Path(buffer.value) == path:
        return None
    return buffer.value


@pytest.mark.parametrize(
    ("executable", "exit_seen"),
    [
        ("owned", "stopped"),
        ("owned", "vanished"),
        ("short-path-alias", "stopped"),
        ("foreign", "stopped"),
    ],
)
def test_host_exit_requests_only_the_exact_owned_host_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str, exit_seen: str
) -> None:
    install = _install(tmp_path / "application with spaces")
    launcher = install.root / "vBot.exe"
    foreign = tmp_path / "foreign.exe"
    foreign.write_bytes(b"foreign")
    alias = _short_path_alias(launcher) if executable == "short-path-alias" else None
    if executable == "short-path-alias" and alias is None:
        pytest.skip("8.3 aliases are unavailable for this test directory")
    process_executable = {"owned": str(launcher), "foreign": str(foreign)}.get(executable, alias)
    (install.root / "host.json").write_text(
        json.dumps({"schema_version": 1, "pid": 42, "process_created": 12.5}),
        encoding="utf-8",
    )

    class NoSuchProcessError(Exception):
        pass

    class Process:
        def __init__(self, pid: int) -> None:
            assert pid == 42

        def create_time(self) -> float:
            return 12.5

        def exe(self) -> str | None:
            return process_executable

        def is_running(self) -> bool:
            return exit_seen == "vanished"

        def status(self) -> str:
            # The host can exit between the running check and the status query.
            if exit_seen == "vanished":
                raise NoSuchProcessError
            return "stopped"

    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=Process, NoSuchProcess=NoSuchProcessError, STATUS_ZOMBIE="zombie"),
    )
    request_path = install.root / "host-exit-request.json"

    if executable == "foreign":
        with pytest.raises(ApplicationError, match="targets another executable"):
            request_host_exit(install)
        assert not request_path.exists()
        return
    result = request_host_exit(install)

    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["schema_version"] == 1
    assert len(request["nonce"]) == 32
    assert result == {"ok": True, "running": False, "changed": True}


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "stop-fails"])
def test_uninstall_launches_only_after_the_exact_server_stopped(
    tmp_path: Path, stopped: bool
) -> None:
    install = _install(tmp_path / "app")
    uninstaller = install.root / "unins000.exe"
    uninstaller.write_bytes(b"exe")
    uninstaller.with_suffix(".dat").write_bytes(b"data")
    events: list[str] = []

    def stop(candidate: Installation) -> SimpleNamespace:
        events.append(f"stop:{candidate.server_port}")
        return SimpleNamespace(ok=stopped, message="stopped" if stopped else "busy")

    with nullcontext() if stopped else pytest.raises(ApplicationError, match="could not stop"):
        result = uninstall(
            install,
            platform="win32",
            runner=lambda command: CommandRun(3, "", ""),
            exit_host=lambda candidate: events.append("exit-host"),
            stop=stop,
            launcher=lambda path: events.append(f"launch:{path.name}"),
        )

    assert events == ["exit-host", "stop:8420", *(["launch:unins000.exe"] if stopped else [])]
    if stopped:
        assert result["data_preserved"] is True


def test_linux_uninstall_removes_the_unit_command_link_and_application_but_keeps_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = _install(tmp_path / "app")
    install.bootstrap.write_bytes(b"bootstrap")
    commands: list[list[str]] = []

    def runner(command: list[str]) -> CommandRun:
        commands.append(command)
        return CommandRun(0, "", "")

    autostart(install, "enable", platform="linux", runner=runner)
    assert owned_unit(install, unit_dir=user_unit_dir()) is not None
    link = tmp_path / "bin" / "vbot"
    link.parent.mkdir()
    foreign_link = tmp_path / "bin" / "other"
    try:
        link.symlink_to(install.bootstrap)
        foreign_link.symlink_to(tmp_path / "elsewhere")
    except OSError:
        pytest.skip("symbolic links are unavailable here")
    events: list[str] = []
    commands.clear()

    result = uninstall(
        install,
        platform="linux",
        runner=runner,
        exit_host=lambda candidate: events.append("exit-host"),
        stop=lambda candidate: SimpleNamespace(ok=True, message="stopped"),
        launcher=lambda path: events.append("launch"),
        remove_tree=lambda path: events.append(f"remove:{path}"),
        command_link=link,
    )

    assert result["removed"] is True and result["data_preserved"] is True
    assert ["systemctl", "--user", "disable", UNIT_NAME] in commands
    assert not (user_unit_dir() / UNIT_NAME).exists()
    assert events == ["exit-host", f"remove:{install.root}"] and not link.is_symlink()
    # A command link that names something else is not vBot's to remove.
    assert uninstall(
        _install(tmp_path / "second"),
        platform="linux",
        runner=runner,
        exit_host=lambda candidate: None,
        stop=lambda candidate: SimpleNamespace(ok=True, message="stopped"),
        remove_tree=lambda path: None,
        command_link=foreign_link,
    )["removed"]
    assert foreign_link.is_symlink()


def test_notification_identity_is_removed_only_by_the_installation_it_points_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows notification identity")
    import winreg

    key = rf"Software\vBot-tests\{uuid.uuid4().hex}"
    monkeypatch.setattr(integration, "_NOTIFICATION_IDENTITY_KEY", key)
    owner, other = _install(tmp_path / "owner"), _install(tmp_path / "other")
    try:
        integration.register_notification_identity(owner.root / "versions" / "v1" / "icon.ico")
        integration.remove_notification_identity(other)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as registered:
            assert winreg.QueryValueEx(registered, "DisplayName")[0] == "vBot"

        integration.remove_notification_identity(owner)

        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, key)
    finally:
        for leftover in (key, r"Software\vBot-tests"):
            with suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, leftover)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param("running", ["stop", "remove:data", "start"], id="running"),
        pytest.param("absent", ["remove:data"], id="stopped"),
        # A server that lives without answering is neither stopped nor reset under.
        pytest.param("unresponsive", None, id="busy-server"),
        pytest.param("foreign", None, id="foreign-server"),
    ],
)
def test_data_only_reset_preserves_application_autostart_and_running_state(
    tmp_path: Path, state: ServerState, expected: list[str] | None
) -> None:
    install = _install(tmp_path / "app")
    assert install.server_data_directory is not None
    data = Path(install.server_data_directory)
    data.mkdir()
    (data / "state.db").write_bytes(b"state")
    events: list[str] = []

    def stop(candidate: Installation) -> SimpleNamespace:
        events.append("stop")
        return SimpleNamespace(ok=True, message="stopped")

    def start(candidate: Installation) -> SimpleNamespace:
        events.append("start")
        return SimpleNamespace(ok=True, message="started")

    with (
        nullcontext()
        if expected is not None
        else pytest.raises(ApplicationError, match="vBot data was not reset")
    ):
        result = uninstall(
            install,
            data_only=True,
            platform="win32",
            server_state=lambda _install: state,
            stop=stop,
            start=start,
            remove_tree=lambda path: events.append(f"remove:{path.name}"),
        )

    assert events == (expected or [])
    if expected is not None:
        assert result == {
            "ok": True,
            "completed": True,
            "application_preserved": True,
            "autostart_preserved": True,
            "data_removed": True,
            "server_restarted": state == "running",
        }
