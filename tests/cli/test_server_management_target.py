"""Tests for server management target."""

from __future__ import annotations

import json
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
    get_status,
    probe_health,
    probe_webui,
    resolve_instance,
)
from core.utils.logging import resolve_daily_log_path
from tests.cli.server_management_test_support import (
    make_instance,
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


@pytest.mark.parametrize(
    ("host", "expected_url"),
    [
        ("::1", "http://[::1]:8420"),
        ("[::1]", "http://[::1]:8420"),
        ("::", "http://[::1]:8420"),
        ("0.0.0.0", "http://127.0.0.1:8420"),
    ],
)
def test_resolve_instance_builds_connectable_ipv6_safe_url(
    tmp_path: Path, host: str, expected_url: str
) -> None:
    instance = resolve_instance(host=host, port=8420, data_dir=tmp_path)

    assert instance.url == expected_url


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
    assert result.health == HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
    assert result.webui == WebUIProbeResult(True, 200)


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
        instance,
        service_name="vbot-test",
        wait_pid=4321,
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
    assert result.health == HealthProbeResult(reachable=True, is_vbot=False, status_code=200)
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
    assert result.health == HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")
    assert result.webui == WebUIProbeResult(False)
