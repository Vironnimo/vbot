"""The tray host delegates lifecycle policy to the installed application owners."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cli.application import host
from cli.application.state import ApplicationError, Installation


def _install(root: Path, *, shape: str = "server") -> Installation:
    install = Installation(
        root,
        shape,
        None if shape == "desktop-client" else "127.0.0.1",
        None if shape == "desktop-client" else 8420,
        None if shape == "desktop-client" else str((root / "data").resolve()),
    )
    version = root / "versions" / "rel_current"
    (version / "runtime").mkdir(parents=True)
    (version / "app" / "desktop").mkdir(parents=True)
    (version / "release.json").write_text('{"version":"0.4.2"}', encoding="utf-8")
    (root / "active-version").write_text("rel_current\n", encoding="ascii")
    return install


def _desktop_interpreter(install: Installation) -> Path:
    runtime = install.version() / "runtime"
    return runtime / ("vBot.Desktop.exe" if os.name == "nt" else "bin/python3")


def test_client_state_never_resolves_a_local_server_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    facade = host.ApplicationFacade(_install(tmp_path, shape="desktop-client"))
    monkeypatch.setattr(host.processes, "target", lambda _install: pytest.fail("must not target"))
    monkeypatch.setattr(host.operations, "status", lambda _install: None)

    state = facade.state()
    assert state.server_state == "not_applicable"
    assert state.version == "0.4.2"


def test_tray_version_handles_full_manifest_and_refreshes_only_on_activation(tmp_path, monkeypatch):
    install = _install(tmp_path, shape="desktop-client")
    manifest = install.version() / "release.json"
    manifest.write_text(json.dumps({"version": "1.0", "files": {"fixture": "x" * 1024**2}}))
    monkeypatch.setattr(host.operations, "status", lambda _install: None)
    read = host.read_json
    reads = []

    def observed(path, **kwargs):
        reads.append(path)
        return read(path, **kwargs)

    monkeypatch.setattr(host, "read_json", observed)
    facade = host.ApplicationFacade(install)
    assert facade.state().version == "1.0"
    assert facade.state().version == "1.0"
    assert reads == [manifest]
    next_version = install.version("rel_next")
    next_version.mkdir()
    (next_version / "release.json").write_text('{"version":"1.1"}')
    install.activate("rel_next")
    assert facade.state().version == "1.1"
    assert reads == [manifest, next_version / "release.json"]


def test_server_state_uses_actual_health_and_latest_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    facade = host.ApplicationFacade(_install(tmp_path))
    instance = SimpleNamespace(url="http://127.0.0.1:8420", data_dir=tmp_path / "data")
    monkeypatch.setattr(host.processes, "target", lambda _install: instance)
    monkeypatch.setattr(
        host.processes,
        "probe_health",
        lambda _instance: SimpleNamespace(is_vbot=True, reachable=True),
    )
    monkeypatch.setattr(
        host.operations,
        "status",
        lambda _install: SimpleNamespace(
            phase="preparing", terminal=False, message="Checking release"
        ),
    )

    state = facade.state()
    assert state.server_state == "running"
    assert (state.update_phase, state.update_message) == ("preparing", "Checking release")


def test_terminal_update_state_keeps_current_server_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    facade = host.ApplicationFacade(_install(tmp_path))
    instance = SimpleNamespace(url="http://127.0.0.1:8420", data_dir=tmp_path / "data")
    monkeypatch.setattr(host.processes, "target", lambda _install: instance)
    monkeypatch.setattr(
        host.processes,
        "probe_health",
        lambda _instance: SimpleNamespace(is_vbot=False, reachable=False),
    )
    monkeypatch.setattr(
        host.operations,
        "status",
        lambda _install: SimpleNamespace(phase="completed", terminal=True, message="Updated"),
    )

    state = facade.state()
    assert state.server_state == "stopped"
    assert (state.update_phase, state.update_message) == ("completed", "Updated")


def test_malformed_host_exit_request_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install = _install(tmp_path)
    (install.root / "host-exit-request.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(host.operations, "status", lambda _install: None)
    monkeypatch.setattr(host.processes, "target", lambda _install: SimpleNamespace())
    monkeypatch.setattr(
        host.processes,
        "probe_health",
        lambda _instance: SimpleNamespace(is_vbot=False, reachable=False),
    )

    assert host.ApplicationFacade(install).state().exit_requested is False

    (install.root / "host-exit-request.json").write_text(
        json.dumps({"schema_version": 1, "nonce": "invalid"}), encoding="utf-8"
    )
    assert host.ApplicationFacade(install).state().exit_requested is False


def test_lifecycle_actions_hold_operation_lock_and_raise_for_failed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    facade = host.ApplicationFacade(_install(tmp_path))
    calls: list[str] = []

    @contextmanager
    def lock(_root: Path, name: str):
        calls.append(f"lock:{name}")
        yield

    monkeypatch.setattr(host, "exclusive", lock)
    monkeypatch.setattr(
        host.processes, "start", lambda _install: SimpleNamespace(ok=False, message="occupied")
    )
    with pytest.raises(ApplicationError, match="occupied"):
        facade.start_server()
    assert calls == ["lock:operation"]

    monkeypatch.setattr(
        host.processes, "stop", lambda _install: SimpleNamespace(ok=True, message="stopped")
    )
    monkeypatch.setattr(
        host.processes, "start", lambda _install: SimpleNamespace(ok=True, message="started")
    )
    facade.report_error("Startup failed: port is occupied")
    facade.restart_server()
    assert calls[-1] == "lock:operation"
    assert facade._status_error == ""


def test_open_desktop_uses_the_versioned_desktop_host_and_shape_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path, shape="server-desktop")
    executable = _desktop_interpreter(install)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    facade = host.ApplicationFacade(install)
    launches: list[tuple[list[str], dict[str, object]]] = []

    def launch(args, **kwargs):
        # The uninstaller must not reserve removal between the guard and spawn.
        with (
            pytest.raises(ApplicationError, match="Another application operation"),
            host.exclusive(install.root, "dispatch"),
        ):
            pytest.fail("removal was allowed to race Desktop startup")
        launches.append((args, kwargs))

    monkeypatch.setattr(host.subprocess, "Popen", launch)
    monkeypatch.setattr(host, "subprocess_creation_flags", lambda **_kwargs: 7)
    facade.open_desktop()

    arguments, options = launches[0]
    assert arguments == [
        str(executable),
        "-m",
        "desktop.main",
        "--host",
        "127.0.0.1",
        "--port",
        "8420",
    ]
    assert options["cwd"] == install.version() / "app"
    assert options["creationflags"] == 7
    environment = cast(dict[str, str], cast(dict[str, Any], options)["env"])
    assert environment["VBOT_INSTALL_ROOT"] == str(install.root)


def test_desktop_client_launches_without_an_implicit_server_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path, shape="desktop-client")
    executable = _desktop_interpreter(install)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    launches: list[list[str]] = []
    monkeypatch.setattr(host.subprocess, "Popen", lambda args, **_kwargs: launches.append(args))

    host.ApplicationFacade(install).open_desktop()
    assert launches[0] == [str(executable), "-m", "desktop.main"]


def test_open_desktop_preserves_explicit_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path, shape="server-desktop")
    executable = _desktop_interpreter(install)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    launches: list[list[str]] = []
    monkeypatch.setattr(host.subprocess, "Popen", lambda args, **_kwargs: launches.append(args))

    host.ApplicationFacade(install).open_desktop(host="192.0.2.8", port=18420)

    assert launches[0] == [
        str(executable),
        "-m",
        "desktop.main",
        "--host",
        "192.0.2.8",
        "--port",
        "18420",
    ]


def test_browser_and_logs_use_only_the_owned_local_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    instance = SimpleNamespace(url="http://127.0.0.1:8420", data_dir=tmp_path / "data")
    opened: list[object] = []
    monkeypatch.setattr(host.processes, "target", lambda _install: instance)
    monkeypatch.setattr(host.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(host, "_open_folder", lambda path: opened.append(path))

    facade = host.ApplicationFacade(install)
    facade.open_browser()
    facade.open_logs()
    facade.open_server_logs()
    assert opened == [instance.url, install.root / "logs", instance.data_dir / "logs"]

    client = host.ApplicationFacade(_install(tmp_path / "client", shape="desktop-client"))
    with pytest.raises(ApplicationError):
        client.open_browser()


def test_main_recovers_before_initial_start_and_never_restarts_after_manual_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    order: list[str] = []

    @contextmanager
    def lock(_root: Path, name: str):
        order.append(f"lock:{name}")
        yield

    class Manager:
        def __init__(self, **kwargs: object):
            order.append("logging")

        def close(self) -> None:
            order.append("close")

    monkeypatch.setattr(host, "discover", lambda: install)
    monkeypatch.setattr(host, "exclusive", lock)
    monkeypatch.setattr(host, "LogManager", Manager)
    monkeypatch.setattr(
        host.operations, "recover_operations", lambda _install: order.append("recover")
    )
    monkeypatch.setattr(host.operations, "status", lambda _install: None)
    monkeypatch.setattr(host.ApplicationFacade, "start_server", lambda _self: order.append("start"))
    monkeypatch.setattr(host, "run_tray", lambda _facade, _icon: order.append("tray"))

    assert host.main() == 0
    assert order == ["lock:host", "logging", "recover", "start", "tray", "close"]


def test_main_keeps_tray_running_when_initial_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    received: list[host.ApplicationFacade] = []

    @contextmanager
    def lock(_root: Path, _name: str):
        yield

    class Manager:
        def __init__(self, **_kwargs: object):
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(host, "discover", lambda: install)
    monkeypatch.setattr(host, "exclusive", lock)
    monkeypatch.setattr(host, "LogManager", Manager)
    monkeypatch.setattr(host.operations, "recover_operations", lambda _install: None)
    monkeypatch.setattr(host.operations, "status", lambda _install: None)
    monkeypatch.setattr(
        host.ApplicationFacade,
        "start_server",
        lambda _self: (_ for _ in ()).throw(ApplicationError("port is occupied")),
    )
    monkeypatch.setattr(host, "run_tray", lambda facade, _icon: received.append(facade))

    assert host.main() == 0
    assert len(received) == 1
    assert received[0].state().error == "Startup failed: port is occupied"
