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


def test_channel_add_posts_create_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
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
        ["100", "101"],
    )

    assert result.ok is True
    assert result.instance is instance
    assert "tg-assistant" in result.message
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
                    "allowed_chat_ids": ["100", "101"],
                },
            },
            "timeout": 10.0,
        }
    ]


def test_channel_add_posts_managed_token_without_echoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": "tg-main",
                    "platform": "telegram",
                    "agent_id": "assistant",
                    "dm_scope": "per_conversation",
                    "enabled": True,
                    "allowed_chat_ids": [],
                    "token_env_var": "VBOT_CHANNEL_TOKEN__74672D6D61696E",
                    "credential": {
                        "key": "VBOT_CHANNEL_TOKEN__74672D6D61696E",
                        "effective_source": "data_dir",
                        "applied": True,
                    },
                    "running": True,
                    "failed": False,
                    "failure_reason": None,
                },
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_add(
        instance,
        "tg-main",
        "telegram",
        "assistant",
        None,
        "per_conversation",
        [],
        token="super-secret-token",
    )

    assert calls[0]["params"]["token"] == "super-secret-token"
    assert "token_env_var" not in calls[0]["params"]
    assert "super-secret-token" not in result.message
    assert "effective_source=data_dir applied=yes" in result.message
    assert "enabled=yes running=yes failed=no" in result.message


def test_channel_set_token_posts_rpc_and_reports_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": "tg-main",
                    "token_env_var": "TELEGRAM_BOT_TOKEN",
                    "credential": {
                        "key": "TELEGRAM_BOT_TOKEN",
                        "effective_source": "process_environment",
                        "applied": False,
                    },
                    "adapter_restart_requested": False,
                    "enabled": True,
                    "running": True,
                    "failed": False,
                    "failure_reason": None,
                },
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_set_token(instance, "tg-main", "rotated-secret")

    assert calls == [
        {
            "method": "channel.set_token",
            "params": {"id": "tg-main", "token": "rotated-secret"},
        }
    ]
    assert "rotated-secret" not in result.message
    assert "effective_source=process_environment applied=no" in result.message


@pytest.mark.parametrize(
    ("command", "method"),
    [
        ("remove", "channel.delete"),
        ("enable", "channel.enable"),
        ("disable", "channel.disable"),
    ],
)
def test_channel_simple_id_commands_post_expected_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    method: str,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)
    function_map = {
        "remove": channel_management.channel_remove,
        "enable": channel_management.channel_enable,
        "disable": channel_management.channel_disable,
    }

    result = function_map[command](instance, "tg-assistant")

    assert result.ok is True
    assert result.instance is instance
    assert "tg-assistant" in result.message
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

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": "tg-assistant",
                    "enabled": True,
                    "running": False,
                    "failed": True,
                    "failure_reason": "Unknown agent_id: missing-agent",
                },
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_status(instance, "tg-assistant")

    assert result == CommandResult(
        ok=True,
        message=(
            "tg-assistant: enabled=yes running=no failed=yes "
            "failure_reason=Unknown agent_id: missing-agent"
        ),
        instance=instance,
    )
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "channel.status", "params": {"id": "tg-assistant"}},
            "timeout": 10.0,
        }
    ]


def test_channel_status_lists_denied_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": "tg-assistant",
                    "enabled": True,
                    "running": True,
                    "failed": False,
                    "failure_reason": None,
                    "denied_chats": [
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
                },
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_status(instance, "tg-assistant")

    assert result.ok is True
    lines = result.message.splitlines()
    assert lines[0] == "tg-assistant: enabled=yes running=yes failed=no"
    assert (
        "- chat_id=99999 kind=direct name=Julian B. last_seen=2026-07-05T12:00:00+00:00 messages=3"
    ) in lines
    assert ("- chat_id=-10001 kind=group last_seen=2026-07-05T11:00:00+00:00 messages=1") in lines
    assert any("vbot channel update tg-assistant --allow" in line for line in lines)


def test_channel_status_omits_denied_chat_block_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": "tg-assistant",
                    "enabled": True,
                    "running": True,
                    "failed": False,
                    "failure_reason": None,
                    "denied_chats": [],
                },
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_status(instance, "tg-assistant")

    assert result == CommandResult(
        ok=True,
        message="tg-assistant: enabled=yes running=yes failed=no",
        instance=instance,
    )


