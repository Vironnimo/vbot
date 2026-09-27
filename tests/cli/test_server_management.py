"""Local server process lifecycle: spawn, start, stop, restart and the scheduled restart."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli import _server_target, server_management
from cli.server_management import (
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
    restart_server,
    start_server,
    start_server_process,
    stop_server,
)
from core.database import DataStoreMarker, read_marker
from core.utils import processes as process_utils
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR
from tests.cli.cli_test_support import make_instance

UNREACHABLE = HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")
VBOT = HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
FOREIGN = HealthProbeResult(reachable=True, is_vbot=False, status_code=200)
MANAGED_CLI_LOG_PATTERN = (
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(INFO|WARN|ERROR)\] "
    r"vbot\.cli\.server_management - .+$"
)
TERMINATED = ["terminate", ("wait", 0.5)]
KILLED = ["terminate", ("wait", 0.5), "kill", ("wait", 0.5)]


class FakeProcess:
    """A spawned or listening server process that records lifecycle calls.

    ``wait`` raises its timeout error for the first ``timeouts`` calls.
    """

    def __init__(
        self,
        *,
        pid: int = 456,
        timeouts: int = 0,
        timeout_error: Callable[[float], Exception] = server_management.psutil.TimeoutExpired,
        exit_code: int | None = None,
        create_time: float = 1000.25,
    ) -> None:
        self.pid = pid
        self.calls: list[Any] = []
        self._timeouts = timeouts
        self._timeout_error = timeout_error
        self._exit_code = exit_code
        self._create_time = create_time

    def poll(self) -> int | None:
        return self._exit_code

    def create_time(self) -> float:
        return self._create_time

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, *, timeout: float) -> None:
        self.calls.append(("wait", timeout))
        if self._timeouts:
            self._timeouts -= 1
            raise self._timeout_error(timeout)


def answer_health(monkeypatch: pytest.MonkeyPatch, *answers: HealthProbeResult) -> None:
    """Answer successive health probes in order."""

    remaining = iter(answers)
    monkeypatch.setattr(server_management, "probe_health", lambda _instance: next(remaining))


@pytest.mark.parametrize(
    ("platform", "creationflags", "new_session"),
    [("linux", 0, True), ("win32", 0x09000200, False)],
)
def test_start_server_process_spawns_a_detached_quiet_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    creationflags: int,
    new_session: bool,
) -> None:
    instance = make_instance(tmp_path, port=8765)
    calls: list[dict[str, Any]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            calls.append({"args": args, **kwargs})
            self.pid = 123

    monkeypatch.setattr(server_management.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(server_management.sys, "executable", "python-test")
    monkeypatch.setattr(server_management.sys, "platform", platform)
    monkeypatch.setattr(process_utils, "_windows_explicit_breakaway_allowed", lambda: True)
    for name, value in [
        ("CREATE_NEW_PROCESS_GROUP", 0x00000200),
        ("CREATE_NO_WINDOW", 0x08000000),
        ("CREATE_BREAKAWAY_FROM_JOB", 0x01000000),
    ]:
        monkeypatch.setattr(server_management.subprocess, name, value, raising=False)

    process = start_server_process(instance)

    assert process.pid == 123
    [call] = calls
    assert call["args"] == [
        "python-test",
        "-m",
        "server.main",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--data-dir",
        str(instance.data_dir),
    ]
    assert call["stdin"] == call["stdout"] == call["stderr"] == subprocess.DEVNULL
    assert call["env"][CONSOLE_LOGGING_ENV_VAR] == "0"
    assert call["creationflags"] == creationflags
    assert call["start_new_session"] is new_session
    # The server writes its own log; spawning must not create it.
    assert instance.log_path.exists() is False


@pytest.mark.parametrize(
    ("health", "ok", "message", "webui"),
    [
        pytest.param(VBOT, True, "already running", WebUIProbeResult(False, 404), id="vbot"),
        pytest.param(FOREIGN, False, "port occupied by non-vBot process", None, id="foreign"),
    ],
)
def test_start_server_never_spawns_when_the_port_already_answers(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    health: HealthProbeResult,
    ok: bool,
    message: str,
    webui: WebUIProbeResult | None,
) -> None:
    answer_health(monkeypatch, health)
    monkeypatch.setattr(
        server_management, "probe_webui", lambda _instance: WebUIProbeResult(False, 404)
    )
    monkeypatch.setattr(
        server_management,
        "start_server_process",
        lambda _instance: pytest.fail("an answering port must not spawn a server"),
    )

    result = start_server(instance)

    assert (result.ok, result.message, result.health, result.webui) == (ok, message, health, webui)
    assert result.instance is instance
    assert result.log_path == instance.log_path


@pytest.mark.parametrize("data_dir_exists", [False, True], ids=["missing-root", "existing-root"])
def test_start_server_waits_for_health_and_logs_its_lifecycle(
    instance: ServerInstance, monkeypatch: pytest.MonkeyPatch, data_dir_exists: bool
) -> None:
    if data_dir_exists:
        instance.data_dir.mkdir()
    markers_at_spawn: list[DataStoreMarker | None] = []

    def spawn(target: ServerInstance) -> FakeProcess:
        # The server refuses an existing root without the bootstrap marker, so a root the
        # CLI creates must carry the marker before the child starts.
        markers_at_spawn.append(read_marker(target.data_dir))
        return FakeProcess(pid=4321)

    answer_health(monkeypatch, UNREACHABLE, UNREACHABLE, VBOT)
    monkeypatch.setattr(
        server_management, "probe_webui", lambda _instance: WebUIProbeResult(True, 200)
    )
    monkeypatch.setattr(server_management, "start_server_process", spawn)

    result = start_server(instance, startup_timeout_seconds=1.0, probe_interval_seconds=0.0)

    assert (result.ok, result.message, result.process_id) == (True, "started", 4321)
    assert (result.health, result.webui) == (VBOT, WebUIProbeResult(True, 200))
    assert result.instance is instance
    assert result.log_path == instance.log_path
    if data_dir_exists:
        # Only the server decides about an existing root; the CLI adds just its logs.
        assert markers_at_spawn == [None]
        assert sorted(path.name for path in instance.data_dir.iterdir()) == ["logs"]
    else:
        [marker] = markers_at_spawn
        assert marker is not None and marker.databases == {}
    log_lines = instance.log_path.read_text(encoding="utf-8").splitlines()
    assert log_lines
    assert all(re.match(MANAGED_CLI_LOG_PATTERN, line) for line in log_lines)
    for event in [
        "Starting CLI-managed background server at http://127.0.0.1:8420",
        "Started CLI-managed background server process 4321",
        "CLI-managed background server became ready at http://127.0.0.1:8420",
    ]:
        assert any(event in line for line in log_lines), event


@pytest.mark.parametrize(
    ("timeout", "later_health", "child", "message", "calls"),
    [
        pytest.param(
            0.0,
            [],
            {},
            "server readiness timed out",
            TERMINATED,
            id="timeout-terminates",
        ),
        pytest.param(
            0.0,
            [],
            {"timeouts": 1},
            "server readiness timed out",
            KILLED,
            id="terminate-timeout-kills",
        ),
        pytest.param(
            0.0,
            [],
            {"timeouts": 2},
            "server readiness timed out",
            KILLED,
            id="kill-timeout-keeps-the-failure",
        ),
        pytest.param(
            1.0,
            [FOREIGN],
            {},
            "port occupied by non-vBot process",
            TERMINATED,
            id="non-vbot-appears",
        ),
        pytest.param(
            1.0,
            [UNREACHABLE],
            {"exit_code": 1},
            "server process exited before readiness",
            [],
            id="child-exits",
        ),
    ],
)
def test_start_server_cleans_up_a_child_that_never_became_ready(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    timeout: float,
    later_health: list[HealthProbeResult],
    child: dict[str, Any],
    message: str,
    calls: list[Any],
) -> None:
    process = FakeProcess(
        pid=654, timeout_error=lambda seconds: subprocess.TimeoutExpired("server", seconds), **child
    )
    answer_health(monkeypatch, UNREACHABLE, *later_health)
    monkeypatch.setattr(server_management, "start_server_process", lambda _instance: process)

    result = start_server(instance, startup_timeout_seconds=timeout, probe_interval_seconds=0.0)

    assert (result.ok, result.message, result.process_id) == (False, message, 654)
    assert result.health == (later_health or [UNREACHABLE])[-1]
    assert result.instance is instance
    assert result.log_path == instance.log_path
    assert process.calls == calls


@pytest.mark.parametrize("host", ["192.0.2.40", "remote.example"])
def test_stop_server_never_controls_a_process_for_a_remote_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    instance = make_instance(tmp_path, host=host)
    process = FakeProcess()
    # A local wildcard listener on the port and a matching control record must not
    # make a remote target local.
    listener = SimpleNamespace(
        status=server_management.psutil.CONN_LISTEN,
        laddr=SimpleNamespace(ip="0.0.0.0", port=instance.port),
    )
    process_with_listener = SimpleNamespace(pid=456, net_connections=lambda kind: [listener])
    monkeypatch.setattr(
        _server_target.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("192.0.2.40", 0))],
    )
    monkeypatch.setattr(
        server_management.psutil,
        "net_if_addrs",
        lambda: {"ethernet": [SimpleNamespace(family=2, address="192.0.2.10")]},
    )
    monkeypatch.setattr(server_management, "probe_health", lambda _instance: VBOT)
    monkeypatch.setattr(server_management.psutil, "process_iter", lambda: [process_with_listener])
    monkeypatch.setattr(server_management.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(
        server_management,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=456, process_create_time=1000.25),
    )

    result = stop_server(instance)

    assert (result.ok, result.message) == (False, "server stop requires a local target")
    assert process.calls == []


def test_stop_server_does_not_inspect_processes_behind_a_non_vbot_answer(
    instance: ServerInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_health(monkeypatch, FOREIGN)
    monkeypatch.setattr(
        server_management,
        "find_listening_process",
        lambda _instance: pytest.fail("must not inspect process before vBot confirmation"),
    )

    result = stop_server(instance)

    assert (result.ok, result.message) == (False, "port occupied by non-vBot process")


@pytest.mark.parametrize(
    ("cooperative", "timeouts", "ok", "message", "forced", "calls"),
    [
        pytest.param(False, 0, True, "stopped", False, ["terminate", ("wait", 2.0)], id="term"),
        pytest.param(True, 0, True, "stopped", False, [("wait", 2.0)], id="cooperative"),
        pytest.param(
            False,
            1,
            True,
            "stopped",
            True,
            ["terminate", ("wait", 2.0), "kill", ("wait", 2.0)],
            id="terminate-timeout-kills",
        ),
        pytest.param(
            False,
            2,
            False,
            "forced termination timed out",
            True,
            ["terminate", ("wait", 2.0), "kill", ("wait", 2.0)],
            id="kill-timeout-fails",
        ),
    ],
)
def test_stop_server_shuts_down_the_confirmed_vbot_listener(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    cooperative: bool,
    timeouts: int,
    ok: bool,
    message: str,
    forced: bool,
    calls: list[Any],
) -> None:
    process = FakeProcess(pid=789, timeouts=timeouts)
    answer_health(monkeypatch, VBOT)
    monkeypatch.setattr(server_management, "find_listening_process", lambda _instance: process)
    monkeypatch.setattr(
        server_management, "_request_cooperative_shutdown", lambda *_args: cooperative
    )

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert (result.ok, result.message, result.forced) == (ok, message, forced)
    assert (result.process_id, result.health, result.instance) == (789, VBOT, instance)
    assert process.calls == calls


@pytest.mark.parametrize(
    ("control", "recorded", "message", "process_id", "forced", "calls"),
    [
        pytest.param(True, {}, "stopped", 456, False, [("wait", 2.0)], id="exits-in-time"),
        pytest.param(
            True,
            {"timeouts": 1},
            "stopped",
            456,
            True,
            [("wait", 2.0), "kill", ("wait", 2.0)],
            id="stuck-is-killed",
        ),
        pytest.param(
            True, {"create_time": 2000.5}, "not running", None, False, [], id="reused-pid"
        ),
        pytest.param(False, {}, "not running", None, False, [], id="no-record"),
    ],
)
def test_stop_server_finishes_only_the_recorded_process_after_the_listener_closed(
    instance: ServerInstance,
    monkeypatch: pytest.MonkeyPatch,
    control: bool,
    recorded: dict[str, Any],
    message: str,
    process_id: int | None,
    forced: bool,
    calls: list[Any],
) -> None:
    process = FakeProcess(**recorded)
    record = SimpleNamespace(pid=456, process_create_time=1000.25) if control else None
    answer_health(monkeypatch, UNREACHABLE)
    monkeypatch.setattr(server_management, "read_server_control", lambda *_args: record)
    monkeypatch.setattr(server_management.psutil, "Process", lambda _pid: process)

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert (result.ok, result.message, result.process_id) == (True, message, process_id)
    assert result.forced is forced
    assert process.calls == calls


def test_cooperative_shutdown_requires_control_record_for_listener_pid(
    instance: ServerInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = SimpleNamespace(pid=456)
    posts: list[dict[str, Any]] = []
    record = SimpleNamespace(pid=456, token="secret")
    monkeypatch.setattr(server_management, "read_server_control", lambda *_args: record)

    def post(url: str, **kwargs: Any) -> SimpleNamespace:
        posts.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(server_management.httpx, "post", post)

    assert server_management._request_cooperative_shutdown(instance, process) is True
    assert posts[0]["headers"] == {"X-VBot-Control-Token": "secret"}

    record.pid = 999
    assert server_management._request_cooperative_shutdown(instance, process) is False
    assert len(posts) == 1


@pytest.mark.parametrize(
    ("stop_ok", "start_ok", "ok", "message", "events"),
    [
        pytest.param(True, True, True, "restarted", ["stop", "start"], id="restarted"),
        pytest.param(
            False,
            True,
            False,
            "restart aborted: port occupied by non-vBot process",
            ["stop"],
            id="stop-fails",
        ),
        pytest.param(
            True,
            False,
            False,
            "restart failed: server readiness timed out",
            ["stop", "start"],
            id="start-fails",
        ),
    ],
)
def test_restart_server_stops_then_starts_an_unmanaged_server(
    instance: ServerInstance,
    stop_ok: bool,
    start_ok: bool,
    ok: bool,
    message: str,
    events: list[str],
) -> None:
    calls: list[str] = []

    def stop(target: ServerInstance) -> CommandResult:
        calls.append("stop")
        stopped = "stopped" if stop_ok else "port occupied by non-vBot process"
        return CommandResult(ok=stop_ok, message=stopped, instance=target)

    def start(target: ServerInstance) -> CommandResult:
        calls.append("start")
        started = "started" if start_ok else "server readiness timed out"
        return CommandResult(ok=start_ok, message=started, instance=target, process_id=7)

    result = restart_server(
        instance, stop=stop, start=start, is_managed=lambda _instance, _name: False
    )

    assert (result.ok, result.message) == (ok, message)
    assert calls == events


def test_restart_server_routes_a_systemd_managed_server_through_its_unit(
    instance: ServerInstance,
) -> None:
    def do_restart(target: ServerInstance, name: str) -> CommandResult:
        return CommandResult(ok=True, message=f"restarted via systemd ({name})", instance=target)

    result = restart_server(
        instance,
        service_name="vbot",
        stop=lambda _target: pytest.fail("managed stop must not run on a systemd install"),
        is_managed=lambda _instance, _name: True,
        do_restart=do_restart,
    )

    assert (result.ok, result.message) == (True, "restarted via systemd (vbot)")


@pytest.mark.parametrize("service_name", ["../../outside", "--system"])
def test_restart_server_rejects_an_unsafe_service_name_before_lifecycle_work(
    instance: ServerInstance, service_name: str
) -> None:
    def unexpected(_instance: ServerInstance) -> CommandResult:
        raise AssertionError("an invalid service name must fail before server lifecycle work")

    result = restart_server(instance, service_name=service_name, stop=unexpected, start=unexpected)

    assert result.ok is False
    assert result.message.startswith("invalid systemd service name")


def test_schedule_server_restart_detaches_exact_target_and_strips_run_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path, port=9001)
    captured: dict[str, Any] = {}
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "main")
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "session-1")

    def open_process(arguments: list[str], environment: dict[str, str]) -> SimpleNamespace:
        captured["arguments"] = arguments
        captured["environment"] = environment
        return SimpleNamespace(pid=7654)

    monkeypatch.setattr(server_management, "_open_scheduled_restart_process", open_process)

    result = server_management.schedule_server_restart(
        instance, service_name="vbot-test", wait_pid=4321
    )

    assert result.ok is True
    assert "7654" in result.message
    assert captured["arguments"] == [
        server_management.sys.executable,
        "-m",
        "cli.server_management",
        "--scheduled-restart",
        "--wait-pid",
        "4321",
        "--host",
        "127.0.0.1",
        "--port",
        "9001",
        "--data-dir",
        str(instance.data_dir),
        "--service-name",
        "vbot-test",
    ]
    assert not any(key.startswith("VBOT_RUN_") for key in captured["environment"])


def test_vbot_run_context_requires_agent_and_session_identity() -> None:
    assert server_management.has_vbot_run_context({}) is False
    assert server_management.has_vbot_run_context({"VBOT_RUN_AGENT_ID": "main"}) is False
    assert (
        server_management.has_vbot_run_context(
            {"VBOT_RUN_AGENT_ID": "main", "VBOT_RUN_SESSION_ID": "session-1"}
        )
        is True
    )


def test_scheduled_restart_waits_then_runs_once_and_logs_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path, port=9001)
    events: list[object] = []

    class FakeCaller:
        def wait(self, *, timeout: float) -> None:
            events.append(("wait", timeout))

    class FakeLogger:
        def info(self, message: str, value: str) -> None:
            events.append(("log", message, value))

        def error(self, message: str, value: str) -> None:
            events.append(("error", message, value))

    class FakeManager:
        def get_logger(self, _name: str) -> FakeLogger:
            return FakeLogger()

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(server_management, "resolve_instance", lambda **_kwargs: instance)
    monkeypatch.setattr(
        server_management, "_create_cli_log_manager", lambda _instance: FakeManager()
    )
    monkeypatch.setattr(server_management.psutil, "Process", lambda _pid: FakeCaller())
    monkeypatch.setattr(
        server_management.time, "sleep", lambda seconds: events.append(("sleep", seconds))
    )
    monkeypatch.setattr(
        server_management,
        "restart_server",
        lambda target, *, service_name: CommandResult(
            ok=True, message=f"restarted {service_name}", instance=target
        ),
    )

    result = server_management._run_scheduled_restart(
        [
            "--scheduled-restart",
            "--wait-pid",
            "4321",
            "--host",
            "127.0.0.1",
            "--port",
            "9001",
            "--data-dir",
            str(instance.data_dir),
            "--service-name",
            "vbot-test",
        ]
    )

    assert result == 0
    assert events == [
        ("wait", 60.0),
        ("sleep", 0.5),
        ("log", "Scheduled update restart result: %s", "restarted vbot-test"),
        "close",
    ]
