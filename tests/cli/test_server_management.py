"""Tests for CLI server lifecycle primitives."""

from __future__ import annotations

import json
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
    probe_health,
    probe_webui,
    resolve_instance,
    start_server,
    start_server_process,
    stop_server,
)


def make_instance(tmp_path: Path, *, port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url=f"http://127.0.0.1:{port}",
        log_path=data_dir / "logs" / "server.log",
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
    assert instance.log_path == data_dir.resolve() / "logs" / "server.log"


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
        lambda url, *, timeout: httpx.Response(200, json={"status": "ok"}),
    )

    result = probe_health(instance)

    assert result == HealthProbeResult(reachable=True, is_vbot=True, status_code=200)


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
    monkeypatch.setattr(server_management.httpx, "get", lambda url, *, timeout: response)

    result = probe_health(instance)

    assert result.reachable is True
    assert result.is_vbot is False


def test_probe_health_reports_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def raise_connect_error(url, *, timeout):
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
        lambda url, *, timeout: httpx.Response(status_code),
    )

    result = probe_webui(instance)

    assert result == WebUIProbeResult(available=available, status_code=status_code)


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
    assert instance.log_path.exists()
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
    assert calls[0]["kwargs"]["stderr"] == subprocess.STDOUT
    assert calls[0]["kwargs"]["stdin"] == subprocess.DEVNULL


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
