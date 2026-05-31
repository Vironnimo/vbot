"""Channel management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

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
