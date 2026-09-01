"""Session management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Sequence

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def session_list(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Return formatted session list output from `session.list` RPC."""

    sessions: list[object] = []
    cursor: object = None
    while True:
        params: dict[str, object] = {
            "agent_id": agent_id,
            "limit": 100,
            "include_subagents": True,
            "include_memory_reflections": True,
            "include_skill_reflections": True,
            "include_cron": True,
        }
        if cursor is not None:
            params["cursor"] = cursor
        payload = _rpc_call(instance, "session.list", params)
        if not payload.ok:
            return payload.to_command_result()
        page = payload.data.get("sessions")
        if not isinstance(page, list):
            return CommandResult(
                ok=False, message="RPC result missing sessions list", instance=instance
            )
        sessions.extend(page)
        cursor = payload.data.get("next_cursor")
        if cursor is None:
            break
        if not isinstance(cursor, dict):
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


def session_delete(
    instance: ServerInstance,
    agent_id: str,
    session_id: str,
    confirm: bool,
) -> CommandResult:
    """Delete (archive) a session via `session.delete` RPC.

    A typed command is already deliberate, but the destructive call still
    requires an explicit ``--yes`` so a stray invocation cannot drop a
    conversation. Without it the command refuses and explains how to proceed.
    The session is archived (recoverable by hand), not erased.
    """

    if not confirm:
        return CommandResult(
            ok=False,
            message=(
                f"refusing to delete session {session_id} for {agent_id} without confirmation; "
                "re-run with --yes (the session is archived, not erased)"
            ),
            instance=instance,
        )

    params = {"agent_id": agent_id, "session_id": session_id}
    payload = _rpc_call(instance, "session.delete", params)
    if not payload.ok:
        return payload.to_command_result()
    next_session_id = _string_or_default(payload.data.get("next_session_id"), "?")
    return CommandResult(
        ok=True,
        message=(
            f"deleted session {session_id} for {agent_id} (archived, recoverable); "
            f"next session: {next_session_id}"
        ),
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


def session_fork(
    instance: ServerInstance,
    agent_id: str,
    session_id: str,
    target_agent_id: str | None,
) -> CommandResult:
    """Fork a session via ``session.fork`` RPC."""

    params: dict[str, object] = {"agent_id": agent_id, "session_id": session_id}
    if target_agent_id is not None:
        params["target_agent_id"] = target_agent_id
    payload = _rpc_call(instance, "session.fork", params)
    if not payload.ok:
        return payload.to_command_result()
    session = payload.data.get("session")
    if not isinstance(session, dict):
        return CommandResult(
            ok=False, message="RPC result missing forked session", instance=instance
        )
    fork_id = _string_or_default(session.get("id"), "?")
    destination = _string_or_default(session.get("agent_id"), target_agent_id or agent_id)
    source = _json_text(session.get("fork_source"))
    return CommandResult(
        ok=True,
        message=(
            f"forked session {session_id} to {fork_id} for {destination}\nfork_source: {source}"
        ),
        instance=instance,
    )


def session_rename(
    instance: ServerInstance,
    agent_id: str,
    session_id: str,
    title: str,
) -> CommandResult:
    """Set or clear a session title via ``session.rename`` RPC."""

    payload = _rpc_call(
        instance,
        "session.rename",
        {"agent_id": agent_id, "session_id": session_id, "title": title},
    )
    if not payload.ok:
        return payload.to_command_result()
    stored_title = payload.data.get("title")
    title_text = stored_title if isinstance(stored_title, str) and stored_title else "(automatic)"
    resolved_agent = _string_or_default(payload.data.get("agent_id"), agent_id)
    return CommandResult(
        ok=True,
        message=f"renamed session {session_id} for {resolved_agent}\ntitle: {title_text}",
        instance=instance,
    )


def session_set_compaction_policy(
    instance: ServerInstance,
    agent_id: str,
    session_id: str,
    policy: dict[str, object] | None,
) -> CommandResult:
    """Set or clear a Session Policy override and show effective provenance."""

    payload = _rpc_call(
        instance,
        "session.set_compaction_policy",
        {"agent_id": agent_id, "session_id": session_id, "policy": policy},
    )
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=(
            f"updated Session Policy for {session_id} ({agent_id})\n"
            f"override: {_json_text(payload.data.get('override'))}\n"
            f"effective: {_json_text(payload.data.get('effective'))}\n"
            f"source: {_string_or_default(payload.data.get('source'), '?')}"
        ),
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
    title = session.get("title")
    if isinstance(title, str) and title:
        line = f"{line} title={json.dumps(title, ensure_ascii=False)}"
    source_channel_id = session.get("source_channel_id")
    if isinstance(source_channel_id, str) and source_channel_id:
        line = f"{line} channel={source_channel_id}"
    if "compaction_policy_override" in session:
        line = f"{line} compaction_override={_json_text(session.get('compaction_policy_override'))}"
    if "compaction_policy_effective" in session:
        line = (
            f"{line} compaction_effective={_json_text(session.get('compaction_policy_effective'))}"
        )
    return line


def _json_text(value: object) -> str:
    if value is None:
        return "-"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
