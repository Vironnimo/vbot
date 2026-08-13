"""Tests for the vBot CLI command parser and output mapping."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from cli import main as cli_main
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance, WebUIProbeResult
from cli.uninstall_management import UninstallMode, UninstallResult
from core.utils.config import VBOT_ROOT
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


@pytest.mark.parametrize(
    "argv",
    [
        ["server"],
        ["server", "start"],
        ["server", "stop"],
        ["server", "restart"],
        ["server", "status"],
        ["desktop"],
        ["home"],
        ["uninstall"],
        ["agent"],
        ["agent", "list"],
        ["agent", "show"],
        ["agent", "create"],
        ["agent", "update"],
        ["agent", "rename"],
        ["agent", "delete"],
        ["session"],
        ["session", "list"],
        ["session", "create"],
        ["session", "link-channel"],
        ["channel"],
        ["channel", "add"],
        ["channel", "list"],
        ["channel", "remove"],
        ["channel", "update"],
        ["channel", "enable"],
        ["channel", "disable"],
        ["channel", "status"],
        ["tool"],
        ["tool", "list"],
        ["prompt"],
        ["prompt", "list"],
        ["prompt", "update"],
        ["prompt", "reset"],
        ["prompt", "preview"],
        ["log"],
        ["log", "list"],
        ["log", "read"],
        ["provider"],
        ["provider", "list"],
        ["provider", "status"],
        ["provider", "set-key"],
        ["provider", "connect"],
        ["provider", "disconnect"],
        ["provider", "connect-status"],
        ["model"],
        ["model", "list"],
        ["model", "show"],
        ["model", "refresh"],
        ["task-model"],
        ["task-model", "list"],
        ["task-model", "targets"],
        ["task-model", "options"],
        ["task-model", "set"],
        ["task-model", "set-option"],
        ["task-model", "unset-option"],
        ["task-model", "clear"],
        ["skill"],
        ["skill", "list"],
        ["cron"],
        ["cron", "list"],
        ["cron", "create"],
        ["cron", "update"],
        ["cron", "delete"],
        ["cron", "enable"],
        ["cron", "disable"],
        ["config"],
        ["config", "get"],
        ["config", "set"],
        ["debug"],
        ["debug", "status"],
        ["debug", "traces"],
        ["debug", "trace"],
        ["debug", "clear"],
        ["debug", "probe"],
        ["doctor"],
        ["doctor", "settings"],
        ["doctor", "config"],
    ],
)
def test_cli_area_and_subcommand_help_is_informative(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.parse_args([*argv, "--help"])

    assert exc_info.value.code == 0


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


def test_parse_args_desktop_without_target_leaves_host_and_port_unset() -> None:
    args = cli_main.parse_args(["desktop"])

    assert args.area == "desktop"
    assert args.host is None
    assert args.port is None


def test_parse_args_desktop_accepts_host_and_port() -> None:
    args = cli_main.parse_args(["desktop", "--host", "192.168.1.50", "--port", "8500"])

    assert args.area == "desktop"
    assert args.host == "192.168.1.50"
    assert args.port == 8500


def test_parse_args_desktop_rejects_data_dir() -> None:
    with pytest.raises(SystemExit):
        cli_main.parse_args(["desktop", "--data-dir", "data"])


def test_parse_args_home_accepts_optional_data_dir() -> None:
    args = cli_main.parse_args(["home", "--data-dir", "dev-data"])

    assert args.area == "home"
    assert args.data_dir == "dev-data"


def test_run_home_prints_app_and_resolved_data_directories_without_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_resolve(**kwargs: object) -> ServerInstance:
        raise AssertionError(f"home must not resolve a server: {kwargs}")

    exit_code = cli_main.run(
        ["home", "--data-dir", "runtime-data"],
        resolve=fail_resolve,
    )

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"vbot_root: {VBOT_ROOT}",
        f"data_dir: {tmp_path / 'runtime-data'}",
    ]


def test_parse_args_uninstall_accepts_platform_autostart_names() -> None:
    args = cli_main.parse_args(
        [
            "uninstall",
            "--all",
            "--yes",
            "--host",
            "localhost",
            "--port",
            "9000",
            "--data-dir",
            "custom-data",
            "--task-name",
            "My Task",
            "--service-name",
            "my-service",
        ]
    )

    assert args.area == "uninstall"
    assert args.uninstall_mode == "all"
    assert args.yes is True
    assert args.host == "localhost"
    assert args.port == 9000
    assert args.data_dir == "custom-data"
    assert args.task_name == "My Task"
    assert args.service_name == "my-service"


def test_parse_args_uninstall_rejects_multiple_modes() -> None:
    with pytest.raises(SystemExit):
        cli_main.parse_args(["uninstall", "--app-only", "--data-only"])


def test_run_uninstall_dispatches_selection_and_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def uninstall_fn(**kwargs: object) -> UninstallResult:
        captured.update(kwargs)
        return UninstallResult(ok=True, message="uninstall launched")

    exit_code = cli_main.run(
        [
            "uninstall",
            "--all",
            "--yes",
            "--host",
            "localhost",
            "--port",
            "9000",
            "--data-dir",
            "custom-data",
            "--task-name",
            "My Task",
            "--service-name",
            "my-service",
        ],
        uninstall_fn=uninstall_fn,
    )

    assert exit_code == 0
    assert captured["mode"] is UninstallMode.ALL
    assert captured["assume_yes"] is True
    assert captured["host"] == "localhost"
    assert captured["port"] == 9000
    assert captured["data_dir"] == "custom-data"
    assert captured["task_name"] == "My Task"
    assert captured["service_name"] == "my-service"
    assert captured["resolve"] is cli_main.resolve_instance
    assert captured["stop"] is cli_main.stop_server
    assert captured["start"] is cli_main.start_server
    assert capsys.readouterr().out.splitlines() == ["uninstall launched"]


def test_run_desktop_forwards_supplied_target_flags_to_injected_launcher(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[Sequence[str]] = []

    def fake_launch(launch_argv: Sequence[str]) -> None:
        calls.append(list(launch_argv))

    exit_code = cli_main.run(
        ["desktop", "--host", "192.168.1.50", "--port", "8500"],
        launch_desktop_fn=fake_launch,
    )

    assert exit_code == 0
    assert calls == [["--host", "192.168.1.50", "--port", "8500"]]
    assert capsys.readouterr().out.strip()


def test_run_desktop_without_flags_passes_empty_argv_to_launcher() -> None:
    calls: list[Sequence[str]] = []

    def fake_launch(launch_argv: Sequence[str]) -> None:
        calls.append(list(launch_argv))

    exit_code = cli_main.run(["desktop"], launch_desktop_fn=fake_launch)

    assert exit_code == 0
    assert calls == [[]]


def test_run_desktop_reports_failure_when_launcher_raises_runtime_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_launch(launch_argv: Sequence[str]) -> None:
        raise RuntimeError("pywebview is required to run vBot Desktop")

    exit_code = cli_main.run(["desktop"], launch_desktop_fn=fake_launch)

    assert exit_code == 1
    output = capsys.readouterr().out
    assert output.startswith("error:")
    assert "pywebview is required to run vBot Desktop" in output


def test_parse_args_supports_agent_update_fields() -> None:
    args = cli_main.parse_args(
        [
            "agent",
            "update",
            "coder",
            "--name",
            "Coder Two",
            "--model",
            "openai/gpt-5.2",
            "--clear-temperature",
            "--thinking-effort",
            "none",
            "--memory-prompt-mode",
            "agent",
            "--custom-system-prompt",
            "true",
            "--tool-access-mode",
            "selected",
            "--tool-allow",
            "read_file",
            "edit_file",
            "--tool-deny",
            "memory",
            "--allowed-skills",
            "debugging",
            "vbot-cli",
            "--workspace",
            "C:/agents/coder",
            "--copy-workspace-files",
            "--project",
            "vbot",
            "--current-session-id",
            "session-one",
        ]
    )

    assert args.area == "agent"
    assert args.command == "update"
    assert args.id == "coder"
    assert args.name == "Coder Two"
    assert args.model == "openai/gpt-5.2"
    assert args.clear_temperature is True
    assert args.thinking_effort == "none"
    assert args.memory_prompt_mode == "agent"
    assert args.custom_system_prompt == "true"
    assert args.tool_access_mode == "selected"
    assert args.tool_allow == ["read_file", "edit_file"]
    assert args.tool_deny == ["memory"]
    assert args.allowed_skills == ["debugging", "vbot-cli"]
    assert args.workspace == "C:/agents/coder"
    assert args.copy_workspace_files is True
    assert args.project == "vbot"
    assert args.current_session_id == "session-one"


def test_parse_args_supports_agent_rename() -> None:
    args = cli_main.parse_args(["agent", "rename", "coder", "researcher"])

    assert args.area == "agent"
    assert args.command == "rename"
    assert args.id == "coder"
    assert args.new_id == "researcher"


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


def test_run_agent_update_dispatches_changes_and_prints_plain_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path, port=8765)
    result = CommandResult(ok=True, message="updated coder", instance=instance)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_update_agent(
        resolved_instance: ServerInstance,
        agent_id: str,
        changes: dict[str, Any],
    ) -> CommandResult:
        calls.append(("agent.update", (resolved_instance, agent_id, changes)))
        return result

    exit_code = cli_main.run(
        [
            "agent",
            "update",
            "coder",
            "--name",
            "Coder Two",
            "--clear-temperature",
            "--tool-access-mode",
            "selected",
            "--tool-allow",
            "read_file",
            "--tool-deny",
            "memory",
            "--allowed-skills",
            "debugging",
            "--default-workspace",
            "--copy-workspace-files",
            "--clear-project",
            "--host",
            "localhost",
            "--port",
            "8765",
            "--data-dir",
            "data",
        ],
        resolve=fake_resolve,
        update_agent=fake_update_agent,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        (
            "agent.update",
            (
                instance,
                "coder",
                {
                    "name": "Coder Two",
                    "temperature": None,
                    "tool_access": {
                        "mode": "selected",
                        "allowed": ["read_file"],
                        "denied": ["memory"],
                    },
                    "allowed_skills": ["debugging"],
                    "workspace": None,
                    "copy_workspace_identity_files": True,
                    "root_project_id": None,
                },
            ),
        ),
    ]


def test_run_agent_update_builds_an_explicit_empty_selected_policy(
    tmp_path: Path,
) -> None:
    instance = make_instance(tmp_path)
    update_agent = Mock(return_value=CommandResult(ok=True, message="updated", instance=instance))

    exit_code = cli_main.run(
        ["agent", "update", "coder", "--tool-access-mode", "selected"],
        resolve=lambda **_kwargs: instance,
        update_agent=update_agent,
    )

    assert exit_code == 0
    assert update_agent.call_args.args[2] == {"tool_access": {"mode": "selected", "allowed": []}}


def test_run_agent_update_rejects_tool_names_without_an_explicit_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    update_agent = Mock()

    exit_code = cli_main.run(
        ["agent", "update", "coder", "--tool-deny", "memory"],
        resolve=lambda **_kwargs: instance,
        update_agent=update_agent,
    )

    assert exit_code == 1
    assert "require --tool-access-mode" in capsys.readouterr().out
    update_agent.assert_not_called()


def test_run_agent_rename_dispatches_ids_and_prints_plain_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path)
    result = CommandResult(ok=True, message="renamed coder -> researcher", instance=instance)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_rename_agent(
        resolved_instance: ServerInstance,
        agent_id: str,
        new_agent_id: str,
    ) -> CommandResult:
        calls.append(("agent.rename", (resolved_instance, agent_id, new_agent_id)))
        return result

    exit_code = cli_main.run(
        ["agent", "rename", "coder", "researcher"],
        resolve=fake_resolve,
        rename_agent=fake_rename_agent,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "127.0.0.1", "port": None, "data_dir": None}),
        ("agent.rename", (instance, "coder", "researcher")),
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

    def fake_list_models(
        resolved_instance: ServerInstance, filters: dict[str, Any]
    ) -> CommandResult:
        calls.append(("model.list", (resolved_instance, filters)))
        return result

    exit_code = cli_main.run(
        ["model", "list", "--host", "localhost", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        list_models_fn=fake_list_models,
    )

    assert exit_code == 0
    assert calls == [
        ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"}),
        ("model.list", (instance, {})),
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


def test_run_skill_catalog_dispatches_and_prints_plain_output(
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


def test_configure_console_output_replaces_legacy_windows_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacyStream:
        def __init__(self) -> None:
            self.encoding = "cp1252"
            self.errors = "strict"

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.encoding = encoding
            self.errors = errors

    stdout = LegacyStream()
    stderr = LegacyStream()
    monkeypatch.setattr(cli_main.sys, "stdout", stdout)
    monkeypatch.setattr(cli_main.sys, "stderr", stderr)

    cli_main._configure_console_output()

    assert (stdout.encoding, stdout.errors) == ("utf-8", "backslashreplace")
    assert (stderr.encoding, stderr.errors) == ("utf-8", "backslashreplace")
