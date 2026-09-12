"""Tests for cli channel dispatch."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from cli import main as cli_main
from cli.server_management import CommandResult, ServerInstance
from tests.cli.cli_channel_test_support import (
    make_instance,
)


def test_run_dispatches_additive_channel_admin_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[str, str, str]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_grant(
        resolved_instance: ServerInstance,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> CommandResult:
        assert resolved_instance == instance
        calls.append((channel_id, access_scope_id, user_id))
        return CommandResult(ok=True, message="admin saved", instance=resolved_instance)

    exit_code = cli_main.run(
        [
            "channel",
            "grant-admin",
            "tg-assistant",
            "--group",
            "-100",
            "--user",
            "51",
        ],
        resolve=fake_resolve,
        grant_channel_admin_fn=fake_grant,
    )

    assert exit_code == 0
    assert calls == [("tg-assistant", "-100", "51")]
    assert "admin saved" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "argv", "called_service", "expected_output_line"),
    [
        (
            "add",
            [
                "channel",
                "add",
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
            ["channel", "remove", "tg-assistant"],
            "remove",
            "result: removed tg-assistant",
        ),
        (
            "update",
            [
                "channel",
                "update",
                "tg-assistant",
                "--agent",
                "coder",
                "--allow",
                "1",
                "2",
                "--enabled",
                "false",
            ],
            "update",
            "result: updated tg-assistant",
        ),
        (
            "enable",
            ["channel", "enable", "tg-assistant"],
            "enable",
            "result: enabled tg-assistant",
        ),
        (
            "disable",
            ["channel", "disable", "tg-assistant"],
            "disable",
            "result: disabled tg-assistant",
        ),
        (
            "status",
            ["channel", "status", "tg-assistant"],
            "status",
            "result: tg-assistant: enabled=yes running=no failed=no",
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
        token_env: str | None,
        dm_scope: str,
        allowed_chat_ids: Sequence[str],
        response_mode: str,
        mention_patterns: Sequence[str],
        observe_unaddressed: bool,
        token: str | None,
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
                    "response_mode": response_mode,
                    "mention_patterns": mention_patterns,
                    "observe_unaddressed": observe_unaddressed,
                    "token": token,
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

    def fake_update(
        resolved_instance: ServerInstance,
        channel_id: str,
        changes: dict[str, Any],
    ) -> CommandResult:
        calls.append(
            (
                "update",
                {"instance": resolved_instance, "id": channel_id, "changes": changes},
            )
        )
        return CommandResult(ok=True, message="updated tg-assistant", instance=resolved_instance)

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
            message="tg-assistant: enabled=yes running=no failed=no",
            instance=resolved_instance,
        )

    exit_code = cli_main.run(
        [*argv, "--host", "localhost", "--port", "8765", "--data-dir", "data"],
        resolve=fake_resolve,
        add_channel=fake_add,
        list_channels=fake_list,
        remove_channel=fake_remove,
        update_channel=fake_update,
        enable_channel=fake_enable,
        disable_channel=fake_disable,
        channel_status_fn=fake_status,
        set_channel_token=lambda resolved_instance, channel_id, token: CommandResult(
            ok=True, message=f"saved token for channel {channel_id}", instance=resolved_instance
        ),
    )

    assert exit_code == 0
    assert calls[0] == ("resolve", {"host": "localhost", "port": 8765, "data_dir": "data"})
    assert calls[1][0] == called_service
    output_lines = capsys.readouterr().out.splitlines()
    assert output_lines[0] == f"command: channel {command}"
    assert expected_output_line in output_lines
    assert output_lines[-2] == "url: http://127.0.0.1:8765"
    assert output_lines[-1] == f"local_data_dir: {tmp_path / 'data'}"


def test_run_channel_set_token_reads_utf8_stdin_without_echoing_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    instance = make_instance(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_set_token(
        resolved_instance: ServerInstance, channel_id: str, token: str
    ) -> CommandResult:
        calls.append((channel_id, token))
        return CommandResult(
            ok=True,
            message=f"saved token for channel {channel_id}",
            instance=resolved_instance,
        )

    monkeypatch.setattr(cli_main.sys, "stdin", io.StringIO("rotated-secret\n"))

    exit_code = cli_main.run(
        ["channel", "set-token", "tg-main", "--stdin"],
        resolve=fake_resolve,
        set_channel_token=fake_set_token,
    )

    assert exit_code == 0
    assert calls == [("tg-main", "rotated-secret")]
    assert "rotated-secret" not in capsys.readouterr().out


def test_print_channel_command_result_is_deterministic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    result = CommandResult(ok=True, message="enabled tg-assistant", instance=instance)

    cli_main.print_channel_command_result("enable", result)

    output = capsys.readouterr().out
    assert "channel enable" in output
    assert "enabled tg-assistant" in output
    assert "http://127.0.0.1:8420" in output
    assert str(tmp_path / "data") in output


def test_channel_command_exit_code_maps_failed_result_to_failure(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_disable(resolved_instance: ServerInstance, channel_id: str) -> CommandResult:
        return CommandResult(
            ok=False, message="channel_not_found: missing", instance=resolved_instance
        )

    exit_code = cli_main.run(
        ["channel", "disable", "tg-unknown"],
        resolve=fake_resolve,
        disable_channel=fake_disable,
    )

    assert exit_code == 1
