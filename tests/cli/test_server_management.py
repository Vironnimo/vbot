"""Tests for CLI server lifecycle primitives."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from cli import server_management
from cli.server_management import (
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
    find_listening_process,
    get_status,
    is_systemd_managed,
    probe_health,
    probe_webui,
    resolve_instance,
    restart_server,
    restart_via_systemd_if_managed,
    start_server,
    start_server_process,
    stop_server,
)
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR, resolve_daily_log_path

MANAGED_CLI_LOG_PATTERN = (
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(INFO|WARN|ERROR)\] "
    r"vbot\.cli\.server_management - .+$"
)


def make_instance(tmp_path: Path, *, port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url=f"http://127.0.0.1:{port}",
        log_path=resolve_daily_log_path(data_dir),
    )


def test_resolve_instance_uses_explicit_port_before_environment_and_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(json.dumps({"server_port": 8500}), encoding="utf-8")
    monkeypatch.setenv("VBOT_SERVER_PORT", "8600")

    instance = resolve_instance(host="localhost", port=8700, data_dir=data_dir)

    assert instance.host == "localhost"
    assert instance.port == 8700
    assert instance.url == "http://localhost:8700"
    assert instance.data_dir == data_dir.resolve()
    assert instance.log_path == resolve_daily_log_path(data_dir.resolve())


def test_resolve_instance_uses_environment_before_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(json.dumps({"server_port": 8500}), encoding="utf-8")
    monkeypatch.setenv("VBOT_SERVER_PORT", "8600")

    instance = resolve_instance(data_dir=data_dir)

    assert instance.port == 8600


def test_resolve_instance_uses_settings_before_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VBOT_SERVER_PORT", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(json.dumps({"server_port": 8500}), encoding="utf-8")

    assert resolve_instance(data_dir=data_dir).port == 8500
    assert resolve_instance(data_dir=tmp_path / "missing").port == 8420


def test_probe_health_classifies_exact_health_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    monkeypatch.setattr(
        server_management.httpx,
        "get",
        lambda url, *, timeout, trust_env: httpx.Response(200, json={"status": "ok"}),
    )

    result = probe_health(instance)

    assert result == HealthProbeResult(reachable=True, is_vbot=True, status_code=200)


def test_probe_health_uses_direct_loopback_without_proxy_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = ServerInstance(
        host="0.0.0.0",
        port=8420,
        data_dir=tmp_path / "data",
        url="http://0.0.0.0:8420",
        log_path=resolve_daily_log_path((tmp_path / "data").resolve()),
    )
    captured: dict[str, object] = {}

    def fake_get(url, *, timeout, trust_env):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["trust_env"] = trust_env
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(server_management.httpx, "get", fake_get)

    result = probe_health(instance)

    assert result == HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
    assert captured == {
        "url": "http://127.0.0.1:8420/health",
        "timeout": server_management.DEFAULT_PROBE_TIMEOUT_SECONDS,
        "trust_env": False,
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"status": "ok", "extra": True}),
        httpx.Response(200, json={"status": "up"}),
        httpx.Response(503, json={"status": "ok"}),
        httpx.Response(200, content=b"not-json"),
    ],
)
def test_probe_health_rejects_non_vbot_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management.httpx,
        "get",
        lambda url, *, timeout, trust_env: response,
    )

    result = probe_health(instance)

    assert result.reachable is True
    assert result.is_vbot is False


def test_probe_health_reports_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def raise_connect_error(url, *, timeout, trust_env):
        request = httpx.Request("GET", url)
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(server_management.httpx, "get", raise_connect_error)

    result = probe_health(instance)

    assert result.reachable is False
    assert result.is_vbot is False
    assert result.error == "ConnectError"


@pytest.mark.parametrize(
    ("status_code", "available"),
    [(200, True), (302, True), (404, False), (500, False)],
)
def test_probe_webui_classifies_root_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    available: bool,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management.httpx,
        "get",
        lambda url, *, timeout, trust_env: httpx.Response(status_code),
    )

    result = probe_webui(instance)

    assert result == WebUIProbeResult(available=available, status_code=status_code)


def test_probe_webui_uses_direct_request_without_proxy_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: dict[str, object] = {}

    def fake_get(url, *, timeout, trust_env):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["trust_env"] = trust_env
        return httpx.Response(200)

    monkeypatch.setattr(server_management.httpx, "get", fake_get)

    result = probe_webui(instance)

    assert result == WebUIProbeResult(available=True, status_code=200)
    assert captured == {
        "url": "http://127.0.0.1:8420/",
        "timeout": server_management.DEFAULT_PROBE_TIMEOUT_SECONDS,
        "trust_env": False,
    }


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
    assert result.message == "port occupied by non-vBot process"


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

    assert result == CommandResult(
        ok=True,
        message="already running",
        instance=instance,
        health=HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
        webui=WebUIProbeResult(False, 404),
        log_path=instance.log_path,
    )


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

    assert result == CommandResult(
        ok=True,
        message="started",
        instance=instance,
        health=HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
        webui=WebUIProbeResult(True, 200),
        log_path=instance.log_path,
        process_id=321,
    )


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

    assert result.message == "started"
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

    assert result == CommandResult(
        ok=False,
        message="server readiness timed out",
        instance=instance,
        health=HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
        log_path=instance.log_path,
        process_id=654,
    )


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

    assert result.message == "server readiness timed out"
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

    assert result.message == "server readiness timed out"
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

    assert result.message == "port occupied by non-vBot process"
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


def test_stop_server_does_not_terminate_non_vbot_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
    )
    monkeypatch.setattr(
        server_management,
        "find_listening_process",
        lambda instance: pytest.fail("must not inspect process before vBot confirmation"),
    )

    result = stop_server(instance)

    assert result.ok is False
    assert result.message == "port occupied by non-vBot process"


def test_stop_server_terminates_confirmed_vbot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    class FakeProcess:
        pid = 456

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, *, timeout: float) -> None:
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
    )
    monkeypatch.setattr(server_management, "find_listening_process", lambda instance: FakeProcess())

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert result.ok is True
    assert result.forced is False
    assert result.process_id == 456
    assert calls == ["terminate", ("wait", 2.0)]


def test_stop_server_kills_after_terminate_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    class FakeProcess:
        pid = 789

        def __init__(self) -> None:
            self.waits = 0

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, *, timeout: float) -> None:
            self.waits += 1
            calls.append(("wait", timeout))
            if self.waits == 1:
                raise server_management.psutil.TimeoutExpired(timeout, self.pid)

    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
    )
    monkeypatch.setattr(server_management, "find_listening_process", lambda instance: FakeProcess())

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert result.ok is True
    assert result.forced is True
    assert calls == ["terminate", ("wait", 2.0), "kill", ("wait", 2.0)]


def test_get_status_reports_running_with_webui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
    )
    monkeypatch.setattr(
        server_management, "probe_webui", lambda instance: WebUIProbeResult(True, 200)
    )

    result = get_status(instance)

    assert result.ok is True
    assert result.message == "running"
    assert result.webui == WebUIProbeResult(True, 200)


def test_get_status_reports_non_vbot_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
    )

    result = get_status(instance)

    assert result.ok is False
    assert result.message == "port occupied by non-vBot process"
    assert result.webui == WebUIProbeResult(False)


def test_get_status_reports_not_running_with_webui_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
    )

    result = get_status(instance)

    assert result.ok is True
    assert result.message == "not running"
    assert result.webui == WebUIProbeResult(False)


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

    assert start_result.message == "started"
    assert start_result.process_id == 987
    assert start_result.webui == WebUIProbeResult(False, 404)
    assert status_result.message == "running"
    assert status_result.webui == WebUIProbeResult(False, 404)
    assert stop_result.message == "stopped"
    assert stop_result.forced is False
    assert calls == ["health", "spawn", "health", "health", "health", "terminate", "wait:2.0"]


def test_restart_server_managed_path_stops_then_starts(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    events: list[str] = []

    def stop(resolved: ServerInstance) -> CommandResult:
        events.append("stop")
        return CommandResult(ok=True, message="stopped", instance=resolved)

    def start(resolved: ServerInstance) -> CommandResult:
        events.append("start")
        return CommandResult(ok=True, message="started", instance=resolved)

    result = restart_server(instance, stop=stop, start=start, is_managed=lambda _name: False)

    assert result.ok
    assert result.message == "restarted"
    assert events == ["stop", "start"]


def test_restart_server_managed_aborts_when_stop_fails(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def stop(resolved: ServerInstance) -> CommandResult:
        return CommandResult(
            ok=False, message="port occupied by non-vBot process", instance=resolved
        )

    def start(resolved: ServerInstance) -> CommandResult:
        raise AssertionError("must not start after a failed stop")

    result = restart_server(instance, stop=stop, start=start, is_managed=lambda _name: False)

    assert not result.ok
    assert "restart aborted" in result.message


def test_restart_server_uses_systemd_when_unit_managed(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def stop(resolved: ServerInstance) -> CommandResult:
        raise AssertionError("managed stop must not run on a systemd install")

    def do_restart(resolved: ServerInstance, name: str) -> CommandResult:
        return CommandResult(ok=True, message=f"restarted via systemd ({name})", instance=resolved)

    result = restart_server(
        instance,
        service_name="vbot",
        stop=stop,
        is_managed=lambda _name: True,
        do_restart=do_restart,
    )

    assert result.ok
    assert "via systemd" in result.message


def test_restart_via_systemd_returns_none_when_unmanaged(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    assert restart_via_systemd_if_managed(instance, is_managed=lambda _name: False) is None


def test_is_systemd_managed_false_off_linux(tmp_path: Path) -> None:
    def runner(_args: list[str]) -> server_management._SystemctlRun:
        raise AssertionError("systemctl must not run on a non-linux host")

    assert is_systemd_managed("vbot", platform="win32", runner=runner, unit_dir=tmp_path) is False


def test_is_systemd_managed_requires_unit_file_then_active(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> server_management._SystemctlRun:
        calls.append(args)
        return server_management._SystemctlRun(returncode=0, stdout="active", stderr="")

    # No unit file yet -> not managed, and systemctl is never probed.
    assert is_systemd_managed("vbot", platform="linux", runner=runner, unit_dir=tmp_path) is False
    assert calls == []

    (tmp_path / "vbot.service").write_text("[Unit]\n", encoding="utf-8")
    assert is_systemd_managed("vbot", platform="linux", runner=runner, unit_dir=tmp_path) is True
    assert calls == [["systemctl", "--user", "is-active", "vbot.service"]]


def test_is_systemd_managed_false_when_inactive(tmp_path: Path) -> None:
    (tmp_path / "vbot.service").write_text("[Unit]\n", encoding="utf-8")

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        return server_management._SystemctlRun(returncode=3, stdout="inactive", stderr="")

    assert is_systemd_managed("vbot", platform="linux", runner=runner, unit_dir=tmp_path) is False


def test_systemd_restart_confirms_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    monkeypatch.setattr(
        server_management, "probe_webui", lambda _instance: WebUIProbeResult(True, 200)
    )
    healthy = HealthProbeResult(reachable=True, is_vbot=True, status_code=200)

    def runner(args: list[str]) -> server_management._SystemctlRun:
        assert args == ["systemctl", "--user", "restart", "vbot.service"]
        return server_management._SystemctlRun(returncode=0, stdout="", stderr="")

    result = server_management._systemd_restart(
        instance, "vbot", runner=runner, await_health=lambda _instance: healthy
    )

    assert result.ok
    assert result.message == "restarted via systemd"


def test_systemd_restart_reports_unit_failure(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        return server_management._SystemctlRun(returncode=1, stdout="", stderr="Unit not found")

    result = server_management._systemd_restart(instance, "vbot", runner=runner)

    assert not result.ok
    assert "failed" in result.message


def test_systemd_restart_unhealthy_after_restart(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    unhealthy = HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        return server_management._SystemctlRun(returncode=0, stdout="", stderr="")

    result = server_management._systemd_restart(
        instance, "vbot", runner=runner, await_health=lambda _instance: unhealthy
    )

    assert not result.ok
    assert "did not become healthy" in result.message
