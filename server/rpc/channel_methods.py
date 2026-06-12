"""Channel RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.channels import ChannelConfig, ChannelConfigError, ChannelNotFoundError
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_bool,
    _optional_string_list,
    _required_bool,
    _required_string,
    _required_string_list,
)

JsonObject = dict[str, Any]
CHANNEL_PLATFORMS = frozenset(("discord", "telegram"))
CHANNEL_DM_SCOPES = frozenset(("per_conversation", "main", "per_peer", "per_account_channel_peer"))
CHANNEL_RESPONSE_MODES = frozenset(("mention", "all"))


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
        "enabled",
        "response_mode",
        "mention_patterns",
        "owner_user_ids",
        "observe_unaddressed",
    }
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.create fields: {', '.join(unsupported_fields)}",
        )

    config = ChannelConfig(
        id=_required_string(params, "id"),
        platform=_required_channel_platform(params, "platform"),
        agent_id=_required_string(params, "agent_id"),
        dm_scope=_optional_channel_dm_scope(params, "dm_scope", default="per_conversation"),
        allowed_chat_ids=_optional_platform_id_list(params, "allowed_chat_ids", default=[]),
        token_env_var=_required_string(params, "token_env_var"),
        enabled=_optional_bool(params, "enabled", default=True),
        response_mode=_optional_channel_response_mode(params, "response_mode"),
        mention_patterns=_optional_string_list(params, "mention_patterns", default=[]),
        owner_user_ids=_optional_user_id_list(params, "owner_user_ids", default=[]),
        observe_unaddressed=_optional_bool(params, "observe_unaddressed", default=False),
    )

    try:
        async with _agent_reference_lock(state):
            _validate_channel_agent_exists(state, config.agent_id)
            state.runtime.channel_service.create_channel(config)
            state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"id": config.id}


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
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.update fields: {', '.join(unsupported_fields)}",
        )

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

    if "agent_id" in updates:
        try:
            async with _agent_reference_lock(state):
                _validate_channel_agent_exists(state, updates["agent_id"])
                state.runtime.channel_service.update_channel(channel_id, **updates)
                state.runtime.reload_channel_tool()
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return {"ok": True}

    try:
        state.runtime.channel_service.update_channel(channel_id, **updates)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _delete_channel(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.delete fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        state.runtime.channel_service.delete_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _enable_channel(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.enable fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        state.runtime.channel_service.enable_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _disable_channel(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.disable fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        state.runtime.channel_service.disable_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _channel_status(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.status fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        channel_service = state.runtime.channel_service
        config = _channel_config_by_id(channel_service, channel_id)
        running = _channel_is_running(channel_service, channel_id)
        failed = _channel_is_failed(channel_service, channel_id)
        failure_reason = _channel_failure_reason(channel_service, channel_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "id": config.id,
        "enabled": config.enabled,
        "running": running,
        "failed": failed,
        "failure_reason": failure_reason,
    }


def _validate_channel_agent_exists(state: Any, agent_id: str) -> None:
    try:
        state.runtime.agents.get(agent_id)
    except Exception as error:
        raise ChannelConfigError(f"Unknown agent_id: {agent_id}") from error


def _required_channel_platform(params: JsonObject, key: str) -> str:
    platform = _required_string(params, key)
    if platform not in CHANNEL_PLATFORMS:
        options = ", ".join(sorted(CHANNEL_PLATFORMS))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be one of: {options}",
        )
    return platform


def _optional_channel_dm_scope(params: JsonObject, key: str, *, default: str) -> str:
    if key not in params:
        return default

    dm_scope = _required_string(params, key)
    if dm_scope not in CHANNEL_DM_SCOPES:
        options = ", ".join(sorted(CHANNEL_DM_SCOPES))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be one of: {options}",
        )
    return dm_scope


def _optional_channel_response_mode(params: JsonObject, key: str) -> str:
    if key not in params:
        return "mention"

    response_mode = _required_string(params, key)
    if response_mode not in CHANNEL_RESPONSE_MODES:
        options = ", ".join(sorted(CHANNEL_RESPONSE_MODES))
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


def _channel_is_running(channel_service: Any, channel_id: str) -> bool:
    running_checker = getattr(channel_service, "_is_running", None)
    if callable(running_checker):
        return bool(running_checker(channel_id))

    adapter_tasks = getattr(channel_service, "_adapter_tasks", None)
    if isinstance(adapter_tasks, dict):
        task = adapter_tasks.get(channel_id)
        return bool(task is not None and not task.done())
    return False


def _channel_is_failed(channel_service: Any, channel_id: str) -> bool:
    failed_checker = getattr(channel_service, "is_failed", None)
    if callable(failed_checker):
        return bool(failed_checker(channel_id))

    failed_channels = getattr(channel_service, "_failed_channels", None)
    if isinstance(failed_channels, set):
        return channel_id in failed_channels
    return False


def _channel_failure_reason(channel_service: Any, channel_id: str) -> str | None:
    reason_getter = getattr(channel_service, "failure_reason", None)
    if callable(reason_getter):
        reason = reason_getter(channel_id)
        return reason if isinstance(reason, str) and reason else None

    failure_reasons = getattr(channel_service, "_failure_reasons", None)
    if isinstance(failure_reasons, dict):
        reason = failure_reasons.get(channel_id)
        return reason if isinstance(reason, str) and reason else None
    return None


def _channel_system_reminder(platform: str, channel_id: str, platform_conv_id: str) -> str:
    platform_name = platform.capitalize()
    return (
        f"This session is receiving messages via {platform_name} "
        f"(channel: {channel_id}, chat: {platform_conv_id}).\n"
        f"Respond in a style appropriate for {platform_name} messaging."
    )


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return channel RPC handlers."""

    return {
        "channel.list": _list_channels,
        "channel.create": _create_channel,
        "channel.update": _update_channel,
        "channel.delete": _delete_channel,
        "channel.enable": _enable_channel,
        "channel.disable": _disable_channel,
        "channel.status": _channel_status,
    }
