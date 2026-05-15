"""Tests for channel CLI parsing, RPC commands, and output."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import channel_management
from cli import main as cli_main
from cli.server_management import CommandResult, ServerInstance
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


def test_parse_args_supports_channel_add_options() -> None:
    args = cli_main.parse_args(
        [
            "channel",
            "add",
            "--id",
            "tg-assistant",
            "--platform",
            "telegram",
            "--agent",
            "assistant",
            "--token-env",
            "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
            "--dm-scope",
            "per_peer",
            "--allow",
            "100",
            "101",
            "--host",
            "localhost",
            "--port",
            "8500",
            "--data-dir",
            "dev-data",
        ]
    )

    assert args.area == "channel"
    assert args.command == "add"
    assert args.id == "tg-assistant"
    assert args.platform == "telegram"
    assert args.agent == "assistant"
    assert args.token_env == "TELEGRAM_BOT_TOKEN_TG_ASSISTANT"
    assert args.dm_scope == "per_peer"
    assert args.allow == [100, 101]
    assert args.host == "localhost"
    assert args.port == 8500
    assert args.data_dir == "dev-data"


@pytest.mark.parametrize("command", ["remove", "enable", "disable", "status"])
def test_parse_args_supports_channel_id_commands(command: str) -> None:
    args = cli_main.parse_args(
        [
            "channel",
            command,
            "--id",
            "tg-assistant",
            "--host",
            "0.0.0.0",
            "--port",
            "8600",
            "--data-dir",
            "runtime-data",
        ]
    )

    assert args.area == "channel"
    assert args.command == command
    assert args.id == "tg-assistant"
    assert args.host == "0.0.0.0"
    assert args.port == 8600
    assert args.data_dir == "runtime-data"


def test_parse_args_supports_channel_list_target_options() -> None:
    args = cli_main.parse_args(
        ["channel", "list", "--host", "localhost", "--port", "8700", "--data-dir", "dev"]
    )

    assert args.area == "channel"
    assert args.command == "list"
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_channel_add_posts_create_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"id": "tg-assistant"}})

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_add(
        instance,
        "tg-assistant",
        "telegram",
        "assistant",
        "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        "per_conversation",
        [100, 101],
    )

    assert result == CommandResult(ok=True, message="created tg-assistant", instance=instance)
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {
                "method": "channel.create",
                "params": {
                    "id": "tg-assistant",
                    "platform": "telegram",
                    "agent_id": "assistant",
                    "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
                    "dm_scope": "per_conversation",
                    "allowed_chat_ids": [100, 101],
                },
            },
            "timeout": 10.0,
        }
    ]


@pytest.mark.parametrize(
    ("command", "method", "expected_message"),
    [
        ("remove", "channel.delete", "removed tg-assistant"),
        ("enable", "channel.enable", "enabled tg-assistant"),
        ("disable", "channel.disable", "disabled tg-assistant"),
    ],
)
def test_channel_simple_id_commands_post_expected_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    method: str,
    expected_message: str,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)
    function_map = {
        "remove": channel_management.channel_remove,
        "enable": channel_management.channel_enable,
        "disable": channel_management.channel_disable,
    }

    result = function_map[command](instance, "tg-assistant")

    assert result == CommandResult(ok=True, message=expected_message, instance=instance)
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": method, "params": {"id": "tg-assistant"}},
            "timeout": 10.0,
        }
    ]


def test_channel_status_posts_status_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"id": "tg-assistant", "enabled": True, "running": False},
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_status(instance, "tg-assistant")

    assert result == CommandResult(
        ok=True,
        message="tg-assistant: enabled=yes running=no",
        instance=instance,
    )
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "channel.status", "params": {"id": "tg-assistant"}},
            "timeout": 10.0,
        }
    ]


def test_channel_list_formats_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "channel.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "channels": [
                        {
                            "id": "tg-assistant",
                            "platform": "telegram",
                            "agent_id": "assistant",
                            "dm_scope": "per_conversation",
                            "enabled": True,
                            "allowed_chat_ids": [123, 456],
                            "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
                        },
                        {
                            "id": "tg-work",
                            "platform": "telegram",
                            "agent_id": "assistant",
                            "dm_scope": "main",
                            "enabled": False,
                            "allowed_chat_ids": [],
                            "token_env_var": "TELEGRAM_BOT_TOKEN_TG_WORK",
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_list(instance)

    assert result.ok is True
    assert result.instance == instance
    assert result.message.splitlines() == [
        "channels:",
        (
            "- id=tg-assistant platform=telegram agent=assistant "
            "dm_scope=per_conversation enabled=yes allowed_chat_ids=123,456 "
            "token_env_var=TELEGRAM_BOT_TOKEN_TG_ASSISTANT"
        ),
        (
            "- id=tg-work platform=telegram agent=assistant dm_scope=main "
            "enabled=no allowed_chat_ids=- token_env_var=TELEGRAM_BOT_TOKEN_TG_WORK"
        ),
    ]


def test_channel_commands_surface_rpc_domain_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {"code": "channel_not_found", "message": "channel not found: tg-unknown"},
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_enable(instance, "tg-unknown")

    assert result == CommandResult(
        ok=False,
        message="channel_not_found: channel not found: tg-unknown",
        instance=instance,
    )


@pytest.mark.parametrize(
    ("command", "argv", "called_service", "expected_output_line"),
    [
        (
            "add",
            [
                "channel",
                "add",
                "--id",
                "tg-assistant",
                "--platform",
                "telegram",
                "--agent",
                "assistant",
                "--token-env",
                "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
                "--dm-scope",
                "per_conversation",
                "--allow",
                "1",
                "2",
            ],
            "add",
            "result: created tg-assistant",
        ),
        ("list", ["channel", "list"], "list", "result: channels:"),
        (
            "remove",
            ["channel", "remove", "--id", "tg-assistant"],
            "remove",
            "result: removed tg-assistant",
        ),
        (
            "enable",
            ["channel", "enable", "--id", "tg-assistant"],
            "enable",
            "result: enabled tg-assistant",
        ),
        (
            "disable",
            ["channel", "disable", "--id", "tg-assistant"],
            "disable",
            "result: disabled tg-assistant",
        ),
        (
            "status",
            ["channel", "status", "--id", "tg-assistant"],
            "status",
            "result: tg-assistant: enabled=yes running=no",
        ),
    ],
)
def test_run_dispatches_channel_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    argv: list[str],
    called_service: str,
    expected_output_line: str,
) -> None:
    calls: list[tuple[str, Any]] = []
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(("resolve", {"host": host, "port": port, "data_dir": data_dir}))
        return instance

    def fake_add(
        resolved_instance: ServerInstance,
        channel_id: str,
        platform: str,
        agent_id: str,
        token_env: str,
        dm_scope: str,
        allowed_chat_ids: Sequence[int],
    ) -> CommandResult:
        calls.append(
            (
                "add",
                {
                    "instance": resolved_instance,
                    "id": channel_id,
                    "platform": platform,
                    "agent": agent_id,
                    "token_env": token_env,
                    "dm_scope": dm_scope,
                    "allowed_chat_ids": allowed_chat_ids,
                },
            )
        )
        return CommandResult(ok=True, message="created tg-assistant", instance=resolved_instance)

    def fake_list(resolved_instance: ServerInstance) -> CommandResult:
        calls.append(("list", resolved_instance))
        return CommandResult(
            ok=True, message="channels:\n- id=tg-assistant", instance=resolved_instance
        )

    def fake_remove(resolved_instance: ServerInstance, channel_id: str) -> CommandResult:
        calls.append(("remove", {"instance": resolved_instance, "id": channel_id}))
        return CommandResult(ok=True, message="removed tg-assistant", instance=resolved_instance)

    def fake_enable(resolved_instance: ServerInstance, channel_id: str) -> CommandResult:
        calls.append(("enable", {"instance": resolved_instance, "id": channel_id}))
        return CommandResult(ok=True, message="enabled tg-assistant", instance=resolved_instance)

    def fake_disable(resolved_instance: ServerInstance, channel_id: str) -> CommandResult:
        calls.append(("disable", {"instance": resolved_instance, "id": channel_id}))
        return CommandResult(ok=True, message="disabled tg-assistant", instance=resolved_instance)

    def fake_status(resolved_instance: ServerInstance, channel_id: str) -> CommandResult:
        calls.append(("status", {"instance": resolved_instance, "id": channel_id}))
        return CommandResult(
            ok=True,
            message="tg-assistant: enabled=yes running=no",
            instance=resolved_instance,
        )

    exit_code = cli_main.run(
        [*argv, "--host", "localhost", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        add_channel=fake_add,
        list_channels=fake_list,
        remove_channel=fake_remove,
        enable_channel=fake_enable,
        disable_channel=fake_disable,
        channel_status_fn=fake_status,
    )

    assert exit_code == 0
    assert calls[0] == ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"})
    assert calls[1][0] == called_service
    output_lines = capsys.readouterr().out.splitlines()
    assert output_lines[0] == f"command: channel {command}"
    assert expected_output_line in output_lines
    assert output_lines[-2] == "url: http://127.0.0.1:8765"
    assert output_lines[-1] == f"data_dir: {tmp_path / 'data'}"


def test_print_channel_command_result_is_deterministic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    result = CommandResult(ok=True, message="enabled tg-assistant", instance=instance)

    cli_main.print_channel_command_result("enable", result)

    assert capsys.readouterr().out.splitlines() == [
        "command: channel enable",
        "result: enabled tg-assistant",
        "url: http://127.0.0.1:8420",
        f"data_dir: {tmp_path / 'data'}",
    ]


def test_channel_command_exit_code_maps_failed_result_to_failure(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_disable(resolved_instance: ServerInstance, channel_id: str) -> CommandResult:
        return CommandResult(
            ok=False, message="channel_not_found: missing", instance=resolved_instance
        )

    exit_code = cli_main.run(
        ["channel", "disable", "--id", "tg-unknown"],
        resolve=fake_resolve,
        disable_channel=fake_disable,
    )

    assert exit_code == 1
