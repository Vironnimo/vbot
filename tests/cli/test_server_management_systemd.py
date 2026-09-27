"""systemd user unit ownership and the systemctl-driven server lifecycle."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli import server_management
from cli.server_management import (
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
    is_systemd_managed,
    start_systemd_server,
    stop_systemd_server,
)
from tests.cli.cli_test_support import make_instance

SystemctlRun = server_management._SystemctlRun
HEALTHY = HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
UNHEALTHY = HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")


def write_systemd_unit(unit_dir: Path, instance: ServerInstance) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "vbot.service").write_text(
        "[Service]\n"
        f'ExecStart="/usr/bin/python3" "-m" "server.main" "--host" "{instance.host}" '
        f'"--port" "{instance.port}" "--data-dir" "{instance.data_dir.as_posix()}"\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("platform", "unit_port", "active", "expected"),
    [
        pytest.param("win32", 8420, "active", False, id="off-linux"),
        pytest.param("linux", None, "active", False, id="no-unit-file"),
        pytest.param("linux", 8422, "active", False, id="unit-for-another-instance"),
        pytest.param("linux", 8420, "inactive", False, id="inactive"),
        pytest.param("linux", 8420, "active", True, id="active"),
    ],
)
def test_is_systemd_managed_requires_an_active_unit_that_targets_the_instance(
    tmp_path: Path, platform: str, unit_port: int | None, active: str, expected: bool
) -> None:
    instance = make_instance(tmp_path)
    if unit_port is not None:
        write_systemd_unit(tmp_path, make_instance(tmp_path, port=unit_port))
    calls: list[list[str]] = []

    def runner(args: list[str]) -> SystemctlRun:
        calls.append(args)
        return SystemctlRun(returncode=0 if active == "active" else 3, stdout=active, stderr="")

    managed = is_systemd_managed(
        instance, "vbot", platform=platform, runner=runner, unit_dir=tmp_path
    )

    assert managed is expected
    # systemctl is probed only for a matching unit file on Linux.
    probed = platform == "linux" and unit_port == instance.port
    assert calls == ([["systemctl", "--user", "is-active", "vbot.service"]] if probed else [])


LifecycleCall = Callable[..., CommandResult]


@pytest.mark.parametrize(
    ("verb", "operation"),
    [("start", start_systemd_server), ("restart", server_management._systemd_restart)],
)
@pytest.mark.parametrize(
    ("returncode", "stderr", "health", "ok", "message"),
    [
        pytest.param(0, "", HEALTHY, True, "{verb}ed via systemd", id="healthy"),
        pytest.param(
            0,
            "",
            UNHEALTHY,
            False,
            "{verb}ed via systemd, but the server did not become healthy in time",
            id="unhealthy",
        ),
        pytest.param(
            124,
            "systemctl timed out after 30s",
            None,
            False,
            "systemctl --user {verb} vbot failed: systemctl timed out after 30s",
            id="unit-fails",
        ),
    ],
)
def test_systemd_start_and_restart_run_the_unit_and_confirm_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    operation: LifecycleCall,
    returncode: int,
    stderr: str,
    health: HealthProbeResult | None,
    ok: bool,
    message: str,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        server_management, "probe_webui", lambda _instance: WebUIProbeResult(True, 200)
    )

    def runner(args: list[str]) -> SystemctlRun:
        calls.append(args)
        return SystemctlRun(returncode=returncode, stdout="", stderr=stderr)

    def await_health(_instance: ServerInstance) -> HealthProbeResult:
        assert health is not None, "a failed unit command must not wait for health"
        return health

    result = operation(instance, "vbot", runner=runner, await_health=await_health)

    assert (result.ok, result.message) == (ok, message.format(verb=verb))
    assert result.health == health
    assert result.webui == (WebUIProbeResult(True, 200) if ok else None)
    assert calls == [["systemctl", "--user", verb, "vbot.service"]]


def test_systemd_stop_preserves_unit_and_reports_failure(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    calls: list[list[str]] = []

    def runner(arguments: list[str]) -> SystemctlRun:
        calls.append(arguments)
        return SystemctlRun(0, "", "")

    stopped = stop_systemd_server(instance, "vbot", runner=runner)
    failed = stop_systemd_server(
        instance, "vbot", runner=lambda _arguments: SystemctlRun(1, "", "permission denied")
    )

    assert (stopped.ok, stopped.message) == (True, "stopped via systemd")
    assert calls == [["systemctl", "--user", "stop", "vbot.service"]]
    assert (failed.ok, failed.message) == (
        False,
        "systemctl --user stop vbot failed: permission denied",
    )


def test_run_systemctl_lifecycle_gets_a_longer_timeout_than_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    def fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        captured[args[2]] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server_management.subprocess, "run", fake_run)

    for verb in ["restart", "stop", "is-active"]:
        server_management._run_systemctl(["systemctl", "--user", verb, "vbot.service"])

    assert captured["restart"] == server_management._SYSTEMCTL_RESTART_TIMEOUT_SECONDS
    assert captured["stop"] == server_management._SYSTEMCTL_RESTART_TIMEOUT_SECONDS
    assert captured["is-active"] == server_management._SYSTEMCTL_PROBE_TIMEOUT_SECONDS
    assert captured["restart"] > captured["is-active"]


def test_run_systemctl_decodes_captured_bytes_without_losing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_args: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="Łódź".encode(), stderr=b"\x81tail")

    monkeypatch.setattr(server_management.subprocess, "run", fake_run)

    result = server_management._run_systemctl(["systemctl", "--user", "is-active", "vbot.service"])

    assert result.stdout == "Łódź"
    assert result.stderr == r"\x81tail"


@pytest.mark.parametrize(
    ("error", "returncode", "stderr"),
    [
        pytest.param(
            subprocess.TimeoutExpired(cmd="systemctl", timeout=30.0),
            server_management._SYSTEMCTL_TIMEOUT_RETURN_CODE,
            "systemctl timed out after 30s",
            id="timeout",
        ),
        pytest.param(FileNotFoundError("systemctl"), 127, "systemctl unavailable", id="missing"),
    ],
)
def test_run_systemctl_tells_a_timeout_apart_from_a_missing_binary(
    monkeypatch: pytest.MonkeyPatch, error: Exception, returncode: int, stderr: str
) -> None:
    def fake_run(_args: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise error

    monkeypatch.setattr(server_management.subprocess, "run", fake_run)

    result = server_management._run_systemctl(["systemctl", "--user", "restart", "vbot.service"])

    assert (result.returncode, result.stderr) == (returncode, stderr)
