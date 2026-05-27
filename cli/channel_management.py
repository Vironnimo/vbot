"""Channel management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0
CHANNEL_UPDATE_FLAGS = (
    "--platform",
    "--agent",
    "--token-env",
    "--dm-scope",
    "--allow",
    "--enabled",
)


def channel_add(
    instance: ServerInstance,
    channel_id: str,
    platform: str,
    agent_id: str,
    token_env: str,
    dm_scope: str,
    allowed_chat_ids: Sequence[int],
) -> CommandResult:
    """Create a channel configuration via `channel.create` RPC."""

    params = {
        "id": channel_id,
        "platform": platform,
        "agent_id": agent_id,
        "token_env_var": token_env,
        "dm_scope": dm_scope,
        "allowed_chat_ids": list(allowed_chat_ids),
    }
    payload = _rpc_call(instance, "channel.create", params)
    if not payload.ok:
        return payload.to_command_result()
    created_id = _string_or_default(payload.data.get("id"), channel_id)
    return CommandResult(ok=True, message=f"created {created_id}", instance=instance)


def channel_list(instance: ServerInstance) -> CommandResult:
    """Return formatted channel list output from `channel.list` RPC."""

    payload = _rpc_call(instance, "channel.list", {})
    if not payload.ok:
        return payload.to_command_result()
    channels = payload.data.get("channels")
    if not isinstance(channels, list):
        return CommandResult(
            ok=False, message="RPC result missing channels list", instance=instance
        )
    return CommandResult(ok=True, message=_format_channel_rows(channels), instance=instance)


def channel_remove(instance: ServerInstance, channel_id: str) -> CommandResult:
    """Delete a channel configuration via `channel.delete` RPC."""

    payload = _rpc_call(instance, "channel.delete", {"id": channel_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"removed {channel_id}", instance=instance)


def channel_update(
    instance: ServerInstance,
    channel_id: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Update a channel configuration via `channel.update` RPC."""

    if not changes:
        return CommandResult(
            ok=False,
            message=f"no channel fields provided; use one of: {', '.join(CHANNEL_UPDATE_FLAGS)}",
            instance=instance,
        )

    payload = _rpc_call(instance, "channel.update", {"id": channel_id, **dict(changes)})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"updated {channel_id}", instance=instance)


def channel_enable(instance: ServerInstance, channel_id: str) -> CommandResult:
    """Enable a channel listener via `channel.enable` RPC."""

    payload = _rpc_call(instance, "channel.enable", {"id": channel_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"enabled {channel_id}", instance=instance)


def channel_disable(instance: ServerInstance, channel_id: str) -> CommandResult:
    """Disable a channel listener via `channel.disable` RPC."""

    payload = _rpc_call(instance, "channel.disable", {"id": channel_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=f"disabled {channel_id}", instance=instance)


def channel_status(instance: ServerInstance, channel_id: str) -> CommandResult:
    """Fetch channel runtime status via `channel.status` RPC."""

    payload = _rpc_call(instance, "channel.status", {"id": channel_id})
    if not payload.ok:
        return payload.to_command_result()
    resolved_id = _string_or_default(payload.data.get("id"), channel_id)
    enabled_text = _bool_text(payload.data.get("enabled"))
    running_text = _bool_text(payload.data.get("running"))
    return CommandResult(
        ok=True,
        message=f"{resolved_id}: enabled={enabled_text} running={running_text}",
        instance=instance,
    )


class _RpcPayload:
    def __init__(
        self,
        *,
        ok: bool,
        instance: ServerInstance,
        data: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None:
        self.ok = ok
        self.instance = instance
        self.data = data or {}
        self.message = message

    def to_command_result(self) -> CommandResult:
        return CommandResult(ok=False, message=self.message, instance=self.instance)


def _rpc_call(instance: ServerInstance, method: str, params: dict[str, Any]) -> _RpcPayload:
    """Call one server RPC method and return normalized success/error payload."""

    request_body = {"method": method, "params": params}
    try:
        response = httpx.post(
            f"{instance.url}{RPC_PATH}",
            json=request_body,
            timeout=RPC_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC request failed: {exc.__class__.__name__}",
        )

    try:
        payload = response.json()
    except ValueError:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC response was not JSON (HTTP {response.status_code})",
        )

    if not isinstance(payload, dict):
        return _RpcPayload(ok=False, instance=instance, message="RPC response must be an object")

    if response.status_code != httpx.codes.OK:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(
                payload.get("error"),
                fallback=f"RPC request failed with HTTP {response.status_code}",
            ),
        )

    ok_flag = payload.get("ok")
    if ok_flag is True:
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return _RpcPayload(ok=False, instance=instance, message="RPC result must be an object")
        return _RpcPayload(ok=True, instance=instance, data=result)
    if ok_flag is False:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(payload.get("error"), fallback="RPC request failed"),
        )

    return _RpcPayload(ok=False, instance=instance, message="RPC response missing boolean ok flag")


def _rpc_error_message(error: object, *, fallback: str) -> str:
    """Format a stable error message from server RPC error payload."""

    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(code, str) and isinstance(message, str):
            return f"{code}: {message}"
        if isinstance(message, str):
            return message
    return fallback


def _format_channel_rows(channels: Sequence[object]) -> str:
    if not channels:
        return "no channels configured"

    lines = ["channels:"]
    for channel in channels:
        lines.append(_format_channel_row(channel))
    return "\n".join(lines)


def _format_channel_row(channel: object) -> str:
    if not isinstance(channel, dict):
        return "- invalid channel entry"

    channel_id = _string_or_default(channel.get("id"), "?")
    platform = _string_or_default(channel.get("platform"), "?")
    agent_id = _string_or_default(channel.get("agent_id"), "?")
    dm_scope = _string_or_default(channel.get("dm_scope"), "?")
    token_env_var = _string_or_default(channel.get("token_env_var"), "?")
    allowed_chat_ids = _format_allowed_chat_ids(channel.get("allowed_chat_ids"))
    enabled_text = _bool_text(channel.get("enabled"))
    return (
        "- id="
        f"{channel_id}"
        f" platform={platform}"
        f" agent={agent_id}"
        f" dm_scope={dm_scope}"
        f" enabled={enabled_text}"
        f" allowed_chat_ids={allowed_chat_ids}"
        f" token_env_var={token_env_var}"
    )


def _format_allowed_chat_ids(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "-"
    rendered = [str(item) for item in value]
    return ",".join(rendered)


def _bool_text(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
