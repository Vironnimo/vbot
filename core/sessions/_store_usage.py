"""Minimal accounting export over every retained Session's own audit.

This read intentionally includes archives and superseded entries. Fork lineage
and materialized inherited prefixes never create a second billable request.
It exports accounting metadata only, without decoding Message content.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
_FLAGS = ("input_tokens_estimated", "output_tokens_estimated")


def usage_history(
    connection: sqlite3.Connection, after_entry_key: int, *, limit: int
) -> Callable[[], tuple[dict[str, Any], ...]]:
    """Page billable own entries by immutable canonical entry key."""
    rows = connection.execute(
        "SELECT e.entry_key,e.entry_id AS message_id,e.created_at,e.model,e.role,r.run_id,"
        "s.generation_id,s.agent_id,s.project_id,s.session_id,"
        "COALESCE(s.title,s.auto_title,s.session_id) AS session_title,"
        "b.owner_name,b.group_id,a.interrupted,a.usage_extra_json AS assistant_usage,"
        "c.usage_extra_json AS checkpoint_usage,"
        + ",".join(f"a.{key}" for key in (*_COUNTERS, *_FLAGS))
        + " FROM entries AS e JOIN sessions AS s ON s.session_key=e.session_key "
        "LEFT JOIN runs AS r ON r.run_key=e.run_key "
        "LEFT JOIN assistant_entries AS a ON a.entry_key=e.entry_key "
        "LEFT JOIN checkpoint_entries AS c ON c.entry_key=e.entry_key "
        "LEFT JOIN temporary_session_bindings AS b ON b.session_key=s.session_key "
        "WHERE e.entry_key>? AND e.seq>=COALESCE(s.fork_point_seq,0) "
        "AND (e.role='assistant' OR (e.role='compaction_checkpoint' "
        "AND json_type(c.usage_extra_json,'$.model_call.usage')='object')) "
        "ORDER BY e.entry_key LIMIT ?",
        (after_entry_key, limit),
    ).fetchall()
    return lambda: _decode_usage(rows)


def _decode_usage(rows: Sequence[sqlite3.Row]) -> tuple[dict[str, Any], ...]:
    result = []
    for row in rows:
        if row["role"] == "assistant":
            usage = json.loads(row["assistant_usage"] or "{}")
            usage.update({key: row[key] for key in _COUNTERS if row[key] is not None})
            usage.update({key: bool(row[key]) for key in _FLAGS if row[key] is not None})
            if any(usage.get(key) is True for key in _FLAGS):
                usage["estimated"] = True
            model, kind = row["model"], "chat"
        else:
            call = json.loads(row["checkpoint_usage"])["model_call"]
            usage, model, kind = call["usage"], call.get("model"), "compaction"
        result.append(
            {
                **{
                    key: row[key]
                    for key in (
                        "entry_key",
                        "message_id",
                        "generation_id",
                        "agent_id",
                        "project_id",
                        "session_id",
                        "session_title",
                        "run_id",
                        "owner_name",
                        "group_id",
                    )
                },
                "project_id": row["project_id"] or None,
                "timestamp": row["created_at"],
                "model": model,
                "kind": kind,
                "interrupted": bool(row["interrupted"]),
                "usage": usage,
            }
        )
    return tuple(result)
