from __future__ import annotations

import base64
import ctypes
import json
import re
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

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


def test_host_exit_validates_exact_process_and_writes_bounded_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
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

        def exe(self) -> str:
            return str(install.root / "vBot.exe")

        def is_running(self) -> bool:
            return False

        def status(self) -> str:
            return "stopped"

    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=Process, NoSuchProcess=NoSuchProcessError, STATUS_ZOMBIE="zombie"),
    )

    result = request_host_exit(install)

    request = json.loads((install.root / "host-exit-request.json").read_text(encoding="utf-8"))
    assert request["schema_version"] == 1
    assert len(request["nonce"]) == 32
    assert result == {"ok": True, "running": False, "changed": True}


def test_host_exit_accepts_short_path_alias_for_owned_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "application with spaces")
    launcher = install.root / "vBot.exe"
    buffer = ctypes.create_unicode_buffer(32768)
    length = 0
    if sys.platform == "win32":
        short_path = ctypes.windll.kernel32.GetShortPathNameW
        short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        short_path.restype = wintypes.DWORD
        length = short_path(str(launcher), buffer, len(buffer))
    else:
        pytest.skip("Windows short-path behavior")
    if not length or length >= len(buffer) or Path(buffer.value) == launcher:
        pytest.skip("8.3 aliases are unavailable for this test directory")
    (install.root / "host.json").write_text(
        json.dumps({"schema_version": 1, "pid": 42, "process_created": 12.5}),
        encoding="utf-8",
    )

    class Process:
        def __init__(self, pid: int) -> None:
            assert pid == 42

        def create_time(self) -> float:
            return 12.5

        def exe(self) -> str:
            return buffer.value

        def is_running(self) -> bool:
            return False

        def status(self) -> str:
            return "stopped"

    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=Process, NoSuchProcess=RuntimeError, STATUS_ZOMBIE="zombie"),
    )

    assert request_host_exit(install)["changed"] is True


def test_host_exit_refuses_existing_foreign_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path / "app")
    foreign = tmp_path / "foreign.exe"
    foreign.write_bytes(b"foreign")
    (install.root / "host.json").write_text(
        json.dumps({"schema_version": 1, "pid": 42, "process_created": 12.5}),
        encoding="utf-8",
    )

    class Process:
        def __init__(self, pid: int) -> None:
            assert pid == 42

        def create_time(self) -> float:
            return 12.5

        def exe(self) -> str:
            return str(foreign)

    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=Process, NoSuchProcess=RuntimeError, STATUS_ZOMBIE="zombie"),
    )

    with pytest.raises(ApplicationError, match="targets another executable"):
        request_host_exit(install)


def test_uninstall_stops_exact_server_and_preserves_data_by_default(tmp_path: Path) -> None:
    install = _install(tmp_path / "app")
    uninstaller = install.root / "unins000.exe"
    uninstaller.write_bytes(b"exe")
    uninstaller.with_suffix(".dat").write_bytes(b"data")
    events: list[str] = []

    def stop(candidate: Installation) -> SimpleNamespace:
        events.append(f"stop:{candidate.server_port}")
        return SimpleNamespace(ok=True, message="stopped")

    result = uninstall(
        install,
        platform="win32",
        runner=lambda command: CommandRun(3, "", ""),
        exit_host=lambda candidate: events.append("exit-host"),
        stop=stop,
        launcher=lambda path: events.append(f"launch:{path.name}"),
    )

    assert events == ["exit-host", "stop:8420", "launch:unins000.exe"]
    assert result["data_preserved"] is True


def test_uninstall_does_not_launch_when_exact_stop_fails(tmp_path: Path) -> None:
    install = _install(tmp_path / "app")
    (install.root / "unins000.exe").write_bytes(b"exe")
    (install.root / "unins000.dat").write_bytes(b"data")
    launched: list[Path] = []

    with pytest.raises(ApplicationError, match="could not stop"):
        uninstall(
            install,
            platform="win32",
            runner=lambda command: CommandRun(3, "", ""),
            exit_host=lambda candidate: None,
            stop=lambda candidate: SimpleNamespace(ok=False, message="busy"),
            launcher=launched.append,
        )
    assert launched == []


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
