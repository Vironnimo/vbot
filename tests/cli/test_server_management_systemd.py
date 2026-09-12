"""Tests for server management systemd."""

from __future__ import annotations

import subprocess
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
    restart_server,
    restart_via_systemd_if_managed,
    start_systemd_server,
    stop_systemd_server,
)
from tests.cli.server_management_test_support import (
    make_instance,
)


def write_systemd_unit(unit_dir: Path, instance: ServerInstance) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "vbot.service").write_text(
        "[Service]\n"
        f'ExecStart="/usr/bin/python3" "-m" "server.main" "--host" "{instance.host}" '
        f'"--port" "{instance.port}" "--data-dir" "{instance.data_dir.as_posix()}"\n',
        encoding="utf-8",
    )


def test_restart_server_managed_path_stops_then_starts(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    events: list[str] = []

    def stop(resolved: ServerInstance) -> CommandResult:
        events.append("stop")
        return CommandResult(ok=True, message="stopped", instance=resolved)

    def start(resolved: ServerInstance) -> CommandResult:
        events.append("start")
        return CommandResult(ok=True, message="started", instance=resolved)

    result = restart_server(
        instance, stop=stop, start=start, is_managed=lambda _instance, _name: False
    )

    assert result.ok
    assert events == ["stop", "start"]


def test_restart_server_managed_aborts_when_stop_fails(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def stop(resolved: ServerInstance) -> CommandResult:
        return CommandResult(
            ok=False, message="port occupied by non-vBot process", instance=resolved
        )

    def start(resolved: ServerInstance) -> CommandResult:
        raise AssertionError("must not start after a failed stop")

    result = restart_server(
        instance, stop=stop, start=start, is_managed=lambda _instance, _name: False
    )

    assert not result.ok


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
        is_managed=lambda _instance, _name: True,
        do_restart=do_restart,
    )

    assert result.ok


def test_restart_server_rejects_unsafe_systemd_service_name(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def unexpected(_instance: ServerInstance) -> CommandResult:
        raise AssertionError("an invalid service name must fail before server lifecycle work")

    result = restart_server(
        instance,
        service_name="../../outside",
        stop=unexpected,
        start=unexpected,
    )

    assert not result.ok


def test_restart_server_rejects_option_like_systemd_service_name(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def unexpected(_instance: ServerInstance) -> CommandResult:
        raise AssertionError("an option-like name must fail before server lifecycle work")

    result = restart_server(
        instance,
        service_name="--system",
        stop=unexpected,
        start=unexpected,
    )

    assert not result.ok


def test_restart_via_systemd_returns_none_when_unmanaged(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    assert (
        restart_via_systemd_if_managed(instance, is_managed=lambda _instance, _name: False) is None
    )


def test_is_systemd_managed_false_off_linux(tmp_path: Path) -> None:
    def runner(_args: list[str]) -> server_management._SystemctlRun:
        raise AssertionError("systemctl must not run on a non-linux host")

    instance = make_instance(tmp_path)

    assert (
        is_systemd_managed(instance, "vbot", platform="win32", runner=runner, unit_dir=tmp_path)
        is False
    )


def test_is_systemd_managed_requires_unit_file_then_active(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    calls: list[list[str]] = []

    def runner(args: list[str]) -> server_management._SystemctlRun:
        calls.append(args)
        return server_management._SystemctlRun(returncode=0, stdout="active", stderr="")

    # No unit file yet -> not managed, and systemctl is never probed.
    assert (
        is_systemd_managed(instance, "vbot", platform="linux", runner=runner, unit_dir=tmp_path)
        is False
    )
    assert calls == []

    write_systemd_unit(tmp_path, instance)
    assert (
        is_systemd_managed(instance, "vbot", platform="linux", runner=runner, unit_dir=tmp_path)
        is True
    )
    assert calls == [["systemctl", "--user", "is-active", "vbot.service"]]


def test_is_systemd_managed_false_when_inactive(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    write_systemd_unit(tmp_path, instance)

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        return server_management._SystemctlRun(returncode=3, stdout="inactive", stderr="")

    assert (
        is_systemd_managed(instance, "vbot", platform="linux", runner=runner, unit_dir=tmp_path)
        is False
    )


def test_is_systemd_managed_rejects_active_unit_for_another_instance(tmp_path: Path) -> None:
    managed_instance = make_instance(tmp_path, port=8420)
    selected_instance = make_instance(tmp_path, port=8422)
    write_systemd_unit(tmp_path, managed_instance)

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        raise AssertionError("a mismatched unit must not be probed or controlled")

    assert (
        is_systemd_managed(
            selected_instance,
            "vbot",
            platform="linux",
            runner=runner,
            unit_dir=tmp_path,
        )
        is False
    )


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
    assert result.health == healthy


def test_systemd_restart_reports_unit_failure(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        return server_management._SystemctlRun(returncode=1, stdout="", stderr="Unit not found")

    result = server_management._systemd_restart(instance, "vbot", runner=runner)

    assert not result.ok


def test_systemd_restart_unhealthy_after_restart(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    unhealthy = HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError")

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        return server_management._SystemctlRun(returncode=0, stdout="", stderr="")

    result = server_management._systemd_restart(
        instance, "vbot", runner=runner, await_health=lambda _instance: unhealthy
    )

    assert not result.ok
    assert result.health == unhealthy


def test_systemd_stop_preserves_unit_and_reports_failure(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    calls: list[list[str]] = []

    def runner(arguments: list[str]) -> server_management._SystemctlRun:
        calls.append(arguments)
        return server_management._SystemctlRun(0, "", "")

    stopped = stop_systemd_server(instance, "vbot", runner=runner)
    failed = stop_systemd_server(
        instance,
        "vbot",
        runner=lambda _arguments: server_management._SystemctlRun(1, "", "permission denied"),
    )

    assert stopped.ok
    assert calls == [["systemctl", "--user", "stop", "vbot.service"]]
    assert not failed.ok
    assert "permission denied" in failed.message


def test_systemd_start_confirms_health(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)
    calls: list[list[str]] = []

    def runner(arguments: list[str]) -> server_management._SystemctlRun:
        calls.append(arguments)
        return server_management._SystemctlRun(0, "", "")

    result = start_systemd_server(
        instance,
        "vbot",
        runner=runner,
        await_health=lambda _instance: HealthProbeResult(reachable=True, is_vbot=True),
    )

    assert result.ok
    assert calls == [["systemctl", "--user", "start", "vbot.service"]]
    assert result.health is not None and result.health.is_vbot


def test_run_systemctl_lifecycle_gets_a_longer_timeout_than_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    def fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        captured[args[2]] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server_management.subprocess, "run", fake_run)

    server_management._run_systemctl(["systemctl", "--user", "restart", "vbot.service"])
    server_management._run_systemctl(["systemctl", "--user", "stop", "vbot.service"])
    server_management._run_systemctl(["systemctl", "--user", "is-active", "vbot.service"])

    assert captured["restart"] == server_management._SYSTEMCTL_RESTART_TIMEOUT_SECONDS
    assert captured["stop"] == server_management._SYSTEMCTL_RESTART_TIMEOUT_SECONDS
    assert captured["is-active"] == server_management._SYSTEMCTL_PROBE_TIMEOUT_SECONDS
    assert captured["restart"] > captured["is-active"]


def test_run_systemctl_decodes_captured_bytes_without_losing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_args: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="Łódź".encode(),
            stderr=b"\x81tail",
        )

    monkeypatch.setattr(server_management.subprocess, "run", fake_run)

    result = server_management._run_systemctl(["systemctl", "--user", "is-active", "vbot.service"])

    assert result.stdout == "Łódź"
    assert result.stderr == r"\x81tail"


def test_run_systemctl_timeout_is_reported_distinctly_from_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(server_management.subprocess, "run", fake_run)

    result = server_management._run_systemctl(["systemctl", "--user", "restart", "vbot.service"])

    assert result.returncode == server_management._SYSTEMCTL_TIMEOUT_RETURN_CODE
    assert "timed out" in result.stderr
    assert "unavailable" not in result.stderr


def test_run_systemctl_missing_binary_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_args: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(server_management.subprocess, "run", fake_run)

    result = server_management._run_systemctl(["systemctl", "--user", "is-active", "vbot.service"])

    assert result.returncode == 127
    assert result.stderr == "systemctl unavailable"


def test_systemd_restart_surfaces_timeout_message(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def runner(_args: list[str]) -> server_management._SystemctlRun:
        return server_management._SystemctlRun(
            returncode=server_management._SYSTEMCTL_TIMEOUT_RETURN_CODE,
            stdout="",
            stderr="systemctl timed out after 30s",
        )

    result = server_management._systemd_restart(instance, "vbot", runner=runner)

    assert not result.ok
    assert "systemctl timed out after 30s" in result.message
