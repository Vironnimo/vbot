"""Tests for the ``vbot channel`` commands: options, RPC requests, secrets and output."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

ADD = ("channel", "add", "tg-assistant", "--platform", "telegram", "--agent", "assistant")
CREATED = {
    "id": "tg-assistant",
    "platform": "telegram",
    "agent_id": "assistant",
    "dm_scope": "per_conversation",
    "allowed_chat_ids": [],
}
LISTENER = {"enabled": True, "running": True, "failed": False, "failure_reason": None}


def _result_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("result: "))


def test_channel_add_sends_the_connection_and_prints_the_channel_frame(
    rpc: FakeRpc, run_cli: RunCli, tmp_path: Path
) -> None:
    rpc.reply("channel.create", {"id": "tg-assistant"})

    code, out, _err = run_cli(
        *ADD, "--token-env", "TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "--dm-scope", "per_peer",
        "--allow", "100", "101",
    )  # fmt: skip

    assert code == 0
    assert rpc.calls == [
        (
            "channel.create",
            CREATED
            | {
                "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
                "dm_scope": "per_peer",
                "allowed_chat_ids": ["100", "101"],
            },
        )
    ]
    lines = out.splitlines()
    assert lines[0] == "command: channel add"
    assert "tg-assistant" in _result_line(out)
    assert lines[-2:] == ["url: http://127.0.0.1:8420", f"local_data_dir: {tmp_path / 'data'}"]


@pytest.mark.parametrize(
    ("platform", "flags", "params", "shown"),
    [
        pytest.param(
            "whatsapp",
            ("--allow", "self"),
            {"allowed_chat_ids": ["self"], "enabled": False},
            (),
            id="whatsapp-saved-disabled-until-paired",
        ),
        pytest.param(
            "slack",
            ("--token-env", "BOT", "--app-token-env", "APP", "--disabled"),
            {"token_env_var": "BOT", "app_token_env_var": "APP", "enabled": False},
            ("app_token_env_var=APP",),
            id="slack-saved-before-credentials",
        ),
        pytest.param(
            "mattermost",
            ("--token-env", "BOT", "--server-url", "https://chat.example"),
            {"token_env_var": "BOT", "server_url": "https://chat.example"},
            (),
            id="mattermost",
        ),
        pytest.param(
            "telegram",
            (
                "--token-env", "TELEGRAM_BOT_TOKEN", "--allow", "123", "--response-mode", "all",
                "--mention-pattern", "@vbot", "--observe-unaddressed", "true",
            ),
            {
                "token_env_var": "TELEGRAM_BOT_TOKEN",
                "allowed_chat_ids": ["123"],
                "response_mode": "all",
                "mention_patterns": ["@vbot"],
                "observe_unaddressed": True,
            },
            ("response_mode=all",),
            id="group-response-policy",
        ),
    ],
)  # fmt: skip
def test_channel_add_sends_the_platform_connection_options(
    rpc: FakeRpc,
    run_cli: RunCli,
    platform: str,
    flags: tuple[str, ...],
    params: dict[str, Any],
    shown: tuple[str, ...],
) -> None:
    expected = CREATED | {"platform": platform} | params
    rpc.reply("channel.create", {**expected, "enabled": params.get("enabled", True)})

    code, out, _err = run_cli(
        "channel", "add", "tg-assistant", "--platform", platform, "--agent", "assistant", *flags
    )

    assert code == 0
    assert rpc.params("channel.create") == expected
    for text in shown:
        assert text in out


def test_channel_add_sends_a_managed_token_from_stdin_without_echoing_it(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("super-secret-token\n"))
    rpc.reply(
        "channel.create",
        CREATED
        | LISTENER
        | {
            "token_env_var": "VBOT_CHANNEL_TOKEN__74672D6D61696E",
            "credential": {
                "key": "VBOT_CHANNEL_TOKEN__74672D6D61696E",
                "effective_source": "data_dir",
                "applied": True,
            },
        },
    )

    code, out, err = run_cli(*ADD, "--token-stdin")

    assert code == 0
    assert rpc.params("channel.create") == CREATED | {"token": "super-secret-token"}
    assert "super-secret-token" not in out + err
    assert "effective_source=data_dir applied=yes" in out
    assert "enabled=yes running=yes failed=no" in out


def test_channel_add_accepts_only_one_token_source(rpc: FakeRpc, run_cli: RunCli) -> None:
    with pytest.raises(SystemExit):
        run_cli(*ADD, "--token-stdin", "--token-env", "TELEGRAM_BOT_TOKEN")

    assert rpc.calls == []


@pytest.mark.parametrize(
    ("argv", "params"),
    [
        pytest.param(
            ("channel", "set-token", "tg-main", "--stdin"),
            {"id": "tg-main", "token": "rotated-secret"},
            id="bot-token",
        ),
        pytest.param(
            ("channel", "token", "set", "tg-main", "--slot", "app", "--stdin"),
            {"id": "tg-main", "token": "rotated-secret", "slot": "app"},
            id="slack-app-token",
        ),
    ],
)
def test_channel_set_token_reads_stdin_and_reports_the_effective_source(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    params: dict[str, str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("rotated-secret\n"))
    rpc.reply(
        "channel.set_token",
        LISTENER
        | {
            "id": "tg-main",
            "token_env_var": "TELEGRAM_BOT_TOKEN",
            "credential": {
                "key": "TELEGRAM_BOT_TOKEN",
                "effective_source": "process_environment",
                "applied": False,
            },
            "adapter_restart_requested": False,
        },
    )

    code, out, err = run_cli(*argv)

    assert code == 0
    assert rpc.calls == [("channel.set_token", params)]
    assert "rotated-secret" not in out + err
    assert "effective_source=process_environment applied=no" in out


@pytest.mark.parametrize(
    ("command", "method"),
    [("remove", "channel.delete"), ("enable", "channel.enable"), ("disable", "channel.disable")],
)
def test_channel_id_commands_address_the_channel(
    rpc: FakeRpc, run_cli: RunCli, command: str, method: str
) -> None:
    rpc.reply(method, {"ok": True})

    code, out, _err = run_cli("channel", command, "tg-assistant")

    assert code == 0
    assert rpc.calls == [(method, {"id": "tg-assistant"})]
    assert out.splitlines()[0] == f"command: channel {command}"
    assert "tg-assistant" in _result_line(out)


def test_channel_command_reports_a_domain_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail("channel.enable", "channel_not_found", "channel not found: tg-unknown", status=200)

    code, out, err = run_cli("channel", "enable", "tg-unknown")

    assert code == 1
    assert "tg-unknown" in _result_line(out)
    assert "rpc_method: channel.enable" in err


def test_channel_status_reports_a_failed_listener(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "channel.status",
        {
            "id": "tg-assistant",
            "enabled": True,
            "running": False,
            "failed": True,
            "failure_reason": "Unknown agent_id: missing-agent",
        },
    )

    code, out, err = run_cli("channel", "status", "tg-assistant")

    assert code == 0
    assert rpc.calls == [("channel.status", {"id": "tg-assistant"})]
    assert _result_line(out) == (
        "result: tg-assistant: enabled=yes running=no failed=yes "
        "failure_reason=Unknown agent_id: missing-agent"
    )
    assert "Channel listener failed; inspect the failure details" in err


@pytest.mark.parametrize(
    ("denied_chats", "expected"),
    [
        pytest.param([], ["result: tg-assistant: enabled=yes running=yes failed=no"], id="none"),
        pytest.param(
            [
                {
                    "chat_id": "99999",
                    "kind": "direct",
                    "display_name": "Julian B.",
                    "last_seen_at": "2026-07-05T12:00:00+00:00",
                    "count": 3,
                },
                {
                    "chat_id": "-10001",
                    "kind": "group",
                    "display_name": None,
                    "last_seen_at": "2026-07-05T11:00:00+00:00",
                    "count": 1,
                },
            ],
            [
                "result: tg-assistant: enabled=yes running=yes failed=no",
                "- chat_id=99999 kind=direct name=Julian B. "
                "last_seen=2026-07-05T12:00:00+00:00 messages=3",
                "- chat_id=-10001 kind=group last_seen=2026-07-05T11:00:00+00:00 messages=1",
            ],
            id="denied",
        ),
    ],
)
def test_channel_status_lists_the_denied_chats(
    rpc: FakeRpc, run_cli: RunCli, denied_chats: list[dict[str, Any]], expected: list[str]
) -> None:
    rpc.reply("channel.status", {"id": "tg-assistant", **LISTENER, "denied_chats": denied_chats})

    code, out, _err = run_cli("channel", "status", "tg-assistant")

    lines = out.splitlines()
    assert code == 0
    for line in expected:
        assert line in lines
    allow_hint = any("vbot channel update tg-assistant --allow" in line for line in lines)
    assert allow_hint is bool(denied_chats)
    if not denied_chats:
        assert len(lines) == 4


@pytest.mark.parametrize(
    ("options", "changes"),
    [
        pytest.param(
            (
                "--agent", "coder", "--token-env", "TELEGRAM_BOT_TOKEN_CODER",
                "--dm-scope", "per_peer", "--allow", "100", "101", "--enabled", "false",
            ),
            {
                "agent_id": "coder",
                "token_env_var": "TELEGRAM_BOT_TOKEN_CODER",
                "dm_scope": "per_peer",
                "allowed_chat_ids": ["100", "101"],
                "enabled": False,
            },
            id="connection",
        ),
        pytest.param(
            (
                "--response-mode", "all", "--mention-pattern", "@vbot", "bot please",
                "--observe-unaddressed", "true",
            ),
            {
                "response_mode": "all",
                "mention_patterns": ["@vbot", "bot please"],
                "observe_unaddressed": True,
            },
            id="group-response-policy",
        ),
    ],
)  # fmt: skip
def test_channel_update_sends_only_the_given_changes(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], changes: dict[str, Any]
) -> None:
    rpc.reply("channel.update", {"ok": True})

    code, out, _err = run_cli("channel", "update", "tg-assistant", *options)

    assert code == 0
    assert rpc.calls == [("channel.update", {"id": "tg-assistant", **changes})]
    assert "tg-assistant" in _result_line(out)


def test_channel_update_without_changes_names_every_option(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, _err = run_cli("channel", "update", "tg-assistant")

    assert code == 1
    assert rpc.calls == []
    for option in (
        "--platform",
        "--agent",
        "--token-env",
        "--dm-scope",
        "--allow",
        "--enabled",
        "--response-mode",
        "--mention-pattern",
        "--observe-unaddressed",
    ):
        assert option in out


def test_channel_update_rejects_the_legacy_owner_flag(rpc: FakeRpc, run_cli: RunCli) -> None:
    with pytest.raises(SystemExit):
        run_cli("channel", "update", "tg-assistant", "--owner-user", "50")

    assert rpc.calls == []


def test_channel_list_prints_one_row_per_channel(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "channel.list",
        {
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
    )

    code, out, _err = run_cli("channel", "list")

    assert code == 0
    assert rpc.calls == [("channel.list", {})]
    lines = out.splitlines()
    assert lines[2:4] == [
        "- id=tg-assistant platform=telegram agent=assistant "
        "dm_scope=per_conversation enabled=yes allowed_chat_ids=123,456 "
        "token_env_var=TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        "- id=tg-work platform=telegram agent=assistant dm_scope=main "
        "enabled=no allowed_chat_ids=- token_env_var=TELEGRAM_BOT_TOKEN_TG_WORK",
    ]


def test_channel_access_commands_use_additive_actions_and_print_the_saved_state(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    saved_state = {
        "channel_id": "tg-assistant",
        "self_user_id": "50",
        "groups": [
            {
                "access_scope_id": "-100",
                "admin_user_ids": ["50", "51"],
                "participants": [
                    {"user_id": "50", "display_name": "Alice", "role": "admin"},
                    {"user_id": "51", "display_name": "Bob", "role": "admin"},
                ],
            }
        ],
    }
    for method in (
        "channel.access.get",
        "channel.identity.set",
        "channel.admin.grant",
        "channel.admin.revoke",
    ):
        rpc.reply(method, saved_state)
    admin = ("--group", "-100", "--user", "51")

    outputs = [
        run_cli("channel", "identity", "tg-assistant")[1],
        run_cli("channel", "identity", "tg-assistant", "--user", "50")[1],
        run_cli("channel", "access", "tg-assistant", "--group", "-100")[1],
        run_cli("channel", "grant-admin", "tg-assistant", *admin)[1],
        run_cli("channel", "revoke-admin", "tg-assistant", *admin)[1],
    ]

    admin_params = {"id": "tg-assistant", "access_scope_id": "-100", "user_id": "51"}
    assert rpc.calls == [
        ("channel.access.get", {"id": "tg-assistant"}),
        ("channel.identity.set", {"id": "tg-assistant", "user_id": "50"}),
        ("channel.access.get", {"id": "tg-assistant"}),
        ("channel.admin.grant", admin_params),
        ("channel.admin.revoke", admin_params),
    ]
    identity = "result: channel=tg-assistant self_user_id=50"
    assert [_result_line(out) for out in outputs[:2]] == [identity, identity]
    access = [
        "result: channel=tg-assistant group=-100",
        "admins=50,51",
        "participants:",
        "- user_id=50 name=Alice role=admin",
        "- user_id=51 name=Bob role=admin",
    ]
    for out in outputs[2:]:
        assert out.splitlines()[1:6] == access


def test_channel_whatsapp_status_omits_the_private_qr_and_reports_setup_attention(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "channel.whatsapp.status",
        {
            "id": "wa",
            "installed": False,
            "setup": "failed",
            "error": "Install Node.js",
            "qr_image": "private-qr",
        },
    )

    code, out, err = run_cli("channel", "whatsapp", "status", "wa")

    assert code == 0
    assert rpc.calls == [("channel.whatsapp.status", {"id": "wa"})]
    assert "private-qr" not in out + err
    assert "Install Node.js" in out
    assert "WhatsApp needs attention" in err


def test_channel_whatsapp_pair_can_reset_the_linked_account(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("channel.whatsapp.pair", {"id": "wa", "state": "pairing"})

    code, _out, _err = run_cli("channel", "whatsapp", "pair", "wa", "--reset")

    assert code == 0
    assert rpc.calls == [("channel.whatsapp.pair", {"id": "wa", "reset": True})]
