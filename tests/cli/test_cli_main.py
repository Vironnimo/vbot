"""Tests for cli main."""

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
from tests.cli.cli_main_test_support import (
    make_instance,
)


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


@pytest.mark.parametrize(
    "tokens,method,param",
    [
        (["provider", "set-key", "openai", "--stdin"], "provider.set_key", "value"),
        (["config", "set", "skills.directories", "--stdin"], "settings.patch", "operations"),
    ],
)
def test_stdin_reaches_rpc_without_shell_quoting(
    tmp_path, monkeypatch, capsys, tokens, method, param
):
    import io

    import httpx

    from cli import rpc_client

    content = "credential-sentinel" if param == "value" else '["C:/skills with spaces/ä"]'
    calls = []
    monkeypatch.setattr(cli_main.sys, "stdin", io.StringIO(content))

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(rpc_client.httpx, "post", post)
    cli_main.run(tokens, resolve=lambda **kw: make_instance(tmp_path))
    assert calls[0]["method"] == method
    if param == "value":
        assert calls[0]["params"][param] == content
        assert content not in capsys.readouterr().out
    else:
        assert calls[0]["params"][param] == [
            {"op": "set", "path": "skills.directories", "value": ["C:/skills with spaces/ä"]}
        ]


def test_invalid_stdin_json_never_posts(tmp_path, monkeypatch):
    import io

    from cli import rpc_client

    calls = []
    monkeypatch.setattr(cli_main.sys, "stdin", io.StringIO("{broken"))
    monkeypatch.setattr(rpc_client.httpx, "post", lambda *a, **kw: calls.append(kw))
    assert (
        cli_main.run(
            ["config", "set", "skills.directories", "--stdin"],
            resolve=lambda **kw: make_instance(tmp_path),
        )
        == 1
    )
    assert calls == []
