"""Tests for cli channel parsing."""

from __future__ import annotations

import pytest

from cli import main as cli_main


def test_parse_args_supports_channel_add_options() -> None:
    args = cli_main.parse_args(
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
    assert args.token_stdin is False
    assert args.dm_scope == "per_peer"
    assert args.allow == ["100", "101"]
    assert args.host == "localhost"
    assert args.port == 8500
    assert args.data_dir == "dev-data"


def test_parse_args_supports_managed_channel_token_from_stdin() -> None:
    args = cli_main.parse_args(
        [
            "channel",
            "add",
            "tg-assistant",
            "--platform",
            "telegram",
            "--agent",
            "assistant",
            "--token-stdin",
        ]
    )

    assert args.token_env is None
    assert args.token_stdin is True


def test_parse_args_rejects_multiple_channel_token_sources() -> None:
    with pytest.raises(SystemExit):
        cli_main.parse_args(
            [
                "channel",
                "add",
                "tg-assistant",
                "--platform",
                "telegram",
                "--agent",
                "assistant",
                "--token-stdin",
                "--token-env",
                "TELEGRAM_BOT_TOKEN",
            ]
        )


def test_parse_args_supports_channel_set_token() -> None:
    args = cli_main.parse_args(["channel", "set-token", "tg-assistant", "--stdin"])

    assert args.area == "channel"
    assert args.command == "set-token"
    assert args.id == "tg-assistant"
    assert args.stdin is True


@pytest.mark.parametrize("command", ["remove", "enable", "disable", "status"])
def test_parse_args_supports_channel_id_commands(command: str) -> None:
    args = cli_main.parse_args(
        [
            "channel",
            command,
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


def test_parse_args_supports_channel_identity_and_group_access_commands() -> None:
    identity = cli_main.parse_args(["channel", "identity", "tg-assistant", "--user", "50"])
    access = cli_main.parse_args(["channel", "access", "tg-assistant", "--group", "-100"])
    grant = cli_main.parse_args(
        [
            "channel",
            "grant-admin",
            "tg-assistant",
            "--group",
            "-100",
            "--user",
            "51",
        ]
    )
    revoke = cli_main.parse_args(
        [
            "channel",
            "revoke-admin",
            "tg-assistant",
            "--group",
            "-100",
            "--user",
            "51",
        ]
    )

    assert (identity.command, identity.id, identity.user) == (
        "identity",
        "tg-assistant",
        "50",
    )
    assert (access.command, access.id, access.access_scope_id) == (
        "access",
        "tg-assistant",
        "-100",
    )
    assert (grant.command, grant.id, grant.access_scope_id, grant.user_id) == (
        "grant-admin",
        "tg-assistant",
        "-100",
        "51",
    )
    assert (revoke.command, revoke.id, revoke.access_scope_id, revoke.user_id) == (
        "revoke-admin",
        "tg-assistant",
        "-100",
        "51",
    )


def test_parse_args_rejects_legacy_owner_flag() -> None:
    with pytest.raises(SystemExit):
        cli_main.parse_args(
            [
                "channel",
                "update",
                "tg-assistant",
                "--owner-user",
                "50",
            ]
        )


def test_parse_args_supports_channel_update_options() -> None:
    args = cli_main.parse_args(
        [
            "channel",
            "update",
            "tg-assistant",
            "--agent",
            "coder",
            "--token-env",
            "TELEGRAM_BOT_TOKEN_CODER",
            "--dm-scope",
            "per_peer",
            "--allow",
            "100",
            "101",
            "--enabled",
            "false",
        ]
    )

    assert args.area == "channel"
    assert args.command == "update"
    assert args.id == "tg-assistant"
    assert args.agent == "coder"
    assert args.token_env == "TELEGRAM_BOT_TOKEN_CODER"
    assert args.dm_scope == "per_peer"
    assert args.allow == ["100", "101"]
    assert args.enabled == "false"
