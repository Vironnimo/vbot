"""Archive commands: list, show, restore and permanently delete archive entries.

Deleting an Agent, Project or Session moves it into one archive entry; these
commands read entries, restore one, or purge entries for good. The delete
commands of the other areas use the shared outcome texts at the end.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cli._recovery import format_command
from cli.formatting import record_fields
from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import RpcPayload
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

DEFAULT_LIST_LIMIT = 50
_NEW_ID_PLACEHOLDERS = {
    "agent": "<new-agent-id>",
    "project": "<new-project-id>",
    "session": "<new-session-id>",
}
_CONFLICT_CODES = frozenset({"agent_id_taken", "project_id_taken", "session_address_taken"})


def archive_list(
    instance: ServerInstance,
    *,
    kind: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: dict[str, object] | None = None,
    all_pages: bool = False,
) -> CommandResult:
    """List one page of archive entries, newest first, or every page when asked."""
    filters = _filter_params(kind, agent_id, project_id)
    entries: list[object] = []
    seen: set[str] = set()
    retention_days: object = None
    retention_unknown = False
    while True:
        params: dict[str, object] = {**filters, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        payload = _rpc_call(instance, "archive.list", params)
        if not payload.ok:
            return payload.to_command_result()
        page = payload.data.get("entries")
        if not isinstance(page, list):
            return CommandResult(
                ok=False, message="RPC result missing archive entries list", instance=instance
            )
        entries.extend(page)
        retention_days = payload.data.get("retention_days")
        retention_unknown = payload.data.get("retention_unknown") is True
        next_cursor = payload.data.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, dict):
            return CommandResult(
                ok=False, message="RPC result has an invalid archive cursor", instance=instance
            )
        cursor = next_cursor
        if cursor is None or not all_pages:
            break
        key = json.dumps(cursor, sort_keys=True)
        if key in seen:
            return CommandResult(
                ok=False,
                message="archive pagination did not advance; listing stopped",
                instance=instance,
            )
        seen.add(key)
    lines = _format_entries(
        entries, retention_days, filtered=bool(filters), retention_unknown=retention_unknown
    )
    if cursor is not None:
        command = (
            "vbot",
            "archive",
            "list",
            *_filter_options(kind, agent_id, project_id),
            *(("--limit", str(limit)) if limit != DEFAULT_LIST_LIMIT else ()),
            "--cursor",
            json.dumps(cursor, ensure_ascii=False, separators=(",", ":")),
        )
        lines.append(f"next page: {format_command(command)}")
    attention = (
        (
            "vBot cannot read the archive.retention_days setting, so it deletes no archive "
            "entry automatically until the setting can be read; 'vbot doctor settings' shows "
            "the problem in settings.json",
        )
        if retention_unknown
        else ()
    )
    return CommandResult(ok=True, message="\n".join(lines), instance=instance, attention=attention)


def archive_show(instance: ServerInstance, entry_id: str) -> CommandResult:
    """Show one entry's facts, Sessions, files and whether it can be restored."""
    payload = _rpc_call(instance, "archive.show", {"entry_id": entry_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=_format_detail(payload.data), instance=instance)


def archive_restore(
    instance: ServerInstance, entry_id: str, target_id: str | None = None
) -> CommandResult:
    """Restore an entry, under ``target_id`` when its original id is in use."""
    params: dict[str, object] = {"entry_id": entry_id}
    if target_id is not None:
        params["target_id"] = target_id
    payload = _rpc_call(instance, "archive.restore", params)
    if not payload.ok:
        return _restore_failure(payload, entry_id)
    data = payload.data
    kind = _string_or_default(data.get("kind"), "?")
    subject = _string_or_default(data.get("subject_id"), "?")
    restored = _mapping(data.get("restored"))
    notes: list[str] = []
    if kind == "session":
        session_id = _string_or_default(restored.get("session_id"), subject)
        renamed = f" as {session_id}" if session_id != subject else ""
        owner = _agent_address(restored.get("agent_id"), restored.get("project_id"))
        what = f"session {subject}{renamed} for {owner}"
    else:
        new_id = _string_or_default(restored.get(f"{kind}_id"), subject)
        what = f"{kind} {subject}" + (f" as {new_id}" if new_id != subject else "")
        notes.append(sessions_text(data.get("session_count")))
    grants = _strings(data.get("grant_agent_ids"))
    if grants:
        notes.append(f"delegation grants: {', '.join(grants)}")
    first = f"restored {what} from archive entry {entry_id}"
    lines = [f"{first} ({'; '.join(notes)})" if notes else first]
    warnings = [
        warning["message"]
        for warning in data.get("warnings") or ()
        if isinstance(warning, dict) and isinstance(warning.get("message"), str)
    ]
    lines.extend(f"warning: {warning}" for warning in warnings)
    return CommandResult(
        ok=True, message="\n".join(lines), instance=instance, attention=tuple(warnings)
    )


def archive_purge(
    instance: ServerInstance,
    entry_ids: Sequence[str],
    *,
    all_matching: bool = False,
    kind: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    confirm: bool = False,
) -> CommandResult:
    """Delete the named entries, or with ``all_matching`` every matching one, permanently."""
    selection = (
        ("--all", *_filter_options(kind, agent_id, project_id))
        if all_matching
        else tuple(entry_ids)
    )
    if not confirm:
        if all_matching:
            listing = format_command(
                ("vbot", "archive", "list", *_filter_options(kind, agent_id, project_id))
            )
            what = f"every {'matching ' if len(selection) > 1 else ''}archive entry"
            check = f"; '{listing}' shows them"
        else:
            what = (
                f"archive entry {entry_ids[0]}"
                if len(entry_ids) == 1
                else f"{len(entry_ids)} archive entries"
            )
            check = ""
        command = format_command(("vbot", "archive", "purge", *selection, "--yes"))
        return CommandResult(
            ok=False,
            message=(
                f"refusing to delete {what} permanently without confirmation; "
                f"re-run with --yes: {command}{check}"
            ),
            instance=instance,
        )
    params: dict[str, object] = (
        {"all": True, **_filter_params(kind, agent_id, project_id)}
        if all_matching
        else {"entry_ids": list(entry_ids)}
    )
    payload = _rpc_call(instance, "archive.purge", params)
    if not payload.ok:
        return payload.to_command_result()
    purged = _records(payload.data.get("purged"))
    pending = _records(payload.data.get("pending"))
    skipped = _records(payload.data.get("skipped"))
    gone = _strings(payload.data.get("gone"))
    kept = _records(payload.data.get("kept"))
    session_total = sum(
        count for entry in purged if isinstance(count := entry.get("session_count"), int)
    )
    if purged:
        noun = "archive entry" if len(purged) == 1 else "archive entries"
        first = f"purged {len(purged)} {noun} ({sessions_text(session_total)})"
    elif pending or skipped or gone or kept:
        first = "purged no archive entries"
    else:
        first = "no archive entries matched; nothing was purged"
    lines = [first]
    lines.extend(
        record_fields(
            [
                f"- id={entry.get('entry_id')}",
                f"kind={entry.get('kind')}",
                f"subject={entry.get('subject_id')}",
                f"sessions={entry.get('session_count')}",
            ]
        )
        for entry in purged
    )
    lines.extend(
        f"- id={entry_id} gone (no longer in the archive: another operation deleted or restored it)"
        for entry_id in gone
    )
    lines.extend(
        record_fields([f"- id={entry.get('entry_id')}", "pending", f"reason={entry.get('reason')}"])
        for entry in pending
    )
    lines.extend(
        record_fields(
            [
                f"- id={entry.get('entry_id')}",
                "skipped",
                f"reason={entry.get('reason')}",
                *((f"state={entry['state']}",) if entry.get("state") else ()),
            ]
        )
        for entry in skipped
    )
    lines.extend(
        record_fields([f"- id={entry.get('entry_id')}", "kept", f"reason={entry.get('reason')}"])
        for entry in kept
    )
    attention: list[str] = []
    if pending:
        pending_ids = [str(entry.get("entry_id")) for entry in pending]
        if len(pending) == 1:
            attention.append(pending_purge_attention(pending_ids[0], str(pending[0].get("reason"))))
        else:
            retry = selection if all_matching else tuple(pending_ids)
            command = format_command(("vbot", "archive", "purge", *retry, "--yes"))
            attention.append(
                f"deletion of {len(pending)} archive entries is pending; vBot retries them "
                f"automatically, or run '{command}' to continue them now"
            )
    attention.extend(
        skipped_purge_attention(
            str(entry.get("entry_id")), str(entry.get("reason")), entry.get("state")
        )
        for entry in skipped
    )
    if kept:
        kept_ids = [str(entry.get("entry_id")) for entry in kept]
        command = format_command(("vbot", "archive", "purge", *kept_ids, "--yes"))
        what = (
            f"archive entry {kept_ids[0]}, which may hold"
            if len(kept_ids) == 1
            else f"{len(kept_ids)} archive entries that may hold"
        )
        attention.append(
            f"kept {what} the user's own folders: purge --all never deletes such entries; "
            f"to delete {'it' if len(kept_ids) == 1 else 'them'} as well, run '{command}'"
        )
    return CommandResult(
        ok=not (pending or skipped),
        message="\n".join(lines),
        instance=instance,
        attention=tuple(attention),
    )


# -- Shared delete outcome texts ----------------------------------------------------


def sessions_text(count: object) -> str:
    """``1 session`` or ``12 sessions``; ``? sessions`` when the count is unknown."""
    if not isinstance(count, int) or isinstance(count, bool):
        return "? sessions"
    return f"{count} session" if count == 1 else f"{count} sessions"


def pending_purge_attention(entry_id: str, reason: str | None = None) -> str:
    """The attention line for an entry whose permanent deletion began and did not finish."""
    detail = f" ({reason})" if reason else ""
    return (
        f"deletion of {entry_id} is pending{detail}; vBot retries it automatically, "
        f"or run 'vbot archive purge {entry_id} --yes' to continue it now"
    )


def skipped_purge_attention(entry_id: str, reason: str, state: object = None) -> str:
    """The attention line for an entry a purge left unchanged; nothing retries it."""
    if reason == "busy":
        held = f" ({state})" if isinstance(state, str) and state else ""
        return (
            f"{entry_id} was not deleted: another operation, such as a restore, holds "
            f"it{held}; 'vbot archive show {entry_id}' shows its state"
        )
    return (
        f"{entry_id} was not deleted ({reason}) and stays in the archive unchanged; run "
        f"'vbot archive purge {entry_id} --yes' to try again"
    )


def unfinished_permanent_delete(
    data: Mapping[str, Any], entry_id: str
) -> tuple[str, tuple[str, ...], bool]:
    """How a permanent delete ends whose purge did not delete the entry.

    Returns the clause after the archived form, the attention lines and whether
    the command succeeded.
    """
    reason = data.get("purge_reason")
    if data.get("purge_pending") is True:
        detail = reason if isinstance(reason, str) else None
        return (
            "but deleting it permanently did not finish",
            (pending_purge_attention(entry_id, detail),),
            False,
        )
    if reason == "gone":
        return (
            "but another operation deleted or restored the entry before vBot could delete "
            "it permanently",
            (
                f"archive entry {entry_id} is no longer in the archive; if another operation "
                "restored it, it is live again",
            ),
            True,
        )
    return (
        "but it was not deleted permanently",
        (skipped_purge_attention(entry_id, str(reason)),),
        False,
    )


def permanent_delete_refusal(what: str, command: Sequence[str]) -> str:
    """Refuse a permanent deletion without ``--yes``, naming the confirmed command."""
    return (
        f"refusing to delete {what} permanently without confirmation; re-run with --yes: "
        f"{format_command((*command, '--yes'))} (without --permanent it moves to the archive)"
    )


# -- Formatting ---------------------------------------------------------------------


def _filter_params(
    kind: str | None, agent_id: str | None, project_id: str | None
) -> dict[str, object]:
    values = {"kind": kind, "agent_id": agent_id, "project_id": project_id}
    return {key: value for key, value in values.items() if value is not None}


def _filter_options(
    kind: str | None, agent_id: str | None, project_id: str | None
) -> tuple[str, ...]:
    options: list[str] = []
    for flag, value in (("--kind", kind), ("--agent", agent_id), ("--project", project_id)):
        if value is not None:
            options.extend((flag, value))
    return tuple(options)


def _format_entries(
    entries: Sequence[object],
    retention_days: object,
    *,
    filtered: bool,
    retention_unknown: bool = False,
) -> list[str]:
    if not entries:
        return ["no matching archive entries" if filtered else "no archive entries"]
    if retention_unknown:
        header = "archive entries (automatic deletion paused: the retention period cannot be read):"
    elif isinstance(retention_days, int) and not isinstance(retention_days, bool):
        unit = "day" if retention_days == 1 else "days"
        header = f"archive entries (automatic deletion after {retention_days} {unit}):"
    else:
        header = "archive entries (automatic deletion off):"
    return [header, *(_format_entry_row(entry) for entry in entries)]


def _format_entry_row(entry: object) -> str:
    if not isinstance(entry, dict):
        return "- invalid archive entry"
    kind = entry.get("kind")
    fields = [
        f"- id={entry.get('entry_id')}",
        f"kind={kind}",
        f"subject={entry.get('subject_id')}",
    ]
    if entry.get("agent_id") and kind != "agent":
        fields.append(f"agent={entry['agent_id']}")
    if entry.get("project_id") and kind != "project":
        fields.append(f"project={entry['project_id']}")
    if entry.get("owner_name"):
        fields.append(f"owner={entry['owner_name']}")
    fields.append(f"label={json.dumps(entry.get('label'), ensure_ascii=False)}")
    fields.append(f"sessions={entry.get('session_count')}")
    if entry.get("state") != "archived":
        fields.append(f"state={entry.get('state')}")
    fields.append(f"archived_at={entry.get('archived_at')}")
    if entry.get("origin") == "recovered":
        fields.append("origin=recovered")
    fields.append(f"purge_at={entry.get('purge_at') or '-'}")
    if entry.get("may_hold_user_folders") is True:
        fields.append("user_folders=yes")
    fields.append(f"restorable={'yes' if entry.get('restorable') is True else 'no'}")
    if entry.get("not_restorable_reason"):
        fields.append(f"reason={entry['not_restorable_reason']}")
    return record_fields(fields)


def _format_detail(data: Mapping[str, Any]) -> str:
    entry = _mapping(data.get("entry"))
    entry_id = entry.get("entry_id")
    kind = entry.get("kind")
    subject = entry.get("subject_id")
    label = entry.get("label")
    named = f" ({json.dumps(label, ensure_ascii=False)})" if label and label != subject else ""
    lines = [f"archive entry {entry_id}", f"kind: {kind}", f"subject: {subject}{named}"]
    if entry.get("agent_id") and kind != "agent":
        lines.append(f"agent: {entry['agent_id']}")
    if entry.get("project_id") and kind != "project":
        lines.append(f"project: {entry['project_id']}")
    if entry.get("owner_name"):
        lines.append(f"extension: {entry['owner_name']}")
    lines.extend(
        [
            f"state: {entry.get('state')}",
            f"archived_at: {entry.get('archived_at')}",
            f"purge_at: {entry.get('purge_at') or '-'}",
        ]
    )
    if entry.get("origin") == "recovered":
        lines.append(
            "origin: recovered (vBot found its files without a record of when they were "
            "archived; only a purge that names it deletes it)"
        )
    sessions = [session for session in data.get("sessions") or () if isinstance(session, dict)]
    lines.append(f"sessions: {data.get('session_count')} (showing {len(sessions)})")
    for session in sessions:
        fields = [f"- id={session.get('session_id')}"]
        if kind not in {"agent", "session"}:
            fields.append(
                f"agent={_agent_address(session.get('agent_id'), session.get('project_id'))}"
            )
        if session.get("title"):
            fields.append(f"title={json.dumps(session['title'], ensure_ascii=False)}")
        fields.append(f"last_active_at={session.get('last_activity_at') or '-'}")
        lines.append(record_fields(fields))
    files = _mapping(data.get("files"))
    lines.append(f"files: {files.get('state')}")
    for tree in files.get("trees") or ():
        if not isinstance(tree, dict):
            continue
        source = f" (from {tree['source_path']})" if tree.get("source_path") else ""
        user_folder = (
            ""
            if not tree.get("user_folder")
            else " (may be your own folder; a restore brings it back, a purge deletes it)"
            if tree.get("role") == "workspace"
            else " (may be your own folder; a purge deletes it)"
        )
        lines.append(f"- {tree.get('role')}: {tree.get('path')}{source}{user_folder}")
    lines.extend(_detail_lines(data.get("details")))
    lines.extend(_restore_lines(data.get("restore"), kind))
    return "\n".join(lines)


def _detail_lines(details: object) -> list[str]:
    if not isinstance(details, dict):
        return []
    lines: list[str] = []
    if details.get("cwd"):
        lines.append(f"repo: {details['cwd']}")
    if details.get("root_project_id"):
        lines.append(f"default project: {details['root_project_id']}")
    workspace = details.get("workspace")
    if isinstance(workspace, dict) and workspace.get("external") and workspace.get("path"):
        lines.append(
            f"workspace: {workspace['path']} (outside the Agent's own directory, left in place)"
        )
    grants = _strings(details.get("grants"))
    if grants:
        lines.append(f"delegation grants: {', '.join(grants)}")
    unrooted = _strings(details.get("unrooted_agents"))
    if unrooted:
        lines.append(
            f"agents with this default project: {', '.join(unrooted)} "
            "(their default project again on restore)"
        )
    if details.get("reason"):
        lines.append(f"reason: {details['reason']}")
    return lines


def _restore_lines(restore: object, kind: object) -> list[str]:
    if not isinstance(restore, dict):
        return []
    blockers = [blocker for blocker in restore.get("blockers") or () if isinstance(blocker, dict)]
    warnings = [warning for warning in restore.get("warnings") or () if isinstance(warning, dict)]
    lines = [f"restore: {'possible' if restore.get('possible') is True else 'blocked'}"]
    lines.extend(_blocker_line(blocker, kind) for blocker in blockers)
    lines.extend(
        f"- warning {warning.get('code')}: {warning.get('message')}" for warning in warnings
    )
    return lines


def _blocker_line(blocker: Mapping[str, Any], kind: object) -> str:
    code = blocker.get("code")
    hint = ""
    if code in _CONFLICT_CODES:
        placeholder = _NEW_ID_PLACEHOLDERS.get(str(kind), "<new-id>")
        hint = f"; restore it under a new id with --as {placeholder}"
    elif code == "scope_missing" and isinstance(blocker.get("entry_id"), str):
        hint = f" (vbot archive restore {blocker['entry_id']})"
    return f"- {code}: {blocker.get('message')}{hint}"


def _restore_failure(payload: RpcPayload, entry_id: str) -> CommandResult:
    """Name the restore's blockers, and the corrected call for a taken id."""
    code = payload.failure.code if payload.failure else None
    data = payload.error_data
    if code == "archive_restore_conflict" and isinstance(data.get("conflicts"), list):
        reasons = "; ".join(
            str(conflict.get("message"))
            for conflict in data["conflicts"]
            if isinstance(conflict, dict)
        )
        placeholder = _NEW_ID_PLACEHOLDERS.get(str(data.get("kind")), "<new-id>")
        message = (
            f"{code}: cannot restore archive entry {entry_id}: {reasons}; restore it under "
            f"a new id: vbot archive restore {entry_id} --as {placeholder}"
        )
    elif code == "archive_not_restorable" and isinstance(data.get("blockers"), list):
        message = "\n".join(
            [
                f"{code}: cannot restore archive entry {entry_id}:",
                *(
                    _blocker_line(blocker, None)
                    for blocker in data["blockers"]
                    if isinstance(blocker, dict)
                ),
            ]
        )
    else:
        return payload.to_command_result()
    return CommandResult(
        ok=False, message=message, instance=payload.instance, failure=payload.failure
    )


def _agent_address(agent_id: object, project_id: object) -> str:
    agent = _string_or_default(agent_id, "?")
    return f"{agent}@{project_id}" if isinstance(project_id, str) and project_id else agent


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
