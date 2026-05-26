"""Tests for channel RPC delegates and session channel-linking RPCs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.channels import ChannelConfig, ChannelConfigError
from server.delegates import dispatch_rpc


def _channel_config(
    *,
    channel_id: str = "tg-assistant",
    enabled: bool = True,
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        platform="telegram",
        agent_id="assistant",
        dm_scope="per_conversation",
        allowed_chat_ids=[],
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=enabled,
    )


def _state(
    *,
    channel_service: object | None = None,
    chat_sessions: object | None = None,
    agents: object | None = None,
) -> SimpleNamespace:
    agent_store = agents if agents is not None else Mock()
    if isinstance(agent_store, Mock):
        agent_store.get.return_value = SimpleNamespace(id="assistant")

    runtime = SimpleNamespace(
        channel_service=channel_service if channel_service is not None else Mock(),
        reload_channel_tool=Mock(),
        chat_sessions=chat_sessions if chat_sessions is not None else Mock(),
        agents=agent_store,
    )
    return SimpleNamespace(runtime=runtime)


@pytest.mark.asyncio
async def test_channel_list_happy_path_returns_serialized_channels() -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    state = _state(channel_service=channel_service)

    response = await dispatch_rpc(state, {"method": "channel.list", "params": {}})

    assert response == {"ok": True, "result": {"channels": [config.to_dict()]}}


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
            },
        },
    )

    assert response == {"ok": True, "result": {"id": "tg-assistant"}}
    channel_service.create_channel.assert_called_once()
    created_config = channel_service.create_channel.call_args.args[0]
    assert isinstance(created_config, ChannelConfig)
    assert created_config.to_dict() == _channel_config().to_dict()
    state.runtime.agents.get.assert_called_once_with("assistant")
    state.runtime.reload_channel_tool.assert_called_once_with()


@pytest.mark.asyncio
async def test_channel_update_happy_path_calls_service_and_reload() -> None:
    channel_service = Mock()
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
            },
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    channel_service.update_channel.assert_called_once_with(
        "tg-assistant",
        dm_scope="main",
        allowed_chat_ids=[12345, -100],
        enabled=False,
    )
    state.runtime.agents.get.assert_not_called()
    state.runtime.reload_channel_tool.assert_called_once_with()


@pytest.mark.asyncio
async def test_channel_update_validates_agent_when_agent_id_is_present() -> None:
    channel_service = Mock()
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

    assert response == {"ok": True, "result": {"ok": True}}
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

    assert response == {"ok": True, "result": {"ok": True}}
    getattr(channel_service, service_method).assert_called_once_with("tg-assistant")
    state.runtime.reload_channel_tool.assert_called_once_with()


@pytest.mark.asyncio
async def test_channel_status_happy_path_returns_enabled_and_running() -> None:
    config = _channel_config(enabled=True)
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]
    channel_service._is_running = Mock(return_value=True)
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
        },
    }


@pytest.mark.asyncio
async def test_session_list_happy_path_returns_sessions_with_metadata() -> None:
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
    chat_sessions.list_with_metadata.return_value = sessions
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

    assert response == {"ok": True, "result": {"sessions": sessions}}
    chat_sessions.list_with_metadata.assert_called_once_with("assistant")


@pytest.mark.asyncio
async def test_session_link_channel_sets_metadata_and_writes_system_reminder() -> None:
    config = _channel_config()
    channel_service = Mock()
    channel_service.list_channels.return_value = [config]

    chat_sessions = Mock()
    chat_sessions.get_metadata.return_value = {"persisted": "value"}
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
        "assistant",
        "session-1",
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
    linked_session.add_note.assert_called_once()
    note = linked_session.add_note.call_args.args[0]
    assert "Telegram" in note
    assert "tg-assistant" in note
    assert "12345" in note


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

    assert response == {
        "ok": False,
        "error": {
            "code": "channel_config_error",
            "message": "Channel tg-assistant belongs to agent assistant, not writer",
        },
    }
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

    assert response == {
        "ok": False,
        "error": {
            "code": "channel_already_exists",
            "message": "Channel already exists: tg-assistant",
        },
    }


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

    assert response == {
        "ok": False,
        "error": {
            "code": "channel_config_error",
            "message": "invalid channel config",
        },
    }


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

    assert response == {
        "ok": False,
        "error": {
            "code": "channel_config_error",
            "message": "Unknown agent_id: missing",
        },
    }


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

    assert response == {
        "ok": False,
        "error": {
            "code": "channel_config_error",
            "message": "Unknown agent_id: missing",
        },
    }


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

    assert response == {
        "ok": False,
        "error": {
            "code": "channel_not_found",
            "message": "Channel not found: missing-channel",
        },
    }


@pytest.mark.asyncio
async def test_channel_create_rejects_invalid_platform() -> None:
    state = _state(channel_service=Mock())

    response = await dispatch_rpc(
        state,
        {
            "method": "channel.create",
            "params": {
                "id": "tg-assistant",
                "platform": "discord",
                "agent_id": "assistant",
                "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
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
