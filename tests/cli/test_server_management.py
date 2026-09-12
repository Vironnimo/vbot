"""Tests for server management."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import server_management
from cli.server_management import (
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
    find_listening_process,
    get_status,
    resolve_instance,
    start_server,
    start_server_process,
    stop_server,
)
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR, resolve_daily_log_path
from tests.cli.server_management_test_support import (
    make_instance,
)

MANAGED_CLI_LOG_PATTERN = (
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(INFO|WARN|ERROR)\] "
    r"vbot\.cli\.server_management - .+$"
)


def test_start_server_process_uses_expected_args_and_log_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path, port=8765)
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs) -> None:
            calls.append({"args": args, "kwargs": kwargs})
            self.pid = 123

    monkeypatch.setattr(server_management.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(server_management.sys, "executable", "python-test")

    process = start_server_process(instance)

    assert process.pid == 123
    assert calls[0]["args"] == [
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
    assert calls[0]["kwargs"]["stdout"] == subprocess.DEVNULL
    assert calls[0]["kwargs"]["stderr"] == subprocess.DEVNULL
    assert calls[0]["kwargs"]["stdin"] == subprocess.DEVNULL
    assert calls[0]["kwargs"]["env"][CONSOLE_LOGGING_ENV_VAR] == "0"
    assert instance.log_path.exists() is False


def test_start_server_process_is_durable_and_windowless_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs) -> None:
            calls.append({"args": args, "kwargs": kwargs})
            self.pid = 123

    monkeypatch.setattr(server_management.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(server_management.sys, "platform", "win32")
    monkeypatch.setattr(
        server_management.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False
    )
    monkeypatch.setattr(server_management.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(
        server_management.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000, raising=False
    )

    start_server_process(instance)

    assert calls[0]["kwargs"]["creationflags"] == 0x09000200


def test_resolve_instance_uses_daily_log_file_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    instance = resolve_instance(data_dir=data_dir)

    assert instance.log_path == resolve_daily_log_path(data_dir.resolve())
    assert instance.log_path.suffix == ".log"


def test_start_server_does_not_spawn_when_non_vbot_occupies_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
    )

    def fail_start(unused_instance):
        raise AssertionError("non-vBot conflict must not spawn")

    monkeypatch.setattr(server_management, "start_server_process", fail_start)

    result = start_server(instance)

    assert result.ok is False
    assert result.health == HealthProbeResult(reachable=True, is_vbot=False, status_code=200)


def test_start_server_reports_already_running_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
    )
    monkeypatch.setattr(
        server_management, "probe_webui", lambda instance: WebUIProbeResult(False, 404)
    )

    def fail_start(unused_instance: ServerInstance) -> None:
        raise AssertionError("already-running vBot must not spawn")

    monkeypatch.setattr(server_management, "start_server_process", fail_start)

    result = start_server(instance)

    assert result.ok is True
    assert result.instance is instance
    assert result.health == HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
    assert result.webui == WebUIProbeResult(False, 404)
    assert result.log_path == instance.log_path


def test_start_server_waits_for_health_and_reports_webui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    health_results = iter(
        [
            HealthProbeResult(reachable=False, is_vbot=False),
            HealthProbeResult(reachable=False, is_vbot=False),
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
        ]
    )
    process = SimpleNamespace(pid=321, poll=lambda: None)
    monkeypatch.setattr(server_management, "probe_health", lambda instance: next(health_results))
    monkeypatch.setattr(
        server_management, "probe_webui", lambda instance: WebUIProbeResult(True, 200)
    )
    monkeypatch.setattr(server_management, "start_server_process", lambda instance: process)

    result = start_server(instance, startup_timeout_seconds=1.0, probe_interval_seconds=0.0)

    assert result.ok is True
    assert result.instance is instance
    assert result.health == HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
    assert result.webui == WebUIProbeResult(True, 200)
    assert result.log_path == instance.log_path
    assert result.process_id == 321


def test_start_server_preserves_managed_daily_log_without_raw_child_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    health_results = iter(
        [
            HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
        ]
    )
    process = SimpleNamespace(pid=4321, poll=lambda: None)

    monkeypatch.setattr(server_management, "probe_health", lambda instance: next(health_results))
    monkeypatch.setattr(
        server_management, "probe_webui", lambda instance: WebUIProbeResult(True, 200)
    )
    monkeypatch.setattr(server_management, "start_server_process", lambda instance: process)

    result = start_server(instance, startup_timeout_seconds=1.0, probe_interval_seconds=0.0)

    assert result.ok is True
    log_lines = instance.log_path.read_text(encoding="utf-8").splitlines()
    assert log_lines
    assert all(re.match(MANAGED_CLI_LOG_PATTERN, line) for line in log_lines)
    assert any(
        "Starting CLI-managed background server at http://127.0.0.1:8420" in line
        for line in log_lines
    )
    assert any("Started CLI-managed background server process 4321" in line for line in log_lines)
    assert any(
        "CLI-managed background server became ready at http://127.0.0.1:8420" in line
        for line in log_lines
    )
    assert all("raw child stderr" not in line for line in log_lines)


def test_start_server_reports_readiness_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    class FakeProcess:
        pid = 654

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, *, timeout: float) -> None:
            return None

    process = FakeProcess()
    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
    )
    monkeypatch.setattr(server_management, "start_server_process", lambda instance: process)

    result = start_server(instance, startup_timeout_seconds=0.0, probe_interval_seconds=0.0)

    assert result.ok is False
    assert result.instance is instance
    assert result.health == HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")
    assert result.log_path == instance.log_path
    assert result.process_id == 654


def test_start_server_cleans_up_spawned_process_after_readiness_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[str] = []

    class FakeProcess:
        pid = 654

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, *, timeout: float) -> None:
            calls.append(f"wait:{timeout}")

    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
    )
    monkeypatch.setattr(server_management, "start_server_process", lambda instance: FakeProcess())

    result = start_server(instance, startup_timeout_seconds=0.0, probe_interval_seconds=0.0)

    assert result.ok is False
    assert calls == ["terminate", "wait:0.5"]


def test_start_server_kills_spawned_process_when_cleanup_terminate_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[str] = []

    class FakeProcess:
        pid = 654

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, *, timeout: float) -> None:
            calls.append(f"wait:{timeout}")
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("server", timeout)

    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
    )
    monkeypatch.setattr(server_management, "start_server_process", lambda instance: FakeProcess())

    result = start_server(instance, startup_timeout_seconds=0.0, probe_interval_seconds=0.0)

    assert result.ok is False
    assert calls == ["terminate", "wait:0.5", "kill", "wait:0.5"]


def test_start_server_preserves_failure_when_cleanup_kill_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[str] = []

    class FakeProcess:
        pid = 654

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, *, timeout: float) -> None:
            calls.append(f"wait:{timeout}")
            raise subprocess.TimeoutExpired("server", timeout)

    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
    )
    monkeypatch.setattr(server_management, "start_server_process", lambda instance: FakeProcess())

    result = start_server(instance, startup_timeout_seconds=0.0, probe_interval_seconds=0.0)

    assert result.ok is False
    assert calls == ["terminate", "wait:0.5", "kill", "wait:0.5"]


def test_start_server_cleans_up_spawned_process_when_non_vbot_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    health_results = iter(
        [
            HealthProbeResult(reachable=False, is_vbot=False),
            HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
        ]
    )
    calls: list[str] = []

    class FakeProcess:
        pid = 654

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, *, timeout: float) -> None:
            calls.append(f"wait:{timeout}")

    monkeypatch.setattr(server_management, "probe_health", lambda instance: next(health_results))
    monkeypatch.setattr(server_management, "start_server_process", lambda instance: FakeProcess())

    result = start_server(instance, startup_timeout_seconds=1.0, probe_interval_seconds=0.0)

    assert result.ok is False
    assert calls == ["terminate", "wait:0.5"]


def test_find_listening_process_returns_port_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path, port=9001)
    matching_process = SimpleNamespace(
        net_connections=lambda kind: [
            SimpleNamespace(
                status=server_management.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9001)
            )
        ]
    )
    other_process = SimpleNamespace(
        net_connections=lambda kind: [
            SimpleNamespace(
                status=server_management.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9002)
            )
        ]
    )
    monkeypatch.setattr(
        server_management.psutil, "process_iter", lambda: [other_process, matching_process]
    )

    assert find_listening_process(instance) is matching_process


def test_find_listening_process_matches_requested_address_on_same_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path, port=9001)
    wrong_address_process = SimpleNamespace(
        net_connections=lambda kind: [
            SimpleNamespace(
                status=server_management.psutil.CONN_LISTEN,
                laddr=SimpleNamespace(ip="127.0.0.2", port=9001),
            )
        ]
    )
    matching_process = SimpleNamespace(
        net_connections=lambda kind: [
            SimpleNamespace(
                status=server_management.psutil.CONN_LISTEN,
                laddr=SimpleNamespace(ip="127.0.0.1", port=9001),
            )
        ]
    )
    monkeypatch.setattr(
        server_management.psutil,
        "process_iter",
        lambda: [wrong_address_process, matching_process],
    )

    assert find_listening_process(instance) is matching_process


def test_find_listening_process_matches_ipv4_wildcard_for_ipv4_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path, port=9001)
    wildcard_process = SimpleNamespace(
        net_connections=lambda kind: [
            SimpleNamespace(
                status=server_management.psutil.CONN_LISTEN,
                laddr=SimpleNamespace(ip="0.0.0.0", port=9001),
            )
        ]
    )
    monkeypatch.setattr(server_management.psutil, "process_iter", lambda: [wildcard_process])

    assert find_listening_process(instance) is wildcard_process


def test_find_listening_process_does_not_match_ipv6_wildcard_for_ipv4_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path, port=9001)
    ipv6_wildcard_process = SimpleNamespace(
        net_connections=lambda kind: [
            SimpleNamespace(
                status=server_management.psutil.CONN_LISTEN,
                laddr=SimpleNamespace(ip="::", port=9001),
            )
        ]
    )
    monkeypatch.setattr(server_management.psutil, "process_iter", lambda: [ipv6_wildcard_process])

    assert find_listening_process(instance) is None


def test_cli_lifecycle_smoke_with_faked_process_network_and_webui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate start/status/stop composition without real processes or sockets."""

    instance = make_instance(tmp_path, port=8765)
    health_checks = {"ready": False}
    calls: list[str] = []

    class FakeProcess:
        pid = 987

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, *, timeout: float) -> None:
            calls.append(f"wait:{timeout}")

    process = FakeProcess()

    def fake_probe_health(unused_instance: ServerInstance) -> HealthProbeResult:
        calls.append("health")
        if health_checks["ready"]:
            return HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
        return HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")

    def fake_start_process(unused_instance: ServerInstance) -> FakeProcess:
        calls.append("spawn")
        health_checks["ready"] = True
        return process

    monkeypatch.setattr(server_management, "probe_health", fake_probe_health)
    monkeypatch.setattr(server_management, "start_server_process", fake_start_process)
    monkeypatch.setattr(
        server_management, "probe_webui", lambda unused_instance: WebUIProbeResult(False, 404)
    )
    monkeypatch.setattr(
        server_management, "find_listening_process", lambda unused_instance: process
    )

    start_result = start_server(instance, startup_timeout_seconds=1.0, probe_interval_seconds=0.0)
    status_result = get_status(instance)
    stop_result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert start_result.ok is True
    assert start_result.process_id == 987
    assert start_result.webui == WebUIProbeResult(False, 404)
    assert status_result.ok is True
    assert status_result.webui == WebUIProbeResult(False, 404)
    assert stop_result.ok is True
    assert stop_result.forced is False
    assert calls == ["health", "spawn", "health", "health", "health", "terminate", "wait:2.0"]
