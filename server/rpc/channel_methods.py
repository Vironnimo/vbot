"""Channel RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.channels import (
    ALLOWED_CHANNEL_DM_SCOPES,
    ALLOWED_CHANNEL_PLATFORMS,
    ALLOWED_CHANNEL_RESPONSE_MODES,
    ChannelConfig,
    ChannelConfigError,
    ChannelNotFoundError,
    managed_channel_token_env_var,
)
from core.utils.logging import get_logger
from server.events import RESOURCE_KIND_CHANNELS
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import (
    _optional_bool,
    _optional_string_list,
    _reject_unsupported,
    _required_bool,
    _required_string,
    _required_string_list,
)

JsonObject = dict[str, Any]
_LOGGER = get_logger("server.rpc.channels")


def _list_channels(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "channel.list does not accept params")

    try:
        channels = [config.to_dict() for config in state.runtime.channel_service.list_channels()]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"channels": channels}


async def _create_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "id",
        "platform",
        "agent_id",
        "dm_scope",
        "allowed_chat_ids",
        "token_env_var",
        "token",
        "enabled",
        "response_mode",
        "mention_patterns",
        "owner_user_ids",
        "observe_unaddressed",
    }
    _reject_unsupported(params, supported_fields, "channel.create")

    channel_id = _required_string(params, "id")
    token_env_var, managed_token = _channel_token_input(params, channel_id)
    config = ChannelConfig(
        id=channel_id,
        platform=_required_channel_platform(params, "platform"),
        agent_id=_required_string(params, "agent_id"),
        dm_scope=_optional_channel_dm_scope(params, "dm_scope", default="per_conversation"),
        allowed_chat_ids=_optional_platform_id_list(params, "allowed_chat_ids", default=[]),
        token_env_var=token_env_var,
        enabled=_optional_bool(params, "enabled", default=True),
        response_mode=_optional_channel_response_mode(params, "response_mode"),
        mention_patterns=_optional_string_list(params, "mention_patterns", default=[]),
        owner_user_ids=_optional_user_id_list(params, "owner_user_ids", default=[]),
        observe_unaddressed=_optional_bool(params, "observe_unaddressed", default=False),
    )

    credential_result: JsonObject | None = None
    try:
        async with _agent_reference_lock(state):
            _validate_channel_agent_exists(state, config.agent_id)
            if managed_token is None:
                state.runtime.channel_service.create_channel(config)
            else:
                previous_value = state.runtime.storage.load_environment().get(token_env_var)
                try:
                    credential_result = _store_channel_token(
                        state.runtime,
                        token_env_var,
                        managed_token,
                    )
                    state.runtime.channel_service.create_channel(config)
                except Exception:
                    _restore_channel_token(state.runtime, token_env_var, previous_value)
                    raise
            state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
    _LOGGER.info(
        "Channel created (channel=%s platform=%s enabled=%s)",
        config.id,
        config.platform,
        config.enabled,
    )
    result = config.to_dict()
    if credential_result is not None:
        result["credential"] = credential_result
        result.update(_channel_health(state.runtime.channel_service, config))
    return result


async def _update_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "id",
        "platform",
        "agent_id",
        "dm_scope",
        "allowed_chat_ids",
        "token_env_var",
        "enabled",
        "response_mode",
        "mention_patterns",
        "owner_user_ids",
        "observe_unaddressed",
    }
    _reject_unsupported(params, supported_fields, "channel.update")

    channel_id = _required_string(params, "id")
    updates: JsonObject = {}
    if "platform" in params:
        updates["platform"] = _required_channel_platform(params, "platform")
    if "agent_id" in params:
        updates["agent_id"] = _required_string(params, "agent_id")
    if "dm_scope" in params:
        updates["dm_scope"] = _optional_channel_dm_scope(params, "dm_scope", default="")
    if "allowed_chat_ids" in params:
        updates["allowed_chat_ids"] = _required_platform_id_list(params, "allowed_chat_ids")
    if "token_env_var" in params:
        updates["token_env_var"] = _required_string(params, "token_env_var")
    if "enabled" in params:
        updates["enabled"] = _required_bool(params, "enabled")
    if "response_mode" in params:
        updates["response_mode"] = _optional_channel_response_mode(params, "response_mode")
    if "mention_patterns" in params:
        updates["mention_patterns"] = _required_string_list(params, "mention_patterns")
    if "owner_user_ids" in params:
        updates["owner_user_ids"] = _required_user_id_list(params, "owner_user_ids")
    if "observe_unaddressed" in params:
        updates["observe_unaddressed"] = _required_bool(params, "observe_unaddressed")

    previous_config = _channel_config_if_available(state.runtime.channel_service, channel_id)

    if "agent_id" in updates:
        try:
            async with _agent_reference_lock(state):
                _validate_channel_agent_exists(state, updates["agent_id"])
                state.runtime.channel_service.update_channel(channel_id, **updates)
                state.runtime.reload_channel_tool()
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
        saved_config = _channel_config_by_id(state.runtime.channel_service, channel_id)
        _log_channel_update(channel_id, previous_config, saved_config, updates)
        return saved_config.to_dict()

    try:
        state.runtime.channel_service.update_channel(channel_id, **updates)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
    saved_config = _channel_config_by_id(state.runtime.channel_service, channel_id)
    _log_channel_update(channel_id, previous_config, saved_config, updates)
    return saved_config.to_dict()


def _delete_channel(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "channel.delete")

    channel_id = _required_string(params, "id")
    try:
        state.runtime.channel_service.delete_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
    _LOGGER.info("Channel deleted (channel=%s)", channel_id)
    return {"ok": True}


def _enable_channel(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "channel.enable")

    channel_id = _required_string(params, "id")
    try:
        channel_service = state.runtime.channel_service
        previous_config = _channel_config_by_id(channel_service, channel_id)
        was_running = bool(channel_service.is_running(channel_id))
        channel_service.enable_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
    saved_config = _channel_config_by_id(state.runtime.channel_service, channel_id)
    if not previous_config.enabled or not was_running:
        _LOGGER.info("Channel enabled (channel=%s)", channel_id)
    return saved_config.to_dict()


def _disable_channel(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "channel.disable")

    channel_id = _required_string(params, "id")
    try:
        channel_service = state.runtime.channel_service
        previous_config = _channel_config_by_id(channel_service, channel_id)
        was_running = bool(channel_service.is_running(channel_id))
        channel_service.disable_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
    saved_config = _channel_config_by_id(state.runtime.channel_service, channel_id)
    if previous_config.enabled or was_running:
        _LOGGER.info("Channel disabled (channel=%s)", channel_id)
    return saved_config.to_dict()


def _set_channel_token(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "token"}, "channel.set_token")

    channel_id = _required_string(params, "id")
    token = _required_string(params, "token")
    channel_service = state.runtime.channel_service
    config = _channel_config_by_id(channel_service, channel_id)
    credential_key = config.token_env_var
    previous_value = state.runtime.storage.load_environment().get(credential_key)
    previous_effective_value = state.runtime.resolve_environment_credential(credential_key)
    credential_result: JsonObject
    adapter_restart_requested = False
    adapter_restart_attempted = False

    try:
        credential_result = _store_channel_token(state.runtime, credential_key, token)
        effective_value = state.runtime.resolve_environment_credential(credential_key)
        if previous_effective_value != effective_value:
            adapter_restart_attempted = True
            adapter_restart_requested = bool(channel_service.restart_channel(channel_id))
        state.runtime.reload_channel_tool()
    except Exception as exc:
        _restore_channel_token(state.runtime, credential_key, previous_value)
        if adapter_restart_attempted:
            try:
                channel_service.restart_channel(channel_id)
            except Exception as rollback_error:
                _LOGGER.error(
                    "Channel token rollback could not restore the adapter "
                    "(channel=%s credential_key=%s): %s",
                    channel_id,
                    credential_key,
                    rollback_error,
                    exc_info=(
                        type(rollback_error),
                        rollback_error,
                        rollback_error.__traceback__,
                    ),
                )
        raise _map_expected_error(exc) from exc

    if credential_result["changed"] or adapter_restart_requested:
        _LOGGER.info(
            "Channel token saved (channel=%s credential_key=%s effective_source=%s)",
            channel_id,
            credential_key,
            credential_result["effective_source"],
        )
    if credential_result["changed"] or adapter_restart_requested:
        publish_resource_changed(state, RESOURCE_KIND_CHANNELS)

    return {
        "id": config.id,
        "token_env_var": credential_key,
        "credential": credential_result,
        "adapter_restart_requested": adapter_restart_requested,
        **_channel_health(channel_service, config),
    }


def _log_channel_update(
    channel_id: str,
    previous_config: ChannelConfig | None,
    saved_config: ChannelConfig,
    updates: JsonObject,
) -> None:
    if previous_config is None:
        return
    changed_fields = sorted(
        field
        for field in updates
        if getattr(previous_config, field, None) != getattr(saved_config, field, None)
    )
    if changed_fields:
        _LOGGER.info(
            "Channel updated (channel=%s fields=%s)",
            channel_id,
            ",".join(changed_fields),
        )


def _channel_config_if_available(channel_service: Any, channel_id: str) -> ChannelConfig | None:
    """Best-effort pre-mutation snapshot used only to suppress no-op audit logs."""
    try:
        return _channel_config_by_id(channel_service, channel_id)
    except (ChannelConfigError, ChannelNotFoundError, TypeError):
        return None


def _channel_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "channel.status")

    channel_id = _required_string(params, "id")
    try:
        channel_service = state.runtime.channel_service
        config = _channel_config_by_id(channel_service, channel_id)
        health = _channel_health(channel_service, config)
        denied_chats = [entry.to_dict() for entry in channel_service.denied_chats(channel_id)]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "id": config.id,
        **health,
        "denied_chats": denied_chats,
    }


def _channel_token_input(params: JsonObject, channel_id: str) -> tuple[str, str | None]:
    has_env_var = "token_env_var" in params
    has_managed_token = "token" in params
    if has_env_var == has_managed_token:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "channel.create requires exactly one of 'token_env_var' or 'token'",
        )
    if has_managed_token:
        return managed_channel_token_env_var(channel_id), _required_string(params, "token")
    return _required_string(params, "token_env_var"), None


def _store_channel_token(runtime: Any, credential_key: str, token: str) -> JsonObject:
    previous_value = runtime.storage.load_environment().get(credential_key)
    runtime.storage.set_data_dir_credential(credential_key, token)
    runtime.reload_environment_credentials()
    effective_source = runtime.environment_credential_source(credential_key)
    return {
        "key": credential_key,
        "saved": True,
        "changed": previous_value != token,
        "effective_source": effective_source,
        "applied": effective_source == "data_dir",
    }


def _restore_channel_token(runtime: Any, credential_key: str, previous_value: str | None) -> None:
    try:
        if previous_value is None:
            runtime.storage.remove_data_dir_credential(credential_key)
        else:
            runtime.storage.set_data_dir_credential(credential_key, previous_value)
        runtime.reload_environment_credentials()
    except Exception as rollback_error:
        _LOGGER.error(
            "Channel token rollback failed (credential_key=%s): %s",
            credential_key,
            rollback_error,
            exc_info=(type(rollback_error), rollback_error, rollback_error.__traceback__),
        )


def _channel_health(channel_service: Any, config: ChannelConfig) -> JsonObject:
    failure_reason = channel_service.failure_reason(config.id)
    return {
        "enabled": config.enabled,
        "running": bool(channel_service.is_running(config.id)),
        "failed": bool(channel_service.is_failed(config.id)),
        "failure_reason": (
            failure_reason if isinstance(failure_reason, str) and failure_reason else None
        ),
    }


def _validate_channel_agent_exists(state: Any, agent_id: str) -> None:
    try:
        state.runtime.agents.get(agent_id)
    except Exception as error:
        raise ChannelConfigError(f"Unknown agent_id: {agent_id}") from error


def _required_channel_platform(params: JsonObject, key: str) -> str:
    platform = _required_string(params, key)
    if platform not in ALLOWED_CHANNEL_PLATFORMS:
        options = ", ".join(sorted(ALLOWED_CHANNEL_PLATFORMS))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be one of: {options}",
        )
    return platform


def _optional_channel_dm_scope(params: JsonObject, key: str, *, default: str) -> str:
    if key not in params:
        return default

    dm_scope = _required_string(params, key)
    if dm_scope not in ALLOWED_CHANNEL_DM_SCOPES:
        options = ", ".join(sorted(ALLOWED_CHANNEL_DM_SCOPES))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be one of: {options}",
        )
    return dm_scope


def _optional_channel_response_mode(params: JsonObject, key: str) -> str:
    if key not in params:
        return "mention"

    response_mode = _required_string(params, key)
    if response_mode not in ALLOWED_CHANNEL_RESPONSE_MODES:
        options = ", ".join(sorted(ALLOWED_CHANNEL_RESPONSE_MODES))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be one of: {options}",
        )
    return response_mode


def _required_user_id_list(params: JsonObject, key: str) -> list[str]:
    value = params.get(key)
    if not isinstance(value, list):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be a list of platform user ids",
        )

    parsed: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.{key} must contain strings or integers only",
            )
        normalized = str(item).strip()
        if not normalized:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.{key} must not contain empty values",
            )
        parsed.append(normalized)
    return parsed


def _required_platform_id_list(params: JsonObject, key: str) -> list[str]:
    value = params.get(key)
    if not isinstance(value, list):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be a list of platform ids",
        )

    parsed: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.{key} must contain strings or integers only",
            )
        normalized = str(item).strip()
        if not normalized:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.{key} must not contain empty values",
            )
        parsed.append(normalized)
    return parsed


def _optional_platform_id_list(
    params: JsonObject,
    key: str,
    *,
    default: list[str],
) -> list[str]:
    if key not in params:
        return list(default)
    return _required_platform_id_list(params, key)


def _optional_user_id_list(params: JsonObject, key: str, *, default: list[str]) -> list[str]:
    if key not in params:
        return list(default)
    return _required_user_id_list(params, key)


def _channel_config_by_id(channel_service: Any, channel_id: str) -> ChannelConfig:
    for config in channel_service.list_channels():
        if config.id == channel_id:
            return cast(ChannelConfig, config)
    raise ChannelNotFoundError(f"Channel not found: {channel_id}")


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return channel RPC handlers."""

    return {
        "channel.list": _list_channels,
        "channel.create": _create_channel,
        "channel.update": _update_channel,
        "channel.delete": _delete_channel,
        "channel.enable": _enable_channel,
        "channel.disable": _disable_channel,
        "channel.set_token": _set_channel_token,
        "channel.status": _channel_status,
    }
