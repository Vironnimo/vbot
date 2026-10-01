"""The tray host delegates lifecycle policy to the installed application owners."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cli._server_target import HealthProbeResult
from cli.application import desktop, host
from cli.application.monitor import MonitorStatus
from cli.application.notifications import Toast
from cli.application.state import ApplicationError, Installation, Operation


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

    state = facade.state()
    assert state.server_state == "not_applicable"
    assert state.version == "0.4.2"


def test_tray_version_and_pending_restart_refresh_only_on_activation(tmp_path, monkeypatch):
    install = _install(tmp_path, shape="desktop-client")
    install.save()
    manifest = install.version() / "release.json"
    manifest.write_text(json.dumps({"version": "1.0", "files": {"fixture": "x" * 1024**2}}))
    # The tray runs the code of the version that was active when it started.
    loaded = install.version() / "app" / "cli" / "application" / "host.py"
    monkeypatch.setattr(host, "__file__", str(loaded))
    read = host.read_json
    reads = []

    def observed(path, **kwargs):
        reads.append(path)
        return read(path, **kwargs)

    monkeypatch.setattr(host, "read_json", observed)
    facade = host.ApplicationFacade(install)
    assert (facade.state().version, facade.state().restart_pending) == ("1.0", False)
    assert reads == [manifest]
    next_version = install.version("rel_next")
    next_version.mkdir()
    (next_version / "release.json").write_text('{"version":"1.1"}')
    install.activate("rel_next")
    update = Operation(id="upd_next", phase="verifying", message="Verifying")
    update.save(install)
    # The update that activated the version is still running.
    assert (facade.state().version, facade.state().restart_pending) == ("1.1", False)
    update.transition(install, "completed", "Updated")
    assert facade.state().restart_pending is True
    assert reads == [manifest, next_version / "release.json"]
    # A tray running from a development checkout never restarts.
    assert host.ApplicationFacade(install, running_version=None).state().restart_pending is False


def test_restart_hands_over_to_the_bootstrap_and_keeps_the_server_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    install = _install(tmp_path)
    facade = host.ApplicationFacade(install, running_version="rel_old")
    launches: list[tuple[list[str], dict[str, Any]]] = []
    exit_code: list[int | None] = [1]

    class Successor:
        returncode: int | None = None

        def wait(self, timeout: float) -> int:
            if exit_code[0] is None:
                raise subprocess.TimeoutExpired("vBot.exe", timeout)
            self.returncode = exit_code[0]
            return exit_code[0]

    def launch(arguments: list[str], **options: Any) -> Successor:
        launches.append((arguments, options))
        return Successor()

    monkeypatch.setattr(host.subprocess, "Popen", launch)
    monkeypatch.setattr(
        host,
        "subprocess_creation_flags",
        lambda **kwargs: 11 if kwargs == {"new_process_group": True, "breakaway": True} else 0,
    )
    monkeypatch.setattr(
        host.processes, "stop", lambda *_args, **_kwargs: pytest.fail("the server keeps running")
    )

    # A successor that exits at once leaves this tray in charge.
    with pytest.raises(ApplicationError):
        facade.restart()
    exit_code[0] = None
    with caplog.at_level(logging.INFO, logger="vbot.application.host"):
        facade.restart()

    arguments, options = launches[-1]
    assert arguments == [str(install.root / "vBot.exe")]
    assert options["cwd"] == install.root
    assert options["env"]["VBOT_HOST_SUCCESSOR"] == "1"
    assert options["env"]["VBOT_INSTALL_ROOT"] == str(install.root)
    assert options["creationflags"] == 11
    assert all(options[name] is subprocess.DEVNULL for name in ("stdin", "stdout", "stderr"))
    assert [record.levelno for record in caplog.records] == [logging.INFO]
    assert "from=rel_old to=rel_current" in caplog.records[0].getMessage()


class _Monitor:
    instances: list[_Monitor] = []

    def __init__(self, target, listener, *, local: bool, user_agent: str, classify=None) -> None:
        self.target, self.listener, self.local = target, listener, local
        self.classify = classify
        self.status = MonitorStatus()
        self.reconnects = 0
        _Monitor.instances.append(self)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def reconnect(self) -> None:
        self.reconnects += 1

    async def rpc(self, method: str, params: dict[str, Any]) -> None:
        return None


class _Sink:
    def __init__(self) -> None:
        self.changes = 0
        self.toasts: list[Toast] = []

    def changed(self) -> None:
        self.changes += 1

    def show(self, toast: Toast) -> None:
        self.toasts.append(toast)

    def dismiss(self, key: str) -> None:
        pass


def _watched(
    install: Installation, monkeypatch: pytest.MonkeyPatch
) -> tuple[host.ApplicationFacade, _Monitor, _Sink]:
    monkeypatch.setattr(host, "ServerMonitor", _Monitor)
    monkeypatch.setattr(
        host.processes,
        "target",
        lambda _install: SimpleNamespace(url="http://127.0.0.1:8420", data_dir=Path("data")),
    )
    facade, sink = host.ApplicationFacade(install), _Sink()
    facade.watch(sink)
    return facade, _Monitor.instances[-1], sink


def _on_monitor_loop(callback) -> None:
    """Deliver a monitor callback the way the monitor does: on its event loop."""

    async def deliver() -> None:
        callback()

    asyncio.run(deliver())


@pytest.mark.parametrize(
    ("status", "server_state"),
    [
        pytest.param(MonitorStatus(), "unknown", id="before-first-attempt"),
        pytest.param(MonitorStatus("u", "connected", True), "running", id="connected"),
        pytest.param(MonitorStatus("u", "unresponsive"), "unresponsive", id="busy-server"),
        pytest.param(MonitorStatus("u", "refused"), "stopped", id="refused"),
        pytest.param(MonitorStatus("u", "unreachable"), "stopped", id="unreachable"),
        pytest.param(MonitorStatus("u", "rejected", True), "running", id="safe-mode"),
        pytest.param(MonitorStatus("u", "rejected", False), "conflict", id="foreign-listener"),
    ],
)
def test_server_state_follows_the_event_stream_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: MonitorStatus, server_state: str
):
    facade, monitor, sink = _watched(_install(tmp_path), monkeypatch)
    assert monitor.target() == "http://127.0.0.1:8420"
    assert monitor.local is True
    # The monitor classifies a silent owned target from its own unanswered request.
    classified: list[tuple[str, HealthProbeResult]] = []

    def classify_server(instance: Any, *, health: HealthProbeResult) -> str:
        classified.append((instance.url, health))
        return "unresponsive"

    monkeypatch.setattr(host, "classify_server", classify_server)
    silent = HealthProbeResult(reachable=False, is_vbot=False, timed_out=True)
    assert monitor.classify(silent) == "unresponsive"
    assert classified == [("http://127.0.0.1:8420", silent)]

    monitor.status = status
    _on_monitor_loop(lambda: monitor.listener.status_changed(status))

    assert facade.state().server_state == server_state
    assert sink.changes == 1


def test_update_progress_is_recorded_and_its_result_toasted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    Operation(id="upd_old", phase="failed", message="Old failure").save(install)
    facade, _monitor, sink = _watched(install, monkeypatch)
    # An update that finished before the tray started is only history.
    assert facade.state().update_activity == ()
    assert sink.toasts == []

    operation = Operation(id="upd_new", phase="preparing", message="Checking release")
    operation.save(install)
    state = facade.state()
    assert (state.update_phase, state.update_message) == ("preparing", "Checking release")
    operation.target_label, operation.previous_label = "0.5.0", "0.4.2"
    operation.server_was_running = True
    operation.transition(install, "completed", "Updated")
    lines = facade.state().update_activity
    assert facade.state().update_activity == lines

    assert [line.split("  ", 1)[1] for line in lines] == [
        "Checking release",
        "Updating vBot: 0.4.2 -> 0.5.0",
        "Update completed — server restarted and passed its health check.",
    ]
    assert [(toast.kind, toast.title) for toast in sink.toasts] == [
        ("update_result", "vBot updated")
    ]
    assert "Version: 0.4.2 -> 0.5.0" in sink.toasts[0].body
    assert any(label == "Last update" for label, _value in facade.state().details)


def test_tray_initiated_stop_never_counts_as_an_unexpected_server_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    facade, monitor, sink = _watched(_install(tmp_path), monkeypatch)

    @contextmanager
    def lock(_root: Path, _name: str, **_kwargs: object):
        yield

    monkeypatch.setattr(host, "exclusive", lock)
    monkeypatch.setattr(
        host.processes, "stop", lambda _install, **_kwargs: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(host.processes, "start", lambda _install: SimpleNamespace(ok=True))
    facade.stop_server()
    monitor.listener.connection_lost(1006)
    monitor.listener.status_changed(MonitorStatus("u", "refused"))
    facade.start_server()

    assert sink.toasts == []
    assert monitor.reconnects == 1


def test_malformed_host_exit_request_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install = _install(tmp_path)
    (install.root / "host-exit-request.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(host.processes, "target", lambda _install: SimpleNamespace(url=""))

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
        host.processes,
        "stop",
        lambda _install, **_kwargs: SimpleNamespace(ok=True, message="stopped"),
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
    monkeypatch.setattr(desktop, "subprocess_creation_flags", lambda **_kwargs: 7)
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
    # The Desktop restarts into a later active version through the stable launcher.
    assert json.loads(environment[desktop.RELAUNCH_ENV]) == {
        "version_file": str(install.root / "active-version"),
        "version": install.version().name,
        "command": [str(install.root / "vBot.GUI.exe"), "desktop"],
    }


@pytest.mark.parametrize(
    ("shape", "target", "target_arguments"),
    [
        pytest.param("desktop-client", {}, [], id="client-without-implicit-target"),
        pytest.param(
            "server-desktop",
            {"host": "192.0.2.8", "port": 18420},
            ["--host", "192.0.2.8", "--port", "18420"],
            id="explicit-target",
        ),
    ],
)
def test_open_desktop_targets_only_an_explicit_or_owned_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    target: dict[str, Any],
    target_arguments: list[str],
) -> None:
    install = _install(tmp_path, shape=shape)
    executable = _desktop_interpreter(install)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    launches: list[list[str]] = []
    monkeypatch.setattr(host.subprocess, "Popen", lambda args, **_kwargs: launches.append(args))

    host.ApplicationFacade(install).open_desktop(**target)

    assert launches == [[str(executable), "-m", "desktop.main", *target_arguments]]


@pytest.mark.parametrize(
    ("shape", "opened"),
    [
        pytest.param(
            "server",
            "http://127.0.0.1:8420/?open_agent=coder%40site&open_session=ses_1",
            id="browser-deep-link",
        ),
        pytest.param(
            "server-desktop",
            ["--host", "127.0.0.1", "--port", "8420", "--open-session", "coder@site", "ses_1"],
            id="owned-desktop",
        ),
        pytest.param(
            "desktop-client",
            ["--host", "192.0.2.8", "--port", "18420", "--open-session", "coder@site", "ses_1"],
            id="client-desktop-target",
        ),
    ],
)
def test_open_session_shows_the_session_where_this_installation_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, opened: object
):
    install = _install(tmp_path, shape=shape)
    executable = _desktop_interpreter(install)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    launches: list[object] = []
    monkeypatch.setattr(host.subprocess, "Popen", lambda args, **_kwargs: launches.append(args[3:]))
    monkeypatch.setattr(host.webbrowser, "open", launches.append)
    monkeypatch.setattr(
        host.processes, "target", lambda _install: SimpleNamespace(url="http://127.0.0.1:8420")
    )
    monkeypatch.setattr(host, "_desktop_target", lambda: ("192.0.2.8", 18420))

    host.ApplicationFacade(install).open_session("coder@site", "ses_1")

    assert launches == [opened]


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


@pytest.mark.parametrize(
    ("successor", "timeout", "started"),
    [
        pytest.param(None, 0, ["start"], id="first-host"),
        # A restart successor waits for its predecessor and keeps the server as
        # it is, including a server the user stopped on purpose.
        pytest.param("1", 30, [], id="restart-successor"),
    ],
)
def test_main_recovers_before_initial_start_and_only_a_first_host_starts_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    successor: str | None,
    timeout: float,
    started: list[str],
):
    install = _install(tmp_path)
    order: list[str] = []
    if successor is None:
        monkeypatch.delenv("VBOT_HOST_SUCCESSOR", raising=False)
    else:
        monkeypatch.setenv("VBOT_HOST_SUCCESSOR", successor)

    @contextmanager
    def lock(_root: Path, name: str, *, timeout: float):
        order.append(f"lock:{name}:{timeout:g}")
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
    monkeypatch.setattr(host.ApplicationFacade, "start_server", lambda _self: order.append("start"))
    monkeypatch.setattr(host, "run_tray", lambda _facade, _icon: order.append("tray"))

    assert host.main() == 0
    assert order == [f"lock:host:{timeout:g}", "logging", "recover", *started, "tray", "close"]


def test_main_keeps_tray_running_when_initial_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    received: list[host.ApplicationFacade] = []

    @contextmanager
    def lock(_root: Path, _name: str, **_kwargs: object):
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
