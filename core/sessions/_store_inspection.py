"""Bounded transcript and compact activity reads over the canonical current view.

SQL bounds text before materialization. Tool outcomes come from recorded results,
not an interpretation of their arguments. No Model or disposable index is involved.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_lineage, _store_values
from core.sessions._types import SessionAddress

type JsonObject = dict[str, Any]

_TEXT = (
    "(SELECT COALESCE(t.content, t.search_text, '') "
    "FROM entry_text t WHERE t.entry_key=e.entry_key)"
)
_RUN = "(SELECT r.run_id FROM runs r WHERE r.run_key=e.run_key)"
_ROLES = "e.role IN ('user', 'assistant', 'tool', 'error', 'run_summary')"


def read(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    summary: bool,
    limit: int,
    cursor: JsonObject | None,
    run_id: str | None,
    entry: int | None,
    offset: int,
) -> JsonObject:
    if not 1 <= limit <= 100 or offset < 0 or (entry is not None and entry < 0):
        raise ChatSessionError("Invalid inspection bounds")
    if entry is not None and (summary or cursor is not None or run_id is not None):
        raise ChatSessionError("An exact entry cannot be combined with summary, cursor or Run")
    if offset and entry is None:
        raise ChatSessionError("An offset requires an exact entry")
    state = _store_values._require_live(connection, address)
    generation = str(state["generation_id"])
    revision = int(state["history_revision"])
    before = int(state["next_seq"])
    if cursor is not None:
        if (
            cursor.get("generation") != generation
            or cursor.get("summary") is not summary
            or cursor.get("run_id") != run_id
            or cursor.get("floor_sequence") != int(state["cursor_floor_seq"])
            or not isinstance(cursor.get("revision"), int)
            or not int(state["cursor_floor_revision"]) <= cursor["revision"] <= revision
            or not isinstance(cursor.get("before"), int)
            or not 0 <= cursor["before"] <= before
        ):
            raise ChatSessionError(
                "The Session history or selection changed; read it again without --cursor"
            )
        before = cursor["before"]
    ranges = _store_lineage.view_ranges(connection, int(state["session_key"]))
    visible_result = _store_lineage.view_query(
        ranges, "1", where="e.entry_key = c.result_entry_key"
    )
    where, params = _ROLES, []
    if summary:
        where += (
            f" AND (e.role <> 'assistant' OR length({_TEXT}) > 0 OR EXISTS "
            "(SELECT 1 FROM tool_calls c WHERE c.entry_key=e.entry_key "
            f"AND NOT EXISTS ({visible_result[0]})))"
        )
        params.extend(visible_result[1])
    if run_id is not None:
        where += f" AND {_RUN} = ?"
        params.append(run_id)
        if not _store_lineage.ordered_rows(
            connection, ranges, columns="e.seq", where=where, params=params, limit=1
        ):
            raise ChatSessionError(f"No visible entries for Run {run_id} in this Session")
    width = 600 if summary else (3200 if entry is not None else 1400)
    columns = (
        f"e.entry_key, e.session_key, e.seq, e.role, e.created_at, {_RUN} AS run_id, "
        f"substr({_TEXT}, {offset + 1}, {width + 1}) AS text, "
        "(SELECT r.status FROM runs r WHERE r.end_entry_key=e.entry_key) AS run_status"
    )
    rows = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=columns,
        where=where,
        params=params,
        lower=entry if entry is not None else 0,
        upper=entry + 1 if entry is not None else before,
        descending=entry is None,
        limit=limit + 1,
    )
    if entry is not None and not rows:
        raise ChatSessionError(f"No visible entry at sequence {entry} in this Session")
    more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    entries = [
        _entry(
            connection,
            row,
            summary=summary,
            width=width,
            offset=offset,
            visible_result=visible_result,
            inherited=(
                int(row["session_key"]) != int(state["session_key"])
                or int(row["seq"]) < int(state["fork_point_seq"] or 0)
            ),
        )
        for row in rows
    ]
    result: JsonObject = {
        "generation_id": generation,
        "summary": summary,
        "run_id": run_id,
        "entries": entries,
        "next_cursor": None,
    }
    if more:
        result["next_cursor"] = {
            "generation": generation,
            "revision": revision,
            "summary": summary,
            "run_id": run_id,
            "before": int(rows[0]["seq"]),
            "floor_sequence": int(state["cursor_floor_seq"]),
        }
    if entry is not None and any(
        item["truncated"] or item.get("arguments_truncated") for item in entries
    ):
        result["next_offset"] = offset + width
    if summary:
        for name, role, descending in (
            ("task", "user", False),
            ("latest_answer", "assistant", True),
        ):
            selected = _store_lineage.ordered_rows(
                connection,
                ranges,
                columns=columns,
                where=f"{where} AND e.role = '{role}' AND length({_TEXT}) > 0",
                params=params,
                descending=descending,
                limit=1,
            )
            result[name] = _text_entry(selected[0], width) if selected else None
    return result


def _text_entry(row: sqlite3.Row, width: int) -> JsonObject:
    text = str(row["text"] or "")
    return {
        "seq": int(row["seq"]),
        "role": str(row["role"]),
        "at": str(row["created_at"]),
        "run_id": row["run_id"],
        "text": text[:width],
        "truncated": len(text) > width,
        "status": row["run_status"],
    }


def _entry(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    summary: bool,
    width: int,
    offset: int,
    visible_result: tuple[str, list[Any]],
    inherited: bool,
) -> JsonObject:
    item = _text_entry(row, width)
    key = int(row["entry_key"])
    if row["role"] == "tool":
        call = connection.execute(
            "SELECT c.call_key, c.name, c.status, "
            "COALESCE(c.result_ok, CASE WHEN json_valid(t.content) "
            "THEN json_extract(t.content, '$.ok') END) AS result_ok, "
            "c.error_code, c.duration_ms, "
            "substr(json_extract(p.display_json, '$.summary'), 1, 401) AS summary, "
            f"substr(p.arguments_json, {offset + 1}, {width + 1}) AS arguments, "
            "substr(CASE WHEN json_valid(t.content) THEN "
            "json_extract(t.content, '$.error.message') END, 1, 601) AS error, "
            "CASE WHEN json_valid(t.content) THEN "
            "json_extract(t.content, '$.data.exit_code') END AS exit_code, "
            "substr(CASE WHEN json_valid(t.content) THEN "
            "json_extract(t.content, '$.data.status') END, 1, 101) AS result_status, "
            "substr(CASE WHEN json_valid(t.content) THEN "
            "json_extract(t.content, '$.data.command_id') END, 1, 201) AS command_id "
            "FROM tool_calls c LEFT JOIN tool_call_payloads p ON p.call_key=c.call_key "
            "LEFT JOIN entry_text t ON t.entry_key=c.result_entry_key WHERE c.result_entry_key=?",
            (key,),
        ).fetchone()
        if call is not None:
            arguments = str(call["arguments"] or "")
            action = str(call["summary"] or arguments)
            action_width = 400 if call["summary"] else 200
            item.update(
                tool=call["name"],
                status="failed"
                if call["result_ok"] == 0 and call["status"] == "completed"
                else call["status"],
                ok=call["result_ok"],
                error_code=call["error_code"],
                error=call["error"],
                exit_code=call["exit_code"],
                result_status=call["result_status"],
                command_id=call["command_id"],
                duration_ms=call["duration_ms"],
                action=action[:action_width],
                action_truncated=len(action) > action_width,
            )
            changes = connection.execute(
                "SELECT substr(json_extract(f.value, '$.path'), 1, 201) AS path, "
                "json_extract(f.value, '$.change') AS change, "
                "json_extract(f.value, '$.added') AS added, "
                "json_extract(f.value, '$.removed') AS removed "
                "FROM tool_call_payloads p, json_each(p.display_json, '$.details') d, "
                "json_each(d.value, '$.files') f WHERE p.call_key=? "
                "AND json_extract(d.value, '$.type')='file_changes' LIMIT 11",
                (call["call_key"],),
            ).fetchall()
            item["file_changes"] = [
                {
                    **dict(change),
                    "path": str(change["path"] or "")[:200],
                    "path_truncated": len(str(change["path"] or "")) > 200,
                }
                for change in changes[:10]
            ]
            item["more_file_changes"] = len(changes) > 10
            if not summary:
                item.update(arguments=arguments[:width], arguments_truncated=len(arguments) > width)
            else:
                item.update(text="", truncated=False)
    if row["role"] == "assistant":
        # Only Results visible in this lineage can replace their declaration.
        # A Parent can complete a call after a fork has frozen its history.
        calls = connection.execute(
            "SELECT c.name, CASE WHEN NOT ? AND c.result_entry_key IS NULL THEN c.status "
            "ELSE 'unresolved' END AS status, substr(p.arguments_json, 1, 201) AS arguments "
            "FROM tool_calls c LEFT JOIN tool_call_payloads p ON p.call_key=c.call_key "
            f"WHERE c.entry_key=? AND NOT EXISTS ({visible_result[0]}) "
            "ORDER BY c.ordinal LIMIT 21",
            (inherited, key, *visible_result[1]),
        ).fetchall()
        item["pending_calls"] = [
            {
                **dict(call),
                "arguments": str(call["arguments"] or "")[:200],
                "arguments_truncated": len(str(call["arguments"] or "")) > 200,
            }
            for call in calls[:20]
        ]
        item["more_calls"] = len(calls) > 20
    return item
