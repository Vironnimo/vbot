"""Tests for cli main parsing."""

from __future__ import annotations

import argparse
import shlex
from typing import Any

import pytest

from cli import main as cli_main


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


def test_published_help_examples_parse_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    roots: list[argparse.ArgumentParser] = []

    def capture_parser(
        self: argparse.ArgumentParser, *args: Any, **kwargs: Any
    ) -> argparse.Namespace:
        roots.append(self)
        return argparse.Namespace()

    with monkeypatch.context() as capture:
        capture.setattr(argparse.ArgumentParser, "parse_args", capture_parser)
        cli_main.parse_args([])

    def check_examples(parser: argparse.ArgumentParser) -> None:
        description = parser.description or ""
        if "Example: " in description:
            example = description.split("Example: ", 1)[1]
            tokens = shlex.split(example)
            if tokens[0] == "vbot":
                tokens = tokens[1:]
            # Exercise the actual published arguments, without executing their effects.
            cli_main.parse_args(tokens)
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    check_examples(child)

    check_examples(roots[0])


def test_parse_args_supports_server_command_options() -> None:
    args = cli_main.parse_args(
        ["server", "start", "--host", "0.0.0.0", "--port", "9000", "--data-dir", "dev-data"]
    )

    assert args.area == "server"
    assert args.command == "start"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.data_dir == "dev-data"


def test_parse_args_supports_session_store_operations() -> None:
    status = cli_main.parse_args(["session-store", "status", "--data-dir", "dev-data"])
    restore = cli_main.parse_args(["session-store", "snapshot", "restore", "snapshot-1", "--yes"])
    incident = cli_main.parse_args(["session-store", "incident", "acknowledge", "incident-1"])

    assert (status.area, status.command, status.data_dir) == (
        "session-store",
        "status",
        "dev-data",
    )
    assert (restore.command, restore.snapshot_command, restore.snapshot_id, restore.yes) == (
        "snapshot",
        "restore",
        "snapshot-1",
        True,
    )
    assert (incident.command, incident.incident_command, incident.incident_id) == (
        "incident",
        "acknowledge",
        "incident-1",
    )


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
    "tokens",
    [
        ["agent", "update", "a", "--model", "p/m", "--clear-model"],
        ["agent", "update", "a", "--temperature", "0.4", "--clear-temperature"],
        ["agent", "update", "a", "--thinking-effort", "none", "--clear-thinking-effort"],
        ["agent", "update", "a", "--fallback-models", "p/m", "--clear-fallback-models"],
        ["agent", "update", "a", "--workspace", "C:/x", "--default-workspace"],
        ["agent", "update", "a", "--project", "p", "--clear-project"],
        ["agent", "update", "a", "--compaction-policy", "{}", "--clear-compaction-policy"],
        ["project", "set", "p", "--default-agent", "a", "--clear-default-agent"],
        ["project", "set", "p", "--default-model", "p/m", "--clear-default-model"],
        ["project", "set", "p", "--default-temperature", "0.4", "--clear-default-temperature"],
        [
            "project",
            "add",
            "C:/x",
            "--default-thinking-effort",
            "none",
            "--clear-default-thinking-effort",
        ],
        ["provider", "set-key", "openai", "sentinel", "--stdin"],
        ["config", "set", "debug.enabled", "true", "--stdin"],
        ["cron", "update", "job", "--session", "s", "--clear-session"],
        ["config", "--port", "8999", "get", "debug.enabled"],
    ],
)
def test_conflicting_or_misplaced_arguments_fail_before_dispatch(tokens: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli_main.parse_args(tokens)
    assert error.value.code == 2


def test_config_target_is_preserved_after_command() -> None:
    args = cli_main.parse_args(
        ["config", "get", "debug.enabled", "--host", "remote", "--port", "8999"]
    )
    assert (args.host, args.port) == ("remote", 8999)
