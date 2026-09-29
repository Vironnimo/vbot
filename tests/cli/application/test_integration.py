from __future__ import annotations

import base64
import ctypes
import json
import re
import subprocess
import sys
import uuid
from contextlib import nullcontext, suppress
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from cli.application import integration
from cli.application.integration import (
    autostart,
    prepare_checkout_transition,
    request_host_exit,
    uninstall,
)
from cli.application.state import ApplicationError, Installation
from cli.autostart_management import CommandRun
from cli.install_state import build_install_state, write_install_state


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


def _operation(command: list[str]) -> dict[str, object]:
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    token = re.search(r'\$payloadToken = "([A-Za-z0-9+/=]+)"', script)
    assert token
    return cast(dict[str, object], json.loads(base64.b64decode(token.group(1))))


def test_packaged_autostart_registers_and_verifies_exact_no_argument_launcher(
    tmp_path: Path,
) -> None:
    install = _install(tmp_path / "app")
    registration: dict[str, str] = {}

    def runner(command: list[str]) -> CommandRun:
        payload = _operation(command)
        operation = payload["operation"]
        if operation == "inspect":
            if not registration:
                return CommandRun(3, "", "")
            return CommandRun(0, json.dumps(registration), "")
        assert operation == "enable"
        registration.update(launcher=str(payload["launcher"]), arguments=str(payload["arguments"]))
        return CommandRun(0, "", "")

    result = autostart(install, "enable", platform="win32", runner=runner)

    assert result["enabled"] is True
    assert registration == {"launcher": str(install.root / "vBot.exe"), "arguments": ""}
    assert autostart(install, "status", platform="win32", runner=runner)["enabled"] is True


def test_packaged_autostart_refuses_foreign_registration_before_disable(tmp_path: Path) -> None:
    install = _install(tmp_path / "app")

    def runner(command: list[str]) -> CommandRun:
        assert _operation(command)["operation"] == "inspect"
        return CommandRun(0, json.dumps({"launcher": "C:/foreign.exe", "arguments": ""}), "")

    with pytest.raises(ApplicationError, match="belongs to another"):
        autostart(install, "disable", platform="win32", runner=runner)


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


@pytest.mark.parametrize("executable", ["owned", "short-path-alias", "foreign"])
def test_host_exit_requests_only_the_exact_owned_host_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str
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
            return False

        def status(self) -> str:
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


def test_data_only_reset_preserves_application_autostart_and_running_state(tmp_path: Path) -> None:
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

    result = uninstall(
        install,
        data_only=True,
        platform="win32",
        probe=lambda instance: SimpleNamespace(is_vbot=True),
        stop=stop,
        start=start,
        remove_tree=lambda path: events.append(f"remove:{path.name}"),
    )

    assert events == ["stop", "remove:data", "start"]
    assert result == {
        "ok": True,
        "completed": True,
        "application_preserved": True,
        "autostart_preserved": True,
        "data_removed": True,
        "server_restarted": True,
    }


def test_checkout_transition_refuses_dirty_source_before_stop(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = build_install_state(
        checkout,
        install_shape="server",
        dependency_groups=("server", "cli"),
        python_executable=str(checkout / ".venv/Scripts/python.exe"),
        server_host="127.0.0.1",
        server_port=8420,
        server_data_directory=str(tmp_path / "data"),
    )
    write_install_state(checkout, state)
    stopped: list[object] = []

    with pytest.raises(ApplicationError, match="customize prepare"):
        prepare_checkout_transition(
            checkout,
            shape="server",
            platform="linux",
            git_runner=lambda path: subprocess.CompletedProcess([], 0, " M core/file.py\n", ""),
            stop=lambda instance: stopped.append(instance),
        )
    assert stopped == []


def test_checkout_status_runs_windowless_and_retains_captured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options: dict[str, object] = {}

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        options.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, " M retained.py\n", "")

    monkeypatch.setattr(integration, "subprocess_creation_flags", lambda: 456)
    monkeypatch.setattr(integration.subprocess, "run", run)

    result = integration._git_status(tmp_path)

    assert options["creationflags"] == 456
    assert options["capture_output"] is True
    assert result.stdout == " M retained.py\n"