def test_channel_update_posts_update_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_update(
        instance,
        "tg-assistant",
        {
            "agent_id": "coder",
            "token_env_var": "TELEGRAM_BOT_TOKEN_CODER",
            "allowed_chat_ids": [100, 101],
            "enabled": False,
        },
    )

    assert result.ok is True
    assert result.instance is instance
    assert "tg-assistant" in result.message
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {
                "method": "channel.update",
                "params": {
                    "id": "tg-assistant",
                    "agent_id": "coder",
                    "token_env_var": "TELEGRAM_BOT_TOKEN_CODER",
                    "allowed_chat_ids": [100, 101],
                    "enabled": False,
                },
            },
            "timeout": 10.0,
        }
    ]


def test_channel_update_rejects_empty_changes(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = channel_management.channel_update(instance, "tg-assistant", {})

    assert result.ok is False
    assert result.instance is instance
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
        assert option in result.message


def test_channel_list_formats_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
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
    assert result.message.splitlines()[1:] == [
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


def test_channel_access_commands_use_additive_rpc_actions_and_saved_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []
    saved_state = {
        "channel_id": "tg-assistant",
        "self_user_id": "50",
        "groups": [
            {
                "access_scope_id": "-100",
                "admin_user_ids": ["50", "51"],
                "participants": [
                    {
                        "user_id": "50",
                        "display_name": "Alice",
                        "last_seen_at": "2026-07-30T10:00:00+00:00",
                        "role": "admin",
                    },
                    {
                        "user_id": "51",
                        "display_name": "Bob",
                        "last_seen_at": "2026-07-30T10:01:00+00:00",
                        "role": "admin",
                    },
                ],
            }
        ],
    }

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": saved_state})

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    shown_identity = channel_management.channel_identity(instance, "tg-assistant")
    saved_identity = channel_management.channel_identity(instance, "tg-assistant", "50")
    listed = channel_management.channel_access(instance, "tg-assistant", "-100")
    granted = channel_management.channel_grant_admin(instance, "tg-assistant", "-100", "51")
    revoked = channel_management.channel_revoke_admin(instance, "tg-assistant", "-100", "51")

    assert calls == [
        {"method": "channel.access.get", "params": {"id": "tg-assistant"}},
        {
            "method": "channel.identity.set",
            "params": {"id": "tg-assistant", "user_id": "50"},
        },
        {"method": "channel.access.get", "params": {"id": "tg-assistant"}},
        {
            "method": "channel.admin.grant",
            "params": {
                "id": "tg-assistant",
                "access_scope_id": "-100",
                "user_id": "51",
            },
        },
        {
            "method": "channel.admin.revoke",
            "params": {
                "id": "tg-assistant",
                "access_scope_id": "-100",
                "user_id": "51",
            },
        },
    ]
    assert shown_identity.message == "channel=tg-assistant self_user_id=50"
    assert saved_identity.message == "channel=tg-assistant self_user_id=50"
    assert listed.message.splitlines() == [
        "channel=tg-assistant group=-100",
        "admins=50,51",
        "participants:",
        "- user_id=50 name=Alice role=admin",
        "- user_id=51 name=Bob role=admin",
    ]
    assert granted.message == listed.message
    assert revoked.message == listed.message


def test_channel_commands_surface_rpc_domain_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {"code": "channel_not_found", "message": "channel not found: tg-unknown"},
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_enable(instance, "tg-unknown")

    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("channel_not_found:")
    assert "tg-unknown" in result.message


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
    assert output_lines[-1] == f"data_dir: {tmp_path / 'data'}"


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


def test_channel_update_maps_group_response_policy_fields() -> None:
    args = cli_main.parse_args(
        [
            "channel",
            "update",
            "tg-main",
            "--response-mode",
            "all",
            "--mention-pattern",
            "@vbot",
            "bot please",
            "--observe-unaddressed",
            "true",
        ]
    )

    assert cli_main._channel_changes_from_args(args) == {
        "response_mode": "all",
        "mention_patterns": ["@vbot", "bot please"],
        "observe_unaddressed": True,
    }


def test_channel_add_advanced_policy_is_sent_and_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    **json["params"],
                    "enabled": True,
                },
            },
        )

    monkeypatch.setattr(channel_management.httpx, "post", fake_post)

    result = channel_management.channel_add(
        instance,
        "tg-main",
        "telegram",
        "assistant",
        "TELEGRAM_BOT_TOKEN",
        "per_conversation",
        ["123"],
        "all",
        ["@vbot"],
        observe_unaddressed=True,
    )

    assert result.ok is True
    assert "response_mode=all" in result.message
    assert "owner_user_ids" not in calls[0]["params"]
    assert calls[0]["params"]["observe_unaddressed"] is True
