"""Tests for server management stop."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli import server_management
from cli.server_management import (
    HealthProbeResult,
    stop_server,
)
from tests.cli.server_management_test_support import (
    make_instance,
)


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


def test_stop_server_waits_for_control_process_after_listener_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    class FakeProcess:
        pid = 456

        @staticmethod
        def create_time() -> float:
            return 1000.25

        @staticmethod
        def wait(*, timeout: float) -> None:
            calls.append(("wait", timeout))

        @staticmethod
        def kill() -> None:
            raise AssertionError("normal shutdown completion must not be killed")

    health = HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")
    monkeypatch.setattr(server_management, "probe_health", lambda _instance: health)
    monkeypatch.setattr(
        server_management,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=456, process_create_time=1000.25),
    )
    monkeypatch.setattr(server_management.psutil, "Process", lambda _pid: FakeProcess())

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert result.ok is True
    assert result.message == "stopped"
    assert result.process_id == 456
    assert result.forced is False
    assert calls == [("wait", 2.0)]


def test_stop_server_ignores_reused_control_pid_after_listener_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    class ReusedProcess:
        pid = 456

        @staticmethod
        def create_time() -> float:
            return 2000.5

        @staticmethod
        def wait(*, timeout: float) -> None:
            raise AssertionError("a reused PID must not be waited on")

        @staticmethod
        def kill() -> None:
            raise AssertionError("a reused PID must not be killed")

    health = HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")
    monkeypatch.setattr(server_management, "probe_health", lambda _instance: health)
    monkeypatch.setattr(
        server_management,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=456, process_create_time=1000.25),
    )
    monkeypatch.setattr(server_management.psutil, "Process", lambda _pid: ReusedProcess())

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert result.ok is True
    assert result.message == "not running"
    assert result.process_id is None


def test_stop_server_kills_stuck_control_process_after_listener_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    class StuckProcess:
        pid = 456

        def __init__(self) -> None:
            self.waits = 0

        @staticmethod
        def create_time() -> float:
            return 1000.25

        def wait(self, *, timeout: float) -> None:
            self.waits += 1
            calls.append(("wait", timeout))
            if self.waits == 1:
                raise server_management.psutil.TimeoutExpired(timeout, self.pid)

        @staticmethod
        def kill() -> None:
            calls.append("kill")

    health = HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")
    monkeypatch.setattr(server_management, "probe_health", lambda _instance: health)
    monkeypatch.setattr(
        server_management,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=456, process_create_time=1000.25),
    )
    monkeypatch.setattr(server_management.psutil, "Process", lambda _pid: StuckProcess())

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert result.ok is True
    assert result.process_id == 456
    assert result.forced is True
    assert calls == [("wait", 2.0), "kill", ("wait", 2.0)]


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


def test_stop_server_waits_for_cooperative_runtime_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    class FakeProcess:
        pid = 456

        def terminate(self) -> None:
            raise AssertionError("cooperative shutdown must not terminate the process")

        def wait(self, *, timeout: float) -> None:
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        server_management,
        "probe_health",
        lambda instance: HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
    )
    monkeypatch.setattr(server_management, "find_listening_process", lambda instance: FakeProcess())
    monkeypatch.setattr(server_management, "_request_cooperative_shutdown", lambda *_args: True)

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert result.ok is True
    assert result.forced is False
    assert calls == [("wait", 2.0)]


def test_cooperative_shutdown_requires_control_record_for_listener_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    process = SimpleNamespace(pid=456)
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        server_management,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=456, token="secret"),
    )

    def post(url: str, **kwargs: Any) -> SimpleNamespace:
        posts.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(server_management.httpx, "post", post)

    assert server_management._request_cooperative_shutdown(instance, process) is True
    assert posts[0]["headers"] == {"X-VBot-Control-Token": "secret"}

    monkeypatch.setattr(
        server_management,
        "read_server_control",
        lambda *_args: SimpleNamespace(pid=999, token="secret"),
    )
    assert server_management._request_cooperative_shutdown(instance, process) is False
    assert len(posts) == 1


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


def test_stop_server_returns_failure_when_kill_wait_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[Any] = []

    class FakeProcess:
        pid = 789

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, *, timeout: float) -> None:
            calls.append(("wait", timeout))
            raise server_management.psutil.TimeoutExpired(timeout, self.pid)

    health = HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
    monkeypatch.setattr(server_management, "probe_health", lambda instance: health)
    monkeypatch.setattr(server_management, "find_listening_process", lambda instance: FakeProcess())

    result = stop_server(instance, shutdown_timeout_seconds=2.0)

    assert result.ok is False
    assert result.instance is instance
    assert result.health == health
    assert result.process_id == 789
    assert result.forced is True
    assert calls == ["terminate", ("wait", 2.0), "kill", ("wait", 2.0)]
