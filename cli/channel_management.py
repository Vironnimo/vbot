"""Channel management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.formatting import bool_text as _bool_text
from cli.formatting import string_or_default as _string_or_default
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
    "--response-mode",
    "--mention-pattern",
    "--owner-user",
    "--observe-unaddressed",
)


def channel_add(
    instance: ServerInstance,
    channel_id: str,
    platform: str,
    agent_id: str,
    token_env: str,
    dm_scope: str,
    allowed_chat_ids: Sequence[str],
    response_mode: str = "mention",
    mention_patterns: Sequence[str] = (),
    owner_user_ids: Sequence[str] = (),
    observe_unaddressed: bool = False,
) -> CommandResult:
    """Create a channel configuration via `channel.create` RPC."""

    params: dict[str, object] = {
        "id": channel_id,
        "platform": platform,
        "agent_id": agent_id,
        "token_env_var": token_env,
        "dm_scope": dm_scope,
        "allowed_chat_ids": list(allowed_chat_ids),
    }
    if response_mode != "mention":
        params["response_mode"] = response_mode
    if mention_patterns:
        params["mention_patterns"] = list(mention_patterns)
    if owner_user_ids:
        params["owner_user_ids"] = list(owner_user_ids)
    if observe_unaddressed:
        params["observe_unaddressed"] = True
    payload = _rpc_call(instance, "channel.create", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_channel_operation("created", payload.data, channel_id),
        instance=instance,
    )


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
    return CommandResult(
        ok=True,
        message=_format_channel_operation("updated", payload.data, channel_id),
        instance=instance,
    )


def channel_enable(instance: ServerInstance, channel_id: str) -> CommandResult:
    """Enable a channel listener via `channel.enable` RPC."""

    payload = _rpc_call(instance, "channel.enable", {"id": channel_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_channel_operation("enabled", payload.data, channel_id),
        instance=instance,
    )


def channel_disable(instance: ServerInstance, channel_id: str) -> CommandResult:
    """Disable a channel listener via `channel.disable` RPC."""

    payload = _rpc_call(instance, "channel.disable", {"id": channel_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_channel_operation("disabled", payload.data, channel_id),
        instance=instance,
    )


def channel_status(instance: ServerInstance, channel_id: str) -> CommandResult:
    """Fetch channel runtime status via `channel.status` RPC."""

    payload = _rpc_call(instance, "channel.status", {"id": channel_id})
    if not payload.ok:
        return payload.to_command_result()
    resolved_id = _string_or_default(payload.data.get("id"), channel_id)
    enabled_text = _bool_text(payload.data.get("enabled"))
    running_text = _bool_text(payload.data.get("running"))
    failed_text = _bool_text(payload.data.get("failed"))
    failure_reason = payload.data.get("failure_reason")
    failure_suffix = (
        f" failure_reason={failure_reason}"
        if isinstance(failure_reason, str) and failure_reason
        else ""
    )
    lines = [
        f"{resolved_id}: enabled={enabled_text} running={running_text} "
        f"failed={failed_text}{failure_suffix}"
    ]
    lines.extend(_format_denied_chats(resolved_id, payload.data.get("denied_chats")))
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _format_denied_chats(channel_id: str, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return []

    lines = ["denied inbound chats (messaged the bot but are not in the allowlist):"]
    for entry in value:
        if not isinstance(entry, dict):
            continue
        chat_id = _string_or_default(entry.get("chat_id"), "?")
        kind = _string_or_default(entry.get("kind"), "?")
        display_name = entry.get("display_name")
        name_part = (
            f" name={display_name}" if isinstance(display_name, str) and display_name else ""
        )
        last_seen = _string_or_default(entry.get("last_seen_at"), "?")
        count = entry.get("count")
        count_part = f" messages={count}" if isinstance(count, int) else ""
        lines.append(
            f"- chat_id={chat_id} kind={kind}{name_part} last_seen={last_seen}{count_part}"
        )
    lines.append(
        f"to allow a chat: vbot channel update {channel_id} --allow <all allowed chat ids> "
        "(--allow replaces the full list; include existing ids)"
    )
    return lines


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
    line = (
        "- id="
        f"{channel_id}"
        f" platform={platform}"
        f" agent={agent_id}"
        f" dm_scope={dm_scope}"
        f" enabled={enabled_text}"
        f" allowed_chat_ids={allowed_chat_ids}"
        f" token_env_var={token_env_var}"
    )
    if "response_mode" in channel:
        line = f"{line} response_mode={_string_or_default(channel.get('response_mode'), 'mention')}"
    if "mention_patterns" in channel:
        line = (
            f"{line} mention_patterns={_format_allowed_chat_ids(channel.get('mention_patterns'))}"
        )
    if "owner_user_ids" in channel:
        line = f"{line} owner_user_ids={_format_allowed_chat_ids(channel.get('owner_user_ids'))}"
    if "observe_unaddressed" in channel:
        line = f"{line} observe_unaddressed={_bool_text(channel.get('observe_unaddressed'))}"
    return line


def _format_allowed_chat_ids(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "-"
    rendered = [str(item) for item in value]
    return ",".join(rendered)


def _format_channel_operation(action: str, channel: Mapping[str, Any], fallback_id: str) -> str:
    channel_id = _string_or_default(channel.get("id"), fallback_id)
    if set(channel) <= {"id", "ok"}:
        return f"{action} {channel_id}"
    return f"{action} channel {channel_id}\n{_format_channel_row(channel)}"
