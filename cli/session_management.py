"""Session management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from cli._pagination import encode_cursor
from cli._recovery import format_command, target_options
from cli.archive_management import (
    permanent_delete_refusal,
    unfinished_permanent_delete,
)
from cli.formatting import inline_text, record_fields
from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

SESSION_TEXT_BUDGET = 10_000
_TITLE_PREVIEW_CHARS = 120


def session_list(
    instance: ServerInstance,
    agent_id: str,
    *,
    limit: int = 40,
    cursor: dict[str, object] | None = None,
    all_pages: bool = False,
    json_output: bool = False,
) -> CommandResult:
    """Read one bounded Session page, or all pages when explicitly requested."""
    sessions: list[object] = []
    seen = {encode_cursor(cursor)} if cursor is not None else set()
    total_count: int | None = None
    requested_limit = limit
    while True:
        params: dict[str, object] = {
            "agent_id": agent_id,
            "limit": limit,
            "include_subagents": True,
            "include_memory_reflections": True,
            "include_skill_reflections": True,
            "include_cron": True,
        }
        if cursor is not None:
            params["cursor"] = cursor
        payload = _rpc_call(instance, "session.list", params)
        if not payload.ok:
            return _session_list_failure(payload.to_command_result(), json_output)
        page = payload.data.get("sessions")
        if not isinstance(page, list) or any(
            not isinstance(session, dict)
            or not isinstance(session.get("id"), str)
            or not session["id"]
            for session in page
        ):
            return _session_list_failure(
                CommandResult(
                    ok=False, message="RPC result missing usable Session records", instance=instance
                ),
                json_output,
            )
        next_cursor = payload.data.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, dict):
            return _session_list_failure(
                CommandResult(
                    ok=False, message="RPC result has an invalid Session cursor", instance=instance
                ),
                json_output,
            )
        count = payload.data.get("total_count")
        total_count = (
            count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
        )
        key = encode_cursor(next_cursor) if next_cursor is not None else None
        if key is not None and key in seen:
            return _session_list_failure(
                CommandResult(
                    ok=False,
                    message="Session pagination did not advance; listing stopped",
                    instance=instance,
                ),
                json_output,
            )
        if not all_pages and not json_output:
            message = _format_session_rows(
                instance,
                agent_id,
                page,
                total_count,
                next_cursor,
                limit,
                reduced=limit != requested_limit,
            )
            if len(message) + 1 > SESSION_TEXT_BUDGET:
                if limit <= 1:
                    return _session_list_failure(
                        CommandResult(
                            ok=False,
                            message="A Session record and its continuation exceed "
                            "the text output limit. "
                            "Use --json for complete records.",
                            instance=instance,
                        ),
                        json_output,
                    )
                # Fetch a smaller page from the SAME cursor. Slicing this page
                # would skip unseen Sessions when its server cursor is reused.
                limit = max(1, limit // 2)
                continue
            return CommandResult(ok=True, message=message, instance=instance)
        sessions.extend(page)
        cursor = next_cursor
        if cursor is None or not all_pages:
            break
        assert key is not None
        seen.add(key)
    if json_output:
        message = json.dumps(
            {
                "ok": True,
                "agent_id": agent_id,
                "sessions": sessions,
                "total_count": total_count,
                "next_cursor": encode_cursor(cursor) if cursor is not None else None,
            },
            ensure_ascii=True,
        )
    else:
        message = _format_session_rows(instance, agent_id, sessions, total_count, cursor, limit)
    return CommandResult(ok=True, message=message, instance=instance)


def _session_list_failure(result: CommandResult, json_output: bool) -> CommandResult:
    if not json_output:
        return result
    error: dict[str, object] = {"message": result.message}
    if result.failure is not None:
        error.update(
            code=result.failure.code,
            request_state=result.failure.request_state,
            http_status=result.failure.http_status,
        )
    return replace(result, message=json.dumps({"ok": False, "error": error}, ensure_ascii=True))


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
    permanent: bool = False,
) -> CommandResult:
    """Archive a session via `session.delete` RPC, or delete it permanently.

    Without ``permanent`` the session moves into an archive entry that
    ``vbot archive restore`` brings back. Only permanent deletion requires
    explicit ``--yes`` confirmation.
    """

    if permanent and not confirm:
        message = permanent_delete_refusal(
            instance,
            f"session {session_id} for {agent_id}",
            ("vbot", "session", "delete", agent_id, session_id, "--permanent"),
        )
        return CommandResult(ok=False, message=message, instance=instance)

    params: dict[str, object] = {"agent_id": agent_id, "session_id": session_id}
    if permanent:
        params["permanent"] = True
    payload = _rpc_call(instance, "session.delete", params)
    if not payload.ok:
        return payload.to_command_result()
    next_session = f"next session: {_string_or_default(payload.data.get('next_session_id'), '?')}"
    entry_id = _string_or_default(payload.data.get("archive_entry_id"), "-")
    attention: tuple[str, ...] = ()
    ok = True
    if payload.data.get("purged") is True:
        message = f"deleted session {session_id} for {agent_id} permanently; {next_session}"
    elif permanent:
        clause, attention, ok = unfinished_permanent_delete(instance, payload.data, entry_id)
        message = (
            f"deleted session {session_id} for {agent_id} (archived as archive entry "
            f"{entry_id}), {clause}; {next_session}"
        )
    else:
        message = (
            f"deleted session {session_id} for {agent_id} (archived as archive entry "
            f"{entry_id}); {next_session}; restore with: "
            f"{format_command(('vbot', 'archive', 'restore', entry_id, *target_options(instance)))}"
        )
    return CommandResult(ok=ok, message=message, instance=instance, attention=attention)


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
    destination = target_agent_id or agent_id
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


def _format_session_rows(
    instance: ServerInstance,
    agent_id: str,
    sessions: Sequence[object],
    total_count: int | None,
    cursor: dict[str, object] | None,
    limit: int,
    *,
    reduced: bool = False,
) -> str:
    count = len(sessions)
    total = f" of {total_count}" if total_count is not None and total_count >= count else ""
    lines = [f"sessions for {inline_text(agent_id)}: showing {count}{total}, newest activity first"]
    if total_count is not None and total_count < count:
        lines.append(
            f"Note: the Session count changed while paging; the server now reports {total_count}."
        )
    if reduced:
        lines.append(f"Note: page size reduced to {limit} to fit the text output limit.")
    previews = any(
        isinstance(session, dict)
        and isinstance(session.get("title"), str)
        and len(inline_text(session["title"])) > _TITLE_PREVIEW_CHARS
        for session in sessions
    )
    if previews:
        lines.append(
            "Note: long titles are previews ending in ...; --json includes complete records."
        )
    for session in sessions:
        lines.append(_format_session_row(session))
    if cursor is not None:
        continuation = (
            "vbot",
            "session",
            "list",
            agent_id,
            "--limit",
            str(limit),
            "--cursor",
            encode_cursor(cursor),
            *target_options(instance),
        )
        lines.append(f"Next: {format_command(continuation)}")
    return "\n".join(lines)


def _format_session_row(session: object) -> str:
    if not isinstance(session, dict):
        return "- invalid session entry"

    session_id = _string_or_default(session.get("id"), "?")
    last_active_at = _string_or_default(session.get("last_active_at"), "-")
    fields = [f"- id={inline_text(session_id)}", f"last_active_at={inline_text(last_active_at)}"]
    activity = session.get("activity_state")
    if isinstance(activity, str) and activity:
        fields.append(f"activity={inline_text(activity)}")
    title = session.get("title")
    if isinstance(title, str) and title:
        title = inline_text(title)
        if len(title) > _TITLE_PREVIEW_CHARS:
            title = title[:_TITLE_PREVIEW_CHARS] + "..."
        fields.append(f"title={json.dumps(title, ensure_ascii=False)}")
    source_channel_id = session.get("source_channel_id")
    if isinstance(source_channel_id, str) and source_channel_id:
        fields.append(f"channel={inline_text(source_channel_id)}")
    if "compaction_policy_override" in session:
        source = (
            "override" if session.get("compaction_policy_override") is not None else "inherited"
        )
        fields.append(f"compaction={source}")
    return record_fields(fields)


def _json_text(value: object) -> str:
    if value is None:
        return "-"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
