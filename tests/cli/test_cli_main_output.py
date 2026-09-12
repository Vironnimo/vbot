"""Tests for cli main output."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import main as cli_main
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance, WebUIProbeResult
from core.utils.logging import resolve_daily_log_path
from tests.cli.cli_main_test_support import (
    make_instance,
)


def make_result(
    tmp_path: Path,
    *,
    ok: bool = True,
    message: str = "running",
    health: HealthProbeResult | None = None,
    webui: WebUIProbeResult | None = None,
) -> CommandResult:
    instance = make_instance(tmp_path)
    return CommandResult(
        ok=ok,
        message=message,
        instance=instance,
        health=health,
        webui=webui,
        log_path=instance.log_path,
    )


def test_restart_stops_then_re_resolves_and_starts(tmp_path: Path) -> None:
    calls: list[str] = []
    first_instance = make_instance(tmp_path, port=8001)
    second_instance = make_instance(tmp_path, port=8002)
    instances = iter([first_instance, second_instance])

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(f"resolve:{host}:{port}:{data_dir}")
        return next(instances)

    def fake_stop(instance: ServerInstance) -> CommandResult:
        calls.append(f"stop:{instance.port}")
        return CommandResult(ok=True, message="stopped", instance=instance)

    def fake_start(instance: ServerInstance) -> CommandResult:
        calls.append(f"start:{instance.port}")
        return CommandResult(ok=True, message="started", instance=instance)

    exit_code = cli_main.run(
        ["server", "restart", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        start=fake_start,
        stop=fake_stop,
    )

    assert exit_code == 0
    assert calls == [
        "resolve:127.0.0.1:8765:data",
        "stop:8001",
        "resolve:127.0.0.1:8765:data",
        "start:8002",
    ]


def test_restart_does_not_start_when_stop_fails(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def fake_start(unused_instance: ServerInstance) -> CommandResult:
        raise AssertionError("restart must not start after failed stop")

    exit_code = cli_main.run(
        ["server", "restart"],
        resolve=lambda **kwargs: instance,
        start=fake_start,
        stop=lambda resolved: CommandResult(
            ok=False,
            message="port occupied by non-vBot process",
            instance=resolved,
            health=HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
        ),
    )

    assert exit_code == 1


def test_restart_starts_when_target_is_not_running(tmp_path: Path) -> None:
    calls: list[str] = []
    first_instance = make_instance(tmp_path, port=8001)
    second_instance = make_instance(tmp_path, port=8002)
    instances = iter([first_instance, second_instance])

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(f"resolve:{host}:{port}:{data_dir}")
        return next(instances)

    def fake_stop(instance: ServerInstance) -> CommandResult:
        calls.append(f"stop:{instance.port}")
        return CommandResult(ok=True, message="not running", instance=instance)

    def fake_start(instance: ServerInstance) -> CommandResult:
        calls.append(f"start:{instance.port}")
        return CommandResult(ok=True, message="started", instance=instance)

    exit_code = cli_main.run(
        ["server", "restart", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        start=fake_start,
        stop=fake_stop,
    )

    assert exit_code == 0
    assert calls == [
        "resolve:127.0.0.1:8765:data",
        "stop:8001",
        "resolve:127.0.0.1:8765:data",
        "start:8002",
    ]


def test_output_contains_deterministic_status_fields(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = make_result(
        tmp_path,
        message="started",
        health=HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
        webui=WebUIProbeResult(available=False, status_code=404),
    )

    cli_main.print_command_result("start", result)

    lines = capsys.readouterr().out.splitlines()
    assert "command: server start" in lines
    assert "result: started" in lines
    assert "running: yes" in lines
    assert "url: http://127.0.0.1:8420" in lines
    assert "webui: unavailable" in lines
    assert f"data_dir: {tmp_path / 'data'}" in lines
    assert f"log_path: {resolve_daily_log_path(tmp_path / 'data')}" in lines
    assert lines[-1].strip()


def test_start_output_omits_unknown_webui_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = make_result(
        tmp_path,
        ok=False,
        message="server readiness timed out",
        health=HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
    )

    cli_main.print_command_result("start", result)

    lines = capsys.readouterr().out.splitlines()
    assert "command: server start" in lines
    assert "result: server readiness timed out" in lines
    assert "running: no" in lines
    assert "url: http://127.0.0.1:8420" in lines
    assert all(not line.startswith("webui:") for line in lines)
    assert f"data_dir: {tmp_path / 'data'}" in lines
    assert f"log_path: {resolve_daily_log_path(tmp_path / 'data')}" in lines
    assert lines[-1].strip()


def test_output_reports_process_id_forced_and_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    result = CommandResult(
        ok=False,
        message="port occupied by non-vBot process",
        instance=instance,
        health=HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
        process_id=123,
        forced=True,
    )

    cli_main.print_command_result("stop", result)

    lines = capsys.readouterr().out.splitlines()
    assert "command: server stop" in lines
    assert "url: http://127.0.0.1:8420" in lines
    assert f"data_dir: {tmp_path / 'data'}" in lines
    assert "process_id: 123" in lines
    assert "forced: true" in lines
    assert "conflict: port occupied by non-vBot process" in lines
    assert lines[-1].strip()


def test_status_conflict_output_reports_not_running_with_note(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    result = CommandResult(
        ok=False,
        message="port occupied by non-vBot process",
        instance=instance,
        health=HealthProbeResult(reachable=True, is_vbot=False, status_code=200),
        webui=WebUIProbeResult(available=False),
        log_path=instance.log_path,
    )

    cli_main.print_command_result("status", result)

    lines = capsys.readouterr().out.splitlines()
    assert "command: server status" in lines
    assert "running: no" in lines
    assert "url: http://127.0.0.1:8420" in lines
    assert "webui: unavailable" in lines
    assert f"data_dir: {tmp_path / 'data'}" in lines
    assert f"log_path: {resolve_daily_log_path(tmp_path / 'data')}" in lines
    assert "conflict: port occupied by non-vBot process" in lines
    assert lines[-1].strip()


def test_server_command_announces_action_before_dispatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)

    def fake_start(resolved: ServerInstance) -> CommandResult:
        announcement = capsys.readouterr().out
        assert announcement.strip()
        assert "http://127.0.0.1:8420" in announcement
        return CommandResult(ok=True, message="started", instance=resolved)

    exit_code = cli_main.run(
        ["server", "start"],
        resolve=lambda **_kwargs: instance,
        start=fake_start,
    )

    assert exit_code == 0
    completion = capsys.readouterr().out
    assert completion.strip()
    assert "http://127.0.0.1:8420" in completion


@pytest.mark.parametrize(
    ("ok", "before", "after"),
    [
        (True, "0.1.22", "0.1.23"),
        (True, "0.1.23", "0.1.23"),
        (False, "0.1.22", "0.1.22"),
    ],
)
def test_update_output_has_readable_start_and_completion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ok: bool,
    before: str,
    after: str,
) -> None:
    result = CommandResult(ok=ok, message="update details", instance=make_instance(tmp_path))

    cli_main.print_update_command_start(before)
    cli_main.print_update_command_result(
        result,
        version_before=before,
        version_after=after,
    )

    output = capsys.readouterr().out
    assert "update details" in output
    assert before in output
    assert after in output


def test_run_update_announces_before_work_and_ends_with_version_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = iter(["0.1.22", "0.1.23"])
    instance = make_instance(tmp_path)
    monkeypatch.setattr(cli_main, "read_checkout_version", lambda: next(versions))

    def fake_dispatch(
        _args: object,
        *,
        resolve: object,
        stop: object,
        start: object,
    ) -> CommandResult:
        assert resolve is cli_main.resolve_instance
        assert stop is cli_main.stop_server
        assert start is cli_main.start_server
        announcement = capsys.readouterr().out
        assert announcement.strip()
        assert "0.1.22" in announcement
        return CommandResult(ok=True, message="updated checkout", instance=instance)

    monkeypatch.setattr(cli_main, "dispatch_update_command", fake_dispatch)

    exit_code = cli_main.run(["update"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "updated checkout" in output
    assert "0.1.22" in output
    assert "0.1.23" in output


def test_management_output_never_silent_for_empty_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = CommandResult(ok=True, message="", instance=make_instance(tmp_path))

    cli_main.print_management_command_result(result)

    output = capsys.readouterr().out
    assert output.startswith("success:")
    assert output.strip()


def test_management_output_never_silent_for_empty_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = CommandResult(ok=False, message="", instance=make_instance(tmp_path))

    cli_main.print_management_command_result(result)

    output = capsys.readouterr().out
    assert output.startswith("error:")
    assert output.strip()


@pytest.mark.parametrize(
    ("command", "result", "expected_exit_code"),
    [
        ("start", CommandResult(True, "already running", make_instance(Path("data"))), 0),
        ("stop", CommandResult(True, "not running", make_instance(Path("data"))), 0),
        ("status", CommandResult(True, "not running", make_instance(Path("data"))), 0),
        (
            "status",
            CommandResult(False, "port occupied by non-vBot process", make_instance(Path("data"))),
            0,
        ),
        (
            "start",
            CommandResult(False, "port occupied by non-vBot process", make_instance(Path("data"))),
            1,
        ),
        (
            "start",
            CommandResult(False, "server readiness timed out", make_instance(Path("data"))),
            1,
        ),
    ],
)
def test_exit_code_mapping(command: str, result: CommandResult, expected_exit_code: int) -> None:
    assert cli_main.exit_code_for(command, result) == expected_exit_code


def test_main_exits_with_run_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "run", lambda argv: 7)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["server", "status"])

    assert exc_info.value.code == 7
