"""Tests for cli channel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import channel_management
from cli import main as cli_main
from cli.server_management import CommandResult
from tests.cli.cli_channel_test_support import (
    make_instance,
)


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
