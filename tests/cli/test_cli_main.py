"""Tests for the vBot CLI command parser and output mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cli import main as cli_main
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance, WebUIProbeResult
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url=f"http://127.0.0.1:{port}",
        log_path=resolve_daily_log_path(data_dir),
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


def test_parse_args_supports_server_command_options() -> None:
    args = cli_main.parse_args(
        ["server", "start", "--host", "0.0.0.0", "--port", "9000", "--data-dir", "dev-data"]
    )

    assert args.area == "server"
    assert args.command == "start"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.data_dir == "dev-data"


@pytest.mark.parametrize("command", ["start", "stop", "restart", "status"])
def test_each_server_command_accepts_target_options(command: str) -> None:
    args = cli_main.parse_args(
        ["server", command, "--host", "localhost", "--port", "8765", "--data-dir", "data"]
    )

    assert args.command == command
    assert args.host == "localhost"
    assert args.port == 8765
    assert args.data_dir == "data"


@pytest.mark.parametrize(
    ("command", "called_service"),
    [("start", "start"), ("stop", "stop"), ("status", "status")],
)
def test_run_dispatches_command_to_service_layer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    called_service: str,
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path, port=8765)
    result = CommandResult(
        ok=True,
        message="running",
        instance=instance,
        health=HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
        webui=WebUIProbeResult(available=True, status_code=200),
        log_path=instance.log_path,
    )

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def service(name: str):
        def fake_service(resolved_instance: ServerInstance) -> CommandResult:
            calls.append((name, resolved_instance))
            return result

        return fake_service

    exit_code = cli_main.run(
        ["server", command, "--host", "localhost", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        start=service("start"),
        stop=service("stop"),
        status=service("status"),
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        (called_service, instance),
    ]
    assert f"command: server {command}" in capsys.readouterr().out


def test_run_provider_list_dispatches_and_prints_plain_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path, port=8765)
    result = CommandResult(
        ok=True,
        message=(
            "connections:\n"
            "- id: openai:default  provider_id: openai"
            "  type: api_key  label: OpenAI  usable: yes"
        ),
        instance=instance,
    )

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_list_providers(resolved_instance: ServerInstance) -> CommandResult:
        calls.append(("provider.list", resolved_instance))
        return result

    exit_code = cli_main.run(
        ["provider", "list", "--host", "localhost", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        list_providers=fake_list_providers,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        ("provider.list", instance),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "connections:",
        "- id: openai:default  provider_id: openai  type: api_key  label: OpenAI  usable: yes",
    ]


def test_run_model_list_dispatches_and_prints_plain_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path, port=8765)
    result = CommandResult(
        ok=True,
        message="models:\n- id: openai/gpt-4o  name: GPT-4o  context_window: 128000",
        instance=instance,
    )

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_list_models(resolved_instance: ServerInstance) -> CommandResult:
        calls.append(("model.list", resolved_instance))
        return result

    exit_code = cli_main.run(
        ["model", "list", "--host", "localhost", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        list_models_fn=fake_list_models,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        ("model.list", instance),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "models:",
        "- id: openai/gpt-4o  name: GPT-4o  context_window: 128000",
    ]


def test_run_model_refresh_dispatches_provider_and_prints_plain_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path, port=8765)
    result = CommandResult(ok=True, message="refreshed openai", instance=instance)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_refresh_models(
        resolved_instance: ServerInstance, provider_id: str | None
    ) -> CommandResult:
        calls.append(("model.refresh_db", (resolved_instance, provider_id)))
        return result

    exit_code = cli_main.run(
        [
            "model",
            "refresh",
            "--provider",
            "openai",
            "--host",
            "localhost",
            "--port",
            "8765",
            "--data-dir",
            "data",
        ],
        resolve=fake_resolve,
        refresh_models_fn=fake_refresh_models,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        ("model.refresh_db", (instance, "openai")),
    ]
    assert capsys.readouterr().out.splitlines() == ["refreshed openai"]


def test_run_skill_list_dispatches_and_prints_plain_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path, port=8765)
    result = CommandResult(
        ok=True,
        message="skills:\n- summarize  Summarize long text",
        instance=instance,
    )

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_list_skills(resolved_instance: ServerInstance) -> CommandResult:
        calls.append(("skill.list", resolved_instance))
        return result

    exit_code = cli_main.run(
        ["skill", "list", "--host", "localhost", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        list_skills_fn=fake_list_skills,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        ("skill.list", instance),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "skills:",
        "- summarize  Summarize long text",
    ]


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

    assert capsys.readouterr().out.splitlines() == [
        "command: server start",
        "result: started",
        "running: yes",
        "url: http://127.0.0.1:8420",
        "webui: unavailable",
        f"data_dir: {tmp_path / 'data'}",
        f"log_path: {resolve_daily_log_path(tmp_path / 'data')}",
    ]


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

    assert capsys.readouterr().out.splitlines() == [
        "command: server start",
        "result: server readiness timed out",
        "running: no",
        "url: http://127.0.0.1:8420",
        f"data_dir: {tmp_path / 'data'}",
        f"log_path: {resolve_daily_log_path(tmp_path / 'data')}",
    ]


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

    assert capsys.readouterr().out.splitlines() == [
        "command: server stop",
        "result: port occupied by non-vBot process",
        "url: http://127.0.0.1:8420",
        f"data_dir: {tmp_path / 'data'}",
        "process_id: 123",
        "forced: true",
        "conflict: port occupied by non-vBot process",
    ]


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

    assert capsys.readouterr().out.splitlines() == [
        "command: server status",
        "result: port occupied by non-vBot process",
        "running: no",
        "url: http://127.0.0.1:8420",
        "webui: unavailable",
        f"data_dir: {tmp_path / 'data'}",
        f"log_path: {resolve_daily_log_path(tmp_path / 'data')}",
        "conflict: port occupied by non-vBot process",
    ]


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
