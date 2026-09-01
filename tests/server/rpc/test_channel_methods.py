"""Tests for channel RPC handlers and session channel-linking RPCs."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.channels import ChannelConfig, ChannelConfigError, DeniedChatFacts
from core.sessions import SessionAddress
from server.events import ServerEventBus
from server.rpc.methods import dispatch_rpc


class _NullAsyncContext:
    """Stand-in for the per-session write lock in mocked chat-session managers."""

    async def __aenter__(self) -> _NullAsyncContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _CredentialStorage:
    def __init__(self) -> None:
        self.credentials: dict[str, str] = {}

    def load_environment(self) -> dict[str, str]:
        return dict(self.credentials)

    def set_data_dir_credential(self, key: str, value: str) -> None:
        self.credentials[key] = value

    def remove_data_dir_credential(self, key: str) -> bool:
        return self.credentials.pop(key, None) is not None

    def load_compaction_settings(self) -> dict[str, object]:
        return {}


def _channel_config(
    *,
    channel_id: str = "tg-assistant",
    enabled: bool = True,
    observe_unaddressed: bool = False,
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        platform="telegram",
        agent_id="assistant",
        dm_scope="per_conversation",
        allowed_chat_ids=[],
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=enabled,
        observe_unaddressed=observe_unaddressed,
    )


def _state(
    *,
    channel_service: object | None = None,
    chat_sessions: object | None = None,
    agents: object | None = None,
    process_credentials: dict[str, str] | None = None,
) -> SimpleNamespace:
    agent_store = agents if agents is not None else Mock()
    if isinstance(agent_store, Mock):
        agent_store.get.return_value = SimpleNamespace(id="assistant")

    storage = _CredentialStorage()
    process_values = dict(process_credentials or {})

    def resolve_environment_credential(key: str) -> str:
        if key in process_values:
            return process_values[key]
        return storage.credentials.get(key, "")

    def environment_credential_source(key: str) -> str | None:
        if key in process_values:
            return "process_environment"
        if key in storage.credentials:
            return "data_dir"
        return None

    runtime = SimpleNamespace(
        channel_service=channel_service if channel_service is not None else Mock(),
        reload_channel_tool=Mock(),
        reload_environment_credentials=Mock(),
        resolve_environment_credential=resolve_environment_credential,
        environment_credential_source=environment_credential_source,
        storage=storage,
        chat_sessions=chat_sessions if chat_sessions is not None else Mock(),
        agents=agent_store,
    )
    return SimpleNamespace(runtime=runtime, event_bus=ServerEventBus())


@pytest.mark.asyncio
async def test_channel_list_happy_path_returns_serialized_channels() -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(state, {"method": "channel.list", "params": {}})

    assert response == {"ok": True, "result": {"channels": [config.to_dict()]}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "service_method", "service_args"),
    [
        (
            "channel.access.get",
            {"id": "tg-assistant"},
            "channel_access",
            ("tg-assistant",),
        ),
        (
            "channel.identity.set",
            {"id": "tg-assistant", "user_id": "50"},
            "set_channel_self_user_id",
            ("tg-assistant", "50"),
        ),
        (
            "channel.admin.grant",
            {"id": "tg-assistant", "access_scope_id": "-100", "user_id": "51"},
            "grant_channel_group_admin",
            ("tg-assistant", "-100", "51"),
        ),
        (
            "channel.admin.revoke",
            {"id": "tg-assistant", "access_scope_id": "-100", "user_id": "51"},
            "revoke_channel_group_admin",
            ("tg-assistant", "-100", "51"),
        ),
    ],
)
async def test_channel_access_methods_return_saved_state_without_runtime_reload(
    method: str,
    params: dict[str, str],
    service_method: str,
    service_args: tuple[str, ...],
) -> None:
    saved = {
        "channel_id": "tg-assistant",
        "self_user_id": "50",
        "groups": [
            {
                "access_scope_id": "-100",
                "admin_user_ids": ["50", "51"],
                "participants": [],
            }
        ],
    }
    channel_service = Mock()
    getattr(channel_service, service_method).return_value = saved
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response == {"ok": True, "result": saved}
    getattr(channel_service, service_method).assert_called_once_with(*service_args)
    state.runtime.reload_channel_tool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "channel.create",
            {
                "id": "tg-assistant",
                "platform": "telegram",
                "agent_id": "assistant",
                "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
                "owner_user_ids": ["50"],
            },
        ),
        (
            "channel.update",
            {"id": "tg-assistant", "owner_user_ids": ["50"]},
        ),
    ],
)
async def test_channel_rpc_rejects_legacy_owner_field(
    method: str,
    params: dict[str, object],
) -> None:
    response = await dispatch_rpc(_state(), {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "owner_user_ids" in response["error"]["message"]


@pytest.mark.asyncio
async def test_channel_create_happy_path_calls_service_and_reload() -> None:
    channel_service = Mock()
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "tg-assistant",
                "platform": "telegram",
                "agent_id": "assistant",
                "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
                "observe_unaddressed": True,
            },
        },
    )

    channel_service.create_channel.assert_called_once()
    created_config = channel_service.create_channel.call_args.args[0]
    assert isinstance(created_config, ChannelConfig)
    assert created_config.to_dict() == _channel_config(observe_unaddressed=True).to_dict()
    assert response == {"ok": True, "result": created_config.to_dict()}
    state.runtime.agents.get.assert_called_once_with("assistant")
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert state.event_bus.events[-1]["payload"] == {"kind": "channels"}


@pytest.mark.asyncio
async def test_channel_create_with_managed_token_stores_secret_without_returning_it() -> None:
    channel_service = Mock()
    channel_service.is_running.return_value = True
    channel_service.is_failed.return_value = False
    channel_service.failure_reason.return_value = None
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "tg-main",
                "platform": "telegram",
                "agent_id": "assistant",
                "token": "super-secret-token",
            },
        },
    )

    credential_key = "VBOT_CHANNEL_TOKEN__74672D6D61696E"
    assert response["ok"] is True
    assert "super-secret-token" not in repr(response)
    assert response["result"]["token_env_var"] == credential_key
    assert response["result"]["credential"] == {
        "key": credential_key,
        "saved": True,
        "changed": True,
        "effective_source": "data_dir",
        "applied": True,
    }
    assert response["result"]["running"] is True
    assert response["result"]["failed"] is False
    assert state.runtime.storage.credentials[credential_key] == "super-secret-token"
    state.runtime.reload_environment_credentials.assert_called_once_with()
    created_config = channel_service.create_channel.call_args.args[0]
    assert created_config.token_env_var == credential_key


@pytest.mark.asyncio
async def test_channel_create_rejects_ambiguous_token_inputs() -> None:
    state = _state()

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "tg-main",
                "platform": "telegram",
                "agent_id": "assistant",
                "token": "secret",
                "token_env_var": "TELEGRAM_BOT_TOKEN",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_channel_create_rolls_back_managed_token_when_create_fails() -> None:
    channel_service = Mock()
    channel_service.create_channel.side_effect = ChannelConfigError("create failed")
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "tg-main",
                "platform": "telegram",
                "agent_id": "assistant",
                "token": "super-secret-token",
            },
        },
    )

    assert response["ok"] is False
    assert state.runtime.storage.credentials == {}
    assert state.runtime.reload_environment_credentials.call_count == 2


@pytest.mark.asyncio
async def test_channel_update_happy_path_calls_service_and_reload() -> None:
    channel_service = Mock()
    updated_config = ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope="main",
        allowed_chat_ids=["12345", "-100"],
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=False,
        observe_unaddressed=True,
    )
    channel_service.list_channels.return_value = [updated_config]
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.update",
            "params": {
                "id": "tg-assistant",
                "dm_scope": "main",
                "allowed_chat_ids": [12345, -100],
                "enabled": False,
                "observe_unaddressed": True,
            },
        },
    )

    assert response == {"ok": True, "result": updated_config.to_dict()}
    channel_service.update_channel.assert_called_once_with(
        "tg-assistant",
        dm_scope="main",
        allowed_chat_ids=["12345", "-100"],
        enabled=False,
        observe_unaddressed=True,
    )
    state.runtime.agents.get.assert_not_called()
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert state.event_bus.events[-1]["payload"] == {"kind": "channels"}


@pytest.mark.asyncio
async def test_channel_update_validates_agent_when_agent_id_is_present() -> None:
    channel_service = Mock()
    updated_config = _channel_config()
    channel_service.list_channels.return_value = [updated_config]
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.update",
            "params": {
                "id": "tg-assistant",
                "agent_id": "assistant",
            },
        },
    )

    assert response == {"ok": True, "result": updated_config.to_dict()}
    state.runtime.agents.get.assert_called_once_with("assistant")
    channel_service.update_channel.assert_called_once_with(
        "tg-assistant",
        agent_id="assistant",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "service_method"),
    [
        ("channel.delete", "delete_channel"),
        ("channel.enable", "enable_channel"),
        ("channel.disable", "disable_channel"),
    ],
)
async def test_channel_mutation_methods_call_service_and_reload(
    method: str,
    service_method: str,
) -> None:
    channel_service = Mock()
    config = _channel_config(enabled=method != "channel.disable")
    channel_service.list_channels.return_value = [config]
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": method,
            "params": {
                "id": "tg-assistant",
            },
        },
    )

    expected_result = {"ok": True} if method == "channel.delete" else config.to_dict()
    assert response == {"ok": True, "result": expected_result}
    getattr(channel_service, service_method).assert_called_once_with("tg-assistant")
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert state.event_bus.events[-1]["payload"] == {"kind": "channels"}


@pytest.mark.asyncio
async def test_channel_set_token_reloads_credentials_and_restarts_only_channel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    channel_service.restart_channel.return_value = True
    channel_service.is_running.return_value = True
    channel_service.is_failed.return_value = False
    channel_service.failure_reason.return_value = None
    state = _state(channel_service=channel_service)
    caplog.set_level(logging.INFO, logger="vbot.server.rpc.channels")

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.set_token",
            "params": {"id": "tg-assistant", "token": "rotated-super-secret"},
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "id": "tg-assistant",
            "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
            "credential": {
                "key": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
                "saved": True,
                "changed": True,
                "effective_source": "data_dir",
                "applied": True,
            },
            "adapter_restart_requested": True,
            "enabled": True,
            "running": True,
            "failed": False,
            "failure_reason": None,
        },
    }
    assert state.runtime.storage.credentials == {
        "TELEGRAM_BOT_TOKEN_TG_ASSISTANT": "rotated-super-secret"
    }
    state.runtime.reload_environment_credentials.assert_called_once_with()
    channel_service.restart_channel.assert_called_once_with("tg-assistant")
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert state.event_bus.events[-1]["payload"] == {"kind": "channels"}
    assert all("rotated-super-secret" not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_channel_set_token_reports_process_environment_override_without_restart() -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    channel_service.is_running.return_value = True
    channel_service.is_failed.return_value = False
    channel_service.failure_reason.return_value = None
    state = _state(
        channel_service=channel_service,
        process_credentials={"TELEGRAM_BOT_TOKEN_TG_ASSISTANT": "process-token"},
    )

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.set_token",
            "params": {"id": "tg-assistant", "token": "saved-fallback-token"},
        },
    )

    assert response["ok"] is True
    result = response["result"]
    assert result["credential"]["effective_source"] == "process_environment"
    assert result["credential"]["applied"] is False
    assert result["adapter_restart_requested"] is False
    channel_service.restart_channel.assert_not_called()


@pytest.mark.asyncio
async def test_channel_set_token_rolls_back_credential_when_restart_fails() -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    channel_service.restart_channel.side_effect = [ChannelConfigError("restart failed"), True]
    state = _state(channel_service=channel_service)
    state.runtime.storage.credentials[config.token_env_var] = "old-token"

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.set_token",
            "params": {"id": "tg-assistant", "token": "new-token"},
        },
    )

    assert response["ok"] is False
    assert state.runtime.storage.credentials[config.token_env_var] == "old-token"
    assert channel_service.restart_channel.call_count == 2
    assert state.runtime.reload_environment_credentials.call_count == 2


@pytest.mark.asyncio
async def test_channel_status_happy_path_returns_enabled_and_running() -> None:
    config = _channel_config(enabled=True)
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    channel_service.is_running = Mock(return_value=True)
    channel_service.is_failed = Mock(return_value=False)
    channel_service.failure_reason = Mock(return_value=None)
    channel_service.denied_chats = Mock(return_value=[])
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.status",
            "params": {
                "id": "tg-assistant",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "id": "tg-assistant",
            "enabled": True,
            "running": True,
            "failed": False,
            "failure_reason": None,
            "denied_chats": [],
        },
    }


@pytest.mark.asyncio
async def test_channel_status_returns_failure_reason() -> None:
    config = _channel_config(enabled=True)
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    channel_service.is_running = Mock(return_value=False)
    channel_service.is_failed = Mock(return_value=True)
    channel_service.failure_reason = Mock(return_value="Unknown agent_id: missing-agent")
    channel_service.denied_chats = Mock(return_value=[])
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.status",
            "params": {
                "id": "tg-assistant",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "id": "tg-assistant",
            "enabled": True,
            "running": False,
            "failed": True,
            "failure_reason": "Unknown agent_id: missing-agent",
            "denied_chats": [],
        },
    }


@pytest.mark.asyncio
async def test_channel_status_returns_denied_chats() -> None:
    config = _channel_config(enabled=True)
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    channel_service.is_running = Mock(return_value=True)
    channel_service.is_failed = Mock(return_value=False)
    channel_service.failure_reason = Mock(return_value=None)
    channel_service.denied_chats = Mock(
        return_value=[
            DeniedChatFacts(
                chat_id="99999",
                kind="direct",
                display_name="Julian B.",
                last_seen_at="2026-07-05T12:00:00+00:00",
                count=3,
            )
        ]
    )
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.status",
            "params": {
                "id": "tg-assistant",
            },
        },
    )

    assert response["ok"] is True
    assert response["result"]["denied_chats"] == [
        {
            "chat_id": "99999",
            "kind": "direct",
            "display_name": "Julian B.",
            "last_seen_at": "2026-07-05T12:00:00+00:00",
            "count": 3,
        }
    ]
    channel_service.denied_chats.assert_called_once_with("tg-assistant")


@pytest.mark.asyncio
async def test_session_list_happy_path_returns_bounded_session_summaries() -> None:
    sessions = [
        {
            "id": "ch-tg-assistant-12345",
            "created_at": "2026-05-15T10:00:00+00:00",
            "last_active_at": "2026-05-15T10:05:00+00:00",
            "source_channel_id": "tg-assistant",
            "platform": "telegram",
            "platform_conv_id": "12345",
        }
    ]
    chat_sessions = Mock()
    chat_sessions.list_summaries_page.return_value = SimpleNamespace(
        sessions=tuple(
            {**session, "agent_id": "assistant", "project_id": None} for session in sessions
        ),
        next_cursor=None,
        total_count=1,
    )
    state = _state(chat_sessions=chat_sessions)

    response = await dispatch_rpc(
        state,
        {
            "method": "session.list",
            "params": {
                "agent_id": "assistant",
            },
        },
    )

    assert response == {
        "ok": True,
        "result": {
            "sessions": [
                {
                    **sessions[0],
                    "agent_address": "assistant",
                    "has_active_run": False,
                    "compaction_policy_override": None,
                    "compaction_policy_effective": {},
                }
            ],
            "next_cursor": None,
            "total_count": 1,
        },
    }
    # A bare agent id resolves to the identity scope (project_id=None).
    call = chat_sessions.list_summaries_page.call_args
    assert call.args == ([(None, "assistant")],)
    assert call.kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_session_link_channel_sets_metadata_without_writing_reminder() -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]

    chat_sessions = Mock()
    chat_sessions.get_metadata.return_value = {"persisted": "value"}
    chat_sessions.write_lock.return_value = _NullAsyncContext()
    linked_session = Mock()
    chat_sessions.get.return_value = linked_session

    state = _state(channel_service=channel_service, chat_sessions=chat_sessions)

    response = await dispatch_rpc(
        state,
        {
            "method": "session.link_channel",
            "params": {
                "agent_id": "assistant",
                "session_id": "session-1",
                "channel_id": "tg-assistant",
                "platform_conv_id": "12345",
            },
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    chat_sessions.set_metadata.assert_called_once_with(
        SessionAddress(project_id=None, agent_id="assistant", session_id="session-1"),
        {
            "persisted": "value",
            "source_channel_id": "tg-assistant",
            "platform": "telegram",
            "platform_conv_id": "12345",
            "last_reply_target": {
                "channel_id": "tg-assistant",
                "platform_target": "12345",
            },
        },
    )
    linked_session.add_note.assert_not_called()
    chat_sessions.write_lock.assert_not_called()


@pytest.mark.asyncio
async def test_session_link_channel_rejects_channel_from_other_agent() -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    chat_sessions = Mock()
    state = _state(channel_service=channel_service, chat_sessions=chat_sessions)

    response = await dispatch_rpc(
        state,
        {
            "method": "session.link_channel",
            "params": {
                "agent_id": "writer",
                "session_id": "session-1",
                "channel_id": "tg-assistant",
                "platform_conv_id": "12345",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "channel_config_error"
    assert "tg-assistant" in response["error"]["message"]
    chat_sessions.get.assert_not_called()
    chat_sessions.set_metadata.assert_not_called()


@pytest.mark.asyncio
async def test_channel_create_maps_duplicate_error_to_channel_already_exists() -> None:
    channel_service = Mock()
    channel_service.create_channel.side_effect = ChannelConfigError(
        "Channel already exists: tg-assistant"
    )
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "tg-assistant",
                "platform": "telegram",
                "agent_id": "assistant",
                "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "channel_already_exists"


@pytest.mark.asyncio
async def test_channel_update_maps_config_error_to_channel_config_error() -> None:
    channel_service = Mock()
    channel_service.update_channel.side_effect = ChannelConfigError("invalid channel config")
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.update",
            "params": {
                "id": "tg-assistant",
                "enabled": False,
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "channel_config_error"


@pytest.mark.asyncio
async def test_channel_create_rejects_unknown_agent() -> None:
    state = _state(channel_service=Mock())
    state.runtime.agents.get.side_effect = KeyError("missing")

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "tg-assistant",
                "platform": "telegram",
                "agent_id": "missing",
                "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "channel_config_error"
    assert "missing" in response["error"]["message"]


@pytest.mark.asyncio
async def test_channel_update_rejects_unknown_agent() -> None:
    state = _state(channel_service=Mock())
    state.runtime.agents.get.side_effect = KeyError("missing")

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.update",
            "params": {
                "id": "tg-assistant",
                "agent_id": "missing",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "channel_config_error"
    assert "missing" in response["error"]["message"]


@pytest.mark.asyncio
async def test_channel_status_unknown_channel_returns_channel_not_found() -> None:
    channel_service = Mock()
    channel_service.list_channels.return_value = []
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.status",
            "params": {
                "id": "missing-channel",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "channel_not_found"
    assert "missing-channel" in response["error"]["message"]


@pytest.mark.asyncio
async def test_channel_create_accepts_discord_platform() -> None:
    state = _state(channel_service=Mock())

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "dc-assistant",
                "platform": "discord",
                "agent_id": "assistant",
                "token_env_var": "DISCORD_BOT_TOKEN_DC_ASSISTANT",
            },
        },
    )

    created_config = state.runtime.channel_service.create_channel.call_args.args[0]
    assert response == {"ok": True, "result": created_config.to_dict()}
    assert created_config.platform == "discord"


@pytest.mark.asyncio
async def test_channel_create_rejects_invalid_platform() -> None:
    state = _state(channel_service=Mock())

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "matrix-assistant",
                "platform": "matrix",
                "agent_id": "assistant",
                "token_env_var": "MATRIX_BOT_TOKEN",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_channel_update_rejects_invalid_dm_scope() -> None:
    state = _state(channel_service=Mock())

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.update",
            "params": {
                "id": "tg-assistant",
                "dm_scope": "unsupported",
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
