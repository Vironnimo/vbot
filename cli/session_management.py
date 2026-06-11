"""Session management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Sequence

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def session_list(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Return formatted session list output from `session.list` RPC."""

    payload = _rpc_call(instance, "session.list", {"agent_id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    sessions = payload.data.get("sessions")
    if not isinstance(sessions, list):
        return CommandResult(
            ok=False, message="RPC result missing sessions list", instance=instance
        )
    return CommandResult(
        ok=True, message=_format_session_rows(agent_id, sessions), instance=instance
    )


def session_create(
    instance: ServerInstance,
    agent_id: str,
    session_id: str | None,
    make_current: bool,
) -> CommandResult:
    """Create a session via `session.create` RPC."""

    params: dict[str, object] = {"agent_id": agent_id}
    if session_id is not None:
        params["session_id"] = session_id
    if make_current:
        params["make_current"] = True

    payload = _rpc_call(instance, "session.create", params)
    if not payload.ok:
        return payload.to_command_result()
    created_id = _string_or_default(payload.data.get("session_id"), "?")
    current_suffix = " (now current)" if make_current else ""
    return CommandResult(
        ok=True,
        message=f"created session {created_id} for {agent_id}{current_suffix}",
        instance=instance,
    )


def session_link_channel(
    instance: ServerInstance,
    agent_id: str,
    session_id: str,
    channel_id: str,
    platform_conv_id: str,
) -> CommandResult:
    """Link a session to a channel conversation via `session.link_channel` RPC."""

    params = {
        "agent_id": agent_id,
        "session_id": session_id,
        "channel_id": channel_id,
        "platform_conv_id": platform_conv_id,
    }
    payload = _rpc_call(instance, "session.link_channel", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=f"linked session {session_id} to channel {channel_id} ({platform_conv_id})",
        instance=instance,
    )


def _format_session_rows(agent_id: str, sessions: Sequence[object]) -> str:
    if not sessions:
        return f"no sessions for {agent_id}"

    lines = [f"sessions for {agent_id}:"]
    for session in sessions:
        lines.append(_format_session_row(session))
    return "\n".join(lines)


def _format_session_row(session: object) -> str:
    if not isinstance(session, dict):
        return "- invalid session entry"

    session_id = _string_or_default(session.get("id"), "?")
    created_at = _string_or_default(session.get("created_at"), "-")
    last_active_at = _string_or_default(session.get("last_active_at"), "-")
    line = f"- id={session_id} created_at={created_at} last_active_at={last_active_at}"
    source_channel_id = session.get("source_channel_id")
    if isinstance(source_channel_id, str) and source_channel_id:
        line = f"{line} channel={source_channel_id}"
    return line


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
