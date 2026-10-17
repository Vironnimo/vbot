"""The tray host delegates lifecycle policy to the installed application owners."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil  # type: ignore[import-untyped]
import pytest

from cli._server_target import HealthProbeResult, RpcFailure
from cli.application import desktop, host
from cli.application.activity import JOURNAL_NAME, ActivityJournal
from cli.application.monitor import MonitorStatus
from cli.application.notifications import Toast
from cli.application.state import ApplicationError, Installation, Operation
from cli.rpc_client import RpcPayload
from core.access.config import access_config
from core.utils.logging import resolve_daily_log_path
from core.utils.server_control import create_server_control, process_started


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


def _activity(install: Installation) -> list[str]:
    """The kinds of the recorded activity entries, as any later tray reads them."""

    journal = ActivityJournal(install.root / "logs" / JOURNAL_NAME)
    return [entry.kind for entry in journal.entries()]


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

    def __init__(
        self, target, listener, *, local: bool, user_agent: str, classify=None, device_sign_in=None
    ) -> None:
        self.target, self.listener, self.local = target, listener, local
        self.classify, self.device_sign_in = classify, device_sign_in
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
    target = SimpleNamespace(url="http://127.0.0.1:8420", port=8420, data_dir=install.root / "data")
    monkeypatch.setattr(host.processes, "target", lambda _install: target)
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
        pytest.param(MonitorStatus("u", "not_listening"), "starting", id="process-starting"),
        pytest.param(MonitorStatus("u", "unreachable"), "stopped", id="unreachable"),
        pytest.param(MonitorStatus("u", "rejected", True), "running", id="safe-mode"),
        pytest.param(MonitorStatus("u", "rejected", False), "conflict", id="foreign-listener"),
        pytest.param(MonitorStatus("u", "denied", True), "running", id="stream-refused"),
    ],
)
def test_server_state_follows_the_event_stream_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: MonitorStatus, server_state: str
):
    facade, monitor, sink = _watched(_install(tmp_path), monkeypatch)
    assert monitor.target().url == "http://127.0.0.1:8420"
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


def test_update_milestones_are_recorded_once_and_the_result_toasted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    Operation(id="upd_old", phase="failed", message="Old failure").save(install)
    facade, _monitor, sink = _watched(install, monkeypatch)
    # An update that finished before the tray started is history: recorded, never toasted.
    assert len(facade.state().activity) == 1
    assert sink.toasts == []

    operation = Operation(id="upd_new", phase="preparing", message="Checking release")
    operation.save(install)
    state = facade.state()
    assert (state.update_phase, state.update_message) == ("preparing", "Checking release")
    operation.target_label, operation.previous_label = "0.5.0", "0.4.2"
    operation.server_was_running = True
    operation.transition(install, "verifying", "Verifying")
    facade.state()
    operation.transition(install, "completed", "Updated")
    lines = facade.state().activity
    assert facade.state().activity == lines

    assert _activity(install) == [
        "update_failed",
        "update_started",
        "update_target",
        "update_finished",
    ]
    # A successor tray, started after the update, records nothing twice.
    _watched(install, monkeypatch)[0].state()
    assert len(_activity(install)) == 4
    assert [(toast.kind, toast.title) for toast in sink.toasts] == [
        ("update_result", "vBot updated")
    ]
    assert "Version: 0.4.2 -> 0.5.0" in sink.toasts[0].body
    assert any(label == "Last update" for label, _value in facade.state().details)


def test_tray_starts_update_workers_itself_and_for_other_requesters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    facade = host.ApplicationFacade(install)
    dispatched: list[Path] = []
    requested: list[dict[str, object]] = []
    monkeypatch.setattr(
        host.operations, "dispatch_requested", lambda target: dispatched.append(target.root)
    )
    monkeypatch.setattr(
        host.operations,
        "request_update",
        lambda _install, **kwargs: requested.append(kwargs),
    )

    facade.start_update()
    assert requested == [{"host": True}]
    facade.state()
    assert dispatched == []
    # Another process, such as a CLI inside a coding agent's Windows Job, handed one over.
    (install.root / host.operations.DISPATCH_REQUEST).write_text("{}", encoding="utf-8")
    facade.state()
    assert dispatched == [install.root]

    def refuse(_install: Installation) -> None:
        raise ApplicationError("the update process could not start")

    monkeypatch.setattr(host.operations, "dispatch_requested", refuse)
    assert "the update process could not start" in facade.state().error
    assert _activity(install)[-1] == "action_failed"


def test_tray_runs_a_handed_server_request_once_and_reports_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    facade = host.ApplicationFacade(install)
    request_id = "b" * 32
    result_file = install.root / f"host-server-result-{request_id}.json"

    @contextmanager
    def lock(_root: Path, _name: str):
        yield

    monkeypatch.setattr(host, "exclusive", lock)
    monkeypatch.setattr(
        host.processes, "stop", lambda _install, **_kwargs: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(
        host.processes,
        "start",
        lambda _install: SimpleNamespace(ok=True, message="started", process_id=55),
    )
    (install.root / host.operations.DISPATCH_REQUEST).write_text(
        json.dumps({"schema_version": 1, "server": "restart", "request": request_id}),
        encoding="utf-8",
    )

    request = facade.state().server_request
    assert request == host.operations.ServerRequest(request_id, "restart")
    # The requester sees it accepted; the controller gets it exactly once.
    assert json.loads(result_file.read_text(encoding="utf-8"))["state"] == "accepted"
    assert facade.state().server_request is None

    facade.answer_server_request(request)
    answer = json.loads(result_file.read_text(encoding="utf-8"))
    assert (answer["state"], answer["ok"], answer["process_id"]) == ("done", True, 55)

    monkeypatch.setattr(
        host.processes, "start", lambda _install: SimpleNamespace(ok=False, message="occupied")
    )
    with pytest.raises(ApplicationError, match="occupied"):
        facade.answer_server_request(request)
    answer = json.loads(result_file.read_text(encoding="utf-8"))
    assert (answer["state"], answer["ok"]) == ("done", False)
    assert "occupied" in answer["message"]


def test_tray_initiated_stop_never_counts_as_an_unexpected_server_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    facade, monitor, sink = _watched(install, monkeypatch)

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
    # The activity names the tray's own stop, never a crash.
    assert _activity(install) == ["server_stopped"]


def test_server_end_without_shutdown_is_logged_where_the_server_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    _facade, monitor, _sink = _watched(install, monkeypatch)
    data_dir = install.root / "data"
    # This test process stands in for the server process its control record names.
    create_server_control(
        data_dir, 8420, process_create_time=process_started(psutil.Process()), token="t"
    )
    connected = MonitorStatus("u", "connected", True, "0.4.2", "2026-01-01T00:00:00.000000Z")
    _on_monitor_loop(lambda: monitor.listener.status_changed(connected))
    # The stream vanishes without a stop announcement, then nothing listens.
    monitor.listener.connection_lost(1006)
    monitor.listener.status_changed(MonitorStatus("u", "refused"))

    lines = resolve_daily_log_path(data_dir).read_text(encoding="utf-8").splitlines()
    [line] = [line for line in lines if " vbot.application.server_exit - " in line]
    fields = dict(re.findall(r"(\w+)=([^\s)]+)", line))
    assert "[WARN]" in line
    assert fields["pid"] == str(os.getpid())
    assert "unknown" not in (fields["started"], fields["uptime"])
    # The process still lives here, so it has no exit code yet.
    assert fields["exit_code"] == "unknown"
    journal = ActivityJournal(install.root / "logs" / JOURNAL_NAME)
    [crash] = [entry for entry in journal.entries() if entry.kind == "server_crashed"]
    assert str(os.getpid()) in crash.text


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
    assert _activity(facade._install) == ["action_failed"]

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


@pytest.mark.parametrize(
    ("server_host", "loopback"),
    [("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("::", "::1")],
)
def test_open_desktop_uses_the_versioned_desktop_host_and_shape_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_host: str, loopback: str
):
    # A wildcard bind is reached over loopback, where the local access token works.
    install = replace(_install(tmp_path, shape="server-desktop"), server_host=server_host)
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
        loopback,
        "--port",
        "8420",
        "--server-data-dir",
        str((tmp_path / "data").resolve()),
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
            [
                *("--host", "127.0.0.1", "--port", "8420", "--server-data-dir", "<data-dir>"),
                *("--open-session", "coder@site", "ses_1"),
            ],
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
    monkeypatch.setattr(
        desktop,
        "desktop_server",
        lambda: desktop.DesktopServer("192.0.2.8", 18420, "http://192.0.2.8:18420"),
    )

    host.ApplicationFacade(install).open_session("coder@site", "ses_1")

    if isinstance(opened, list):
        data_dir = str((tmp_path / "data").resolve())
        opened = [data_dir if item == "<data-dir>" else item for item in opened]
    assert launches == [opened]


def test_a_desktop_client_tray_follows_the_desktop_server_and_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from cli.sign_ins import SignIn, SignInStore
    from desktop.credentials import DEVICE_TOKENS_FILE_NAME, DeviceTokenStore
    from desktop.settings import config_dir, write_last_used

    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = _install(tmp_path, shape="desktop-client")
    executable = _desktop_interpreter(install)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    launches: list[object] = []
    monkeypatch.setattr(host.subprocess, "Popen", lambda args, **_kwargs: launches.append(args[3:]))
    facade, monitor, _sink = _watched(install, monkeypatch)
    assert monitor.target() is None

    write_last_used("vbot.example.com", 443, scheme="https")
    facade.open_session("coder@site", "ses_1")

    assert monitor.target().url == "https://vbot.example.com"
    assert launches == [
        ["--host", "https://vbot.example.com", "--port", "443", "--open-session", "coder@site"]
        + ["ses_1"]
    ]
    # The tray signs in as the Desktop; the CLI's own sign-in is the fallback.
    origin = "https://vbot.example.com"
    sign_in = monitor.device_sign_in
    assert sign_in is not None
    assert sign_in(origin) is None
    cli_token = "vbt_dev_00000000000000c1_" + "c" * 43
    desktop_device = "dev_00000000000000d2"
    desktop_token = f"vbt_{desktop_device}_" + "d" * 43
    SignInStore().save_sign_in(
        SignIn(origin, cli_token, "dev_00000000000000c1", "CLI", "2026-10-17T00:00:00Z")
    )
    assert sign_in(origin) == cli_token
    DeviceTokenStore(config_dir() / DEVICE_TOKENS_FILE_NAME).save_device(
        origin, desktop_token, desktop_device
    )
    assert sign_in(origin) == desktop_token

    write_last_used("192.0.2.8", 18420)
    assert monitor.target().url == "http://192.0.2.8:18420"


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
    ("successor", "timeout", "start", "recorded"),
    [
        pytest.param(None, 0, True, "tray_started", id="first-host"),
        # A restart successor waits for its predecessor and keeps the server as
        # it is, including a server the user stopped on purpose.
        pytest.param("1", 30, False, "tray_restarted", id="restart-successor"),
    ],
)
def test_main_recovers_before_the_tray_and_only_a_first_host_starts_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    successor: str | None,
    timeout: float,
    start: bool,
    recorded: str,
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
    # The tray starts the worker itself; it never hands it to another host.
    monkeypatch.setattr(
        host.operations,
        "recover_operations",
        lambda _install, *, host: order.append(f"recover:host={host}"),
    )
    # The tray shows at once and starts the server as its first action.
    monkeypatch.setattr(
        host,
        "run_tray",
        lambda _facade, _icon, *, start_server: order.append(f"tray:start={start_server}"),
    )

    assert host.main() == 0
    assert order == [
        f"lock:host:{timeout:g}",
        "logging",
        "recover:host=True",
        f"tray:start={start}",
        "close",
    ]
    assert _activity(install) == [recorded]


def test_main_keeps_tray_running_when_recovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install = _install(tmp_path)
    received: list[tuple[host.ApplicationFacade, bool]] = []

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
    monkeypatch.setattr(
        host.operations,
        "recover_operations",
        lambda _install, **_kwargs: (_ for _ in ()).throw(ApplicationError("record unreadable")),
    )
    monkeypatch.setattr(
        host,
        "run_tray",
        lambda facade, _icon, *, start_server: received.append((facade, start_server)),
    )

    assert host.main() == 0
    assert len(received) == 1
    facade, start_server = received[0]
    assert facade.state().error == "Startup failed: record unreadable"
    assert start_server is False


_TICKET_ID = "T" * 43
_TICKET = {"ticket": _TICKET_ID, "path": f"/auth/ticket/{_TICKET_ID}", "expires_in": 60}
_BROWSER_TICKET = ("auth.login_ticket", {"name": "Browser on desk"})


@pytest.mark.parametrize(
    ("action", "reply", "opened", "calls_made"),
    [
        pytest.param(
            "browser",
            _TICKET,
            f"https://vbot.example.com/auth/ticket/{_TICKET_ID}",
            [_BROWSER_TICKET],
            id="browser-signed-in",
        ),
        # The ticket lands the browser on the Session.
        pytest.param(
            "session",
            _TICKET,
            f"https://vbot.example.com/auth/ticket/{_TICKET_ID}",
            [
                (
                    "auth.login_ticket",
                    {
                        "name": "Browser on desk",
                        "next": "/?open_agent=coder%40site&open_session=ses_1",
                    },
                )
            ],
            id="session-signed-in",
        ),
        pytest.param(
            "browser",
            "auth_disabled",
            "http://127.0.0.1:8420",
            [_BROWSER_TICKET],
            id="login-turned-off",
        ),
        pytest.param("browser", "auth_required", ApplicationError, [_BROWSER_TICKET], id="refused"),
        # Only a sign-in link of the server's own shape is opened.
        pytest.param(
            "browser",
            {**_TICKET, "path": f"/auth/ticket/{_TICKET_ID}/../../@evil.example"},
            ApplicationError,
            [_BROWSER_TICKET],
            id="foreign-link",
        ),
        # An Agent's Run never mints a sign-in link.
        pytest.param("run", _TICKET, ApplicationError, [], id="inside-a-run"),
    ],
)
def test_a_browser_opens_signed_in_when_the_server_requires_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    reply: dict[str, Any] | str,
    opened: str | type[ApplicationError],
    calls_made: list[tuple[str, dict[str, Any]]],
):
    install = replace(
        _install(tmp_path),
        server_access=access_config(
            "authenticated", public_url="https://vbot.example.com", trusted_proxies=["127.0.0.1"]
        ),
    )
    target = SimpleNamespace(url="http://127.0.0.1:8420")
    calls: list[tuple[str, dict[str, Any]]] = []

    def rpc_call(instance: Any, method: str, params: dict[str, Any]) -> RpcPayload:
        assert instance is target
        calls.append((method, params))
        if isinstance(reply, dict):
            return RpcPayload(ok=True, instance=cast(Any, instance), data=reply)
        failure = RpcFailure(method, "responded", reply, 401)
        return RpcPayload(ok=False, instance=cast(Any, instance), message=reply, failure=failure)

    launches: list[str] = []
    monkeypatch.setattr(host.processes, "target", lambda _install: target)
    monkeypatch.setattr("cli.rpc_client.rpc_call", rpc_call)
    monkeypatch.setattr(host.webbrowser, "open", launches.append)
    monkeypatch.setattr(host.socket, "gethostname", lambda: "desk")
    if action == "run":
        monkeypatch.setenv("VBOT_RUN_AGENT_ID", "builder")
    facade = host.ApplicationFacade(install)

    def act() -> None:
        if action == "session":
            facade.open_session("coder@site", "ses_1")
        else:
            facade.open_browser()

    if opened is ApplicationError:
        with pytest.raises(ApplicationError):
            act()
    else:
        act()
    assert launches == ([] if opened is ApplicationError else [opened])
    assert calls == calls_made
