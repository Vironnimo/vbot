"""Channel RPCs: configuration, managed credentials, status, access and WhatsApp pairing.

Linking a Session to a Channel is a Session RPC (``test_session_methods.py``).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest

from core.channels import ChannelConfig, ChannelConfigError, ChannelNotFoundError, DeniedChatFacts
from server.events import ServerEventBus
from tests.server.rpc_test_support import resource_changes, rpc_error, rpc_result

JsonObject = dict[str, Any]
_CHANNELS_CHANGED = {"kind": "channels"}
_TOKEN_KEY = "TELEGRAM_BOT_TOKEN_TG_ASSISTANT"
_MANAGED_KEY = "VBOT_CHANNEL_TOKEN__74672D6D61696E"  # managed key of the id "tg-main"
_TELEGRAM = {
    "id": "tg-assistant",
    "platform": "telegram",
    "agent_id": "assistant",
    "token_env_var": _TOKEN_KEY,
}


class _CredentialStorage:
    def __init__(self) -> None:
        self.credentials: dict[str, str] = {}

    def load_environment(self) -> dict[str, str]:
        return dict(self.credentials)

    def set_data_dir_credential(self, key: str, value: str) -> None:
        self.credentials[key] = value

    def remove_data_dir_credential(self, key: str) -> bool:
        return self.credentials.pop(key, None) is not None


def _channel_config(
    *, channel_id: str = "tg-assistant", enabled: bool = True, **changes: Any
) -> ChannelConfig:
    fields: JsonObject = {
        "id": channel_id,
        "platform": "telegram",
        "agent_id": "assistant",
        "dm_scope": "per_conversation",
        "allowed_chat_ids": [],
        "token_env_var": _TOKEN_KEY,
        "enabled": enabled,
    }
    return ChannelConfig(**{**fields, **changes})


def _channel_service(*configs: ChannelConfig) -> Mock:
    """A ChannelService double whose config reads and changes are awaitable like the real ones."""
    service = Mock()
    for name in (
        "create_channel",
        "update_channel",
        "delete_channel",
        "enable_channel",
        "disable_channel",
        "restart_channel",
    ):
        setattr(service, name, AsyncMock())
    saved = {config.id: config for config in configs}

    def get_channel(channel_id: str) -> ChannelConfig:
        if channel_id not in saved:
            raise ChannelNotFoundError(f"Channel not found: {channel_id}")
        return saved[channel_id]

    service.get_channel = AsyncMock(side_effect=get_channel)
    service.list_channels_async = AsyncMock(return_value=list(configs))
    service.is_running.return_value = True
    service.is_failed.return_value = False
    service.failure_reason.return_value = None
    service.denied_chats.return_value = []
    return service


def _state(
    channel_service: Mock | None = None,
    *,
    process_credentials: dict[str, str] | None = None,
) -> SimpleNamespace:
    storage = _CredentialStorage()
    process_values = dict(process_credentials or {})
    # What live credential resolution sees: data-dir values arrive only on reload.
    live: dict[str, str] = {}

    def reload_environment_credentials() -> None:
        live.clear()
        live.update(storage.credentials)

    def resolve_environment_credential(key: str) -> str:
        return process_values.get(key, live.get(key, ""))

    def environment_credential_source(key: str) -> str | None:
        if key in process_values:
            return "process_environment"
        return "data_dir" if key in live else None

    agents = Mock()
    agents.get.return_value = SimpleNamespace(id="assistant")
    runtime = SimpleNamespace(
        channel_service=channel_service if channel_service is not None else _channel_service(),
        reload_channel_tool=Mock(),
        reload_environment_credentials=reload_environment_credentials,
        resolve_environment_credential=resolve_environment_credential,
        environment_credential_source=environment_credential_source,
        storage=storage,
        live_credentials=live,
        agents=agents,
    )
    return SimpleNamespace(runtime=runtime, event_bus=ServerEventBus())


# ---------------------------------------------------------------------------
# channel.list / channel.create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_list_returns_serialized_channels() -> None:
    config = _channel_config()

    result = await rpc_result(_state(_channel_service(config)), "channel.list")

    assert result == {"channels": [config.to_dict()]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {**_TELEGRAM, "observe_unaddressed": True},
            _channel_config(observe_unaddressed=True),
        ),
        (
            {**_TELEGRAM, "id": "dc-assistant", "platform": "discord", "token_env_var": "DC_KEY"},
            _channel_config(channel_id="dc-assistant", platform="discord", token_env_var="DC_KEY"),
        ),
    ],
    ids=["telegram", "discord"],
)
async def test_channel_create_saves_the_config_and_reloads_the_channel_tool(
    params: JsonObject, expected: ChannelConfig
) -> None:
    state = _state()
    service = state.runtime.channel_service

    result = await rpc_result(state, "channel.create", **params)

    [created] = service.create_channel.call_args.args
    assert isinstance(created, ChannelConfig)
    assert created.to_dict() == expected.to_dict()
    assert result == expected.to_dict()
    state.runtime.agents.get.assert_called_once_with("assistant")
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert resource_changes(state) == [_CHANNELS_CHANGED]


@pytest.mark.asyncio
async def test_channel_create_with_managed_token_stores_secret_without_returning_it() -> None:
    state = _state()

    result = await rpc_result(
        state,
        "channel.create",
        id="tg-main",
        platform="telegram",
        agent_id="assistant",
        token="super-secret-token",
    )

    assert "super-secret-token" not in repr(result)
    assert result["token_env_var"] == _MANAGED_KEY
    assert result["credential"] == {
        "key": _MANAGED_KEY,
        "saved": True,
        "changed": True,
        "effective_source": "data_dir",
        "applied": True,
    }
    assert (result["running"], result["failed"]) == (True, False)
    assert state.runtime.storage.credentials == {_MANAGED_KEY: "super-secret-token"}
    assert state.runtime.live_credentials == {_MANAGED_KEY: "super-secret-token"}
    [created] = state.runtime.channel_service.create_channel.call_args.args
    assert created.token_env_var == _MANAGED_KEY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"id": "tg-main", "platform": "telegram", "token": "private-bot-token"},
        # Slack's bot and app tokens are written and rolled back together.
        {
            "id": "slack",
            "platform": "slack",
            "token": "private-bot-token",
            "app_token": "private-app-token",
        },
    ],
    ids=["telegram", "slack"],
)
async def test_failed_channel_create_restores_every_managed_credential(params: JsonObject) -> None:
    service = _channel_service()
    service.create_channel.side_effect = ChannelConfigError("cannot create")
    state = _state(service)
    state.runtime.storage.credentials["UNRELATED"] = "keep"

    error = await rpc_error(state, "channel.create", agent_id="assistant", **params)

    assert error["code"] == "channel_config_error"
    assert "private" not in str(error)
    assert state.runtime.storage.credentials == {"UNRELATED": "keep"}
    assert state.runtime.live_credentials == {"UNRELATED": "keep"}
    assert resource_changes(state) == []


@pytest.mark.asyncio
async def test_whatsapp_creation_needs_no_token_and_pairing_uses_dedicated_rpc() -> None:
    service = _channel_service()
    service.pair_whatsapp = AsyncMock(return_value={"state": "pairing", "qr_image": "private-qr"})
    state = _state(service)

    created = await rpc_result(
        state,
        "channel.create",
        id="wa",
        platform="whatsapp",
        agent_id="assistant",
        allowed_chat_ids=["self"],
        enabled=False,
    )
    paired = await rpc_result(state, "channel.whatsapp.pair", id="wa", reset=True)

    assert created["token_env_var"] == ""
    assert not state.runtime.storage.credentials
    assert paired == {"state": "pairing", "qr_image": "private-qr"}
    service.pair_whatsapp.assert_awaited_once_with("wa", reset=True)
    assert resource_changes(state) == [_CHANNELS_CHANGED, _CHANNELS_CHANGED]


# ---------------------------------------------------------------------------
# channel.update / delete / enable / disable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "updates", "validates_agent"),
    [
        (
            {
                "dm_scope": "main",
                "allowed_chat_ids": [12345, -100],
                "enabled": False,
                "observe_unaddressed": True,
            },
            {
                "dm_scope": "main",
                "allowed_chat_ids": ["12345", "-100"],
                "enabled": False,
                "observe_unaddressed": True,
            },
            False,
        ),
        # A new Agent must exist before the Channel is re-pointed to it.
        ({"agent_id": "assistant"}, {"agent_id": "assistant"}, True),
    ],
    ids=["fields", "agent"],
)
async def test_channel_update_saves_normalized_fields_and_reloads(
    params: JsonObject, updates: JsonObject, validates_agent: bool
) -> None:
    saved = _channel_config(dm_scope="main", allowed_chat_ids=["12345", "-100"], enabled=False)
    state = _state(_channel_service(saved))

    result = await rpc_result(state, "channel.update", id="tg-assistant", **params)

    assert result == saved.to_dict()
    state.runtime.channel_service.update_channel.assert_called_once_with("tg-assistant", **updates)
    assert state.runtime.agents.get.called is validates_agent
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert resource_changes(state) == [_CHANNELS_CHANGED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "service_method"),
    [
        ("channel.delete", "delete_channel"),
        ("channel.enable", "enable_channel"),
        ("channel.disable", "disable_channel"),
    ],
)
async def test_channel_lifecycle_methods_call_service_and_reload(
    method: str, service_method: str
) -> None:
    config = _channel_config(enabled=method != "channel.disable")
    state = _state(_channel_service(config))

    result = await rpc_result(state, method, id="tg-assistant")

    assert result == ({"ok": True} if method == "channel.delete" else config.to_dict())
    getattr(state.runtime.channel_service, service_method).assert_called_once_with("tg-assistant")
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert resource_changes(state) == [_CHANNELS_CHANGED]


# ---------------------------------------------------------------------------
# channel.set_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("process_override", [False, True])
async def test_channel_set_token_saves_the_credential_and_restarts_only_when_it_applies(
    caplog: pytest.LogCaptureFixture, process_override: bool
) -> None:
    state = _state(
        _channel_service(_channel_config()),
        process_credentials={_TOKEN_KEY: "process-token"} if process_override else None,
    )
    service = state.runtime.channel_service
    service.restart_channel.return_value = True
    caplog.set_level(logging.INFO, logger="vbot.server.rpc.channels")

    result = await rpc_result(state, "channel.set_token", id="tg-assistant", token="rotated-secret")

    # A process environment value keeps precedence: the saved fallback does not
    # change the effective token, so the adapter is not restarted.
    assert result == {
        "id": "tg-assistant",
        "token_env_var": _TOKEN_KEY,
        "credential": {
            "key": _TOKEN_KEY,
            "saved": True,
            "changed": True,
            "effective_source": "process_environment" if process_override else "data_dir",
            "applied": not process_override,
        },
        "adapter_restart_requested": not process_override,
        "enabled": True,
        "running": True,
        "failed": False,
        "failure_reason": None,
    }
    assert state.runtime.storage.credentials == {_TOKEN_KEY: "rotated-secret"}
    assert state.runtime.live_credentials == {_TOKEN_KEY: "rotated-secret"}
    assert service.restart_channel.await_args_list == (
        [] if process_override else [call("tg-assistant")]
    )
    state.runtime.reload_channel_tool.assert_called_once_with()
    assert resource_changes(state) == [_CHANNELS_CHANGED]
    assert all("rotated-secret" not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_channel_set_token_rolls_back_credential_when_restart_fails() -> None:
    config = _channel_config()
    service = _channel_service(config)
    service.restart_channel.side_effect = [ChannelConfigError("restart failed"), True]
    state = _state(service)
    state.runtime.storage.credentials[config.token_env_var] = "old-token"
    state.runtime.reload_environment_credentials()

    error = await rpc_error(state, "channel.set_token", id="tg-assistant", token="new-token")

    assert error["code"] == "channel_config_error"
    assert state.runtime.storage.credentials == {_TOKEN_KEY: "old-token"}
    assert state.runtime.live_credentials == {_TOKEN_KEY: "old-token"}
    # The adapter is restarted again on the restored token.
    assert service.restart_channel.await_count == 2
    assert resource_changes(state) == []


# ---------------------------------------------------------------------------
# channel.status / access
# ---------------------------------------------------------------------------


_DENIED_CHAT = DeniedChatFacts(
    chat_id="99999",
    kind="direct",
    display_name="Julian B.",
    last_seen_at="2026-07-05T12:00:00+00:00",
    count=3,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "failure_reason", "denied", "denied_chats"),
    [
        (
            True,
            None,
            [_DENIED_CHAT],
            [
                {
                    "chat_id": "99999",
                    "kind": "direct",
                    "display_name": "Julian B.",
                    "last_seen_at": "2026-07-05T12:00:00+00:00",
                    "count": 3,
                }
            ],
        ),
        (False, "Unknown agent_id: missing-agent", [], []),
    ],
    ids=["running-with-denied-chats", "failed"],
)
async def test_channel_status_reports_health_and_denied_chats(
    running: bool,
    failure_reason: str | None,
    denied: list[DeniedChatFacts],
    denied_chats: list[JsonObject],
) -> None:
    service = _channel_service(_channel_config())
    service.is_running.return_value = running
    service.is_failed.return_value = not running
    service.failure_reason.return_value = failure_reason
    service.denied_chats.return_value = denied

    result = await rpc_result(_state(service), "channel.status", id="tg-assistant")

    assert result == {
        "id": "tg-assistant",
        "enabled": True,
        "running": running,
        "failed": not running,
        "failure_reason": failure_reason,
        "denied_chats": denied_chats,
    }
    # The status of one Channel reads only that Channel's config.
    service.get_channel.assert_awaited_once_with("tg-assistant")
    service.list_channels_async.assert_not_awaited()
    service.denied_chats.assert_called_once_with("tg-assistant")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "service_method", "service_args"),
    [
        ("channel.access.get", {"id": "tg-assistant"}, "channel_access", ("tg-assistant",)),
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
    params: JsonObject,
    service_method: str,
    service_args: tuple[str, ...],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    saved = {
        "channel_id": "tg-assistant",
        "self_user_id": "50",
        "groups": [{"access_scope_id": "-100", "admin_user_ids": ["50", "51"], "participants": []}],
    }
    service = _channel_service()
    setattr(service, service_method, AsyncMock(return_value=saved))
    state = _state(service)

    result = await rpc_result(state, method, **params)

    assert result == saved
    getattr(service, service_method).assert_awaited_once_with(*service_args)
    state.runtime.reload_channel_tool.assert_not_called()
    # Platform user and group ids stay in the saved state; logs name only the channel.
    external_ids = [str(value) for key, value in params.items() if key != "id"]
    messages = [record.getMessage() for record in caplog.records]
    assert not [value for value in external_ids for message in messages if value in message]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "failure", "code", "named"),
    [
        (
            "channel.create",
            {**_TELEGRAM, "owner_user_ids": ["50"]},
            None,
            "invalid_request",
            "owner_user_ids",
        ),
        (
            "channel.update",
            {"id": "tg-assistant", "owner_user_ids": ["50"]},
            None,
            "invalid_request",
            "owner_user_ids",
        ),
        # A Channel takes exactly one token source.
        (
            "channel.create",
            {**_TELEGRAM, "token": "secret"},
            None,
            "invalid_request",
            "token",
        ),
        (
            "channel.create",
            {
                "id": "slack",
                "platform": "slack",
                "agent_id": "assistant",
                "token_env_var": "SAME",
                "app_token_env_var": "SAME",
            },
            None,
            "channel_config_error",
            "",
        ),
        (
            "channel.create",
            {**_TELEGRAM, "platform": "matrix"},
            None,
            "invalid_request",
            "platform",
        ),
        (
            "channel.update",
            {"id": "tg-assistant", "dm_scope": "unsupported"},
            None,
            "invalid_request",
            "dm_scope",
        ),
        (
            "channel.create",
            {**_TELEGRAM, "agent_id": "missing"},
            "unknown_agent",
            "channel_config_error",
            "missing",
        ),
        (
            "channel.update",
            {"id": "tg-assistant", "agent_id": "missing"},
            "unknown_agent",
            "channel_config_error",
            "missing",
        ),
        (
            "channel.create",
            _TELEGRAM,
            ("create_channel", ChannelConfigError("Channel already exists: tg-assistant")),
            "channel_already_exists",
            "tg-assistant",
        ),
        (
            "channel.update",
            {"id": "tg-assistant", "enabled": False},
            ("update_channel", ChannelConfigError("invalid channel config")),
            "channel_config_error",
            "",
        ),
        (
            "channel.status",
            {"id": "missing-channel"},
            None,
            "channel_not_found",
            "missing-channel",
        ),
        # A config that fails to load is reported as such, not as a missing Channel.
        (
            "channel.status",
            {"id": "tg-broken"},
            ("get_channel", ChannelConfigError("Invalid Channel config tg-broken")),
            "channel_config_error",
            "tg-broken",
        ),
        (
            "channel.set_token",
            {"id": "missing-channel", "token": "secret"},
            None,
            "channel_not_found",
            "missing-channel",
        ),
    ],
)
async def test_channel_refusals_store_and_publish_nothing(
    method: str, params: JsonObject, failure: Any, code: str, named: str
) -> None:
    state = _state()
    if failure == "unknown_agent":
        state.runtime.agents.get.side_effect = KeyError("missing")
    elif failure is not None:
        service_method, error = failure
        getattr(state.runtime.channel_service, service_method).side_effect = error

    error = await rpc_error(state, method, **params)

    assert error["code"] == code
    assert named in error["message"]
    assert state.runtime.storage.credentials == {}
    state.runtime.reload_channel_tool.assert_not_called()
    assert resource_changes(state) == []
    if failure is None:
        state.runtime.channel_service.create_channel.assert_not_called()
        state.runtime.channel_service.update_channel.assert_not_called()
