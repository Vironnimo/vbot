"""Timeline identity and incremental reads within the Session read transaction."""

from __future__ import annotations

import bisect
import sqlite3
from collections.abc import Sequence

from core.sessions import _store_values


def can_append(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    after: tuple[str, int] | None,
) -> bool:
    """An edit changes an existing prefix; an append cursor cannot describe that."""
    if after is None:
        return False
    generation, sequence = after
    if generation != state["generation_id"] or not 0 <= sequence <= state["message_count"]:
        return False
    return (
        connection.execute(
            "SELECT 1 FROM messages WHERE session_key = ? AND seq >= ? "
            "AND role IN ('history_edit', 'agent_takeover') LIMIT 1",
            (state["session_key"], sequence),
        ).fetchone()
        is None
    )


def appended_rows(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    *,
    sequence: int,
    limit: int,
    excluded_roles: Sequence[str],
) -> tuple[list[sqlite3.Row], int]:
    # Bound raw records, including hidden Notes. An empty visible batch still
    # advances the cursor, and every continuation has a finite amount of work.
    through = min(sequence + limit, int(state["message_count"]))
    excluded = tuple(excluded_roles)
    where = "m.session_key = ? AND m.active = 1 AND m.seq >= ? AND m.seq < ?"
    if excluded:
        where += " AND m.role NOT IN (" + ",".join("?" for _ in excluded) + ")"
    rows = connection.execute(
        _store_values._message_records_sql(where=where, order_by="ORDER BY m.seq"),
        (state["session_key"], sequence, through, *excluded),
    ).fetchall()
    return rows, through


def record_run_ids(
    connection: sqlite3.Connection, state: sqlite3.Row, rows: Sequence[sqlite3.Row]
) -> tuple[str | None, ...]:
    """Project canonical Run boundaries, without changing ChatMessage role fields.

    Recorded starts delimit current executions even without a visible User or
    terminal annotation. Historical/forked segments have canonical summaries
    but may have no admission record; their persisted summary boundaries remain
    sufficient to identify the segment. Neither path inspects content or live
    replay coverage.
    """
    if not rows:
        return ()
    key = int(state["session_key"])
    first, last = int(rows[0]["seq"]), int(rows[-1]["seq"])
    starts = connection.execute(
        "SELECT run_id, start_sequence FROM run_execution_starts "
        "WHERE session_key = ? AND generation_id = ? AND start_sequence <= ? "
        "AND start_sequence >= COALESCE((SELECT MAX(start_sequence) "
        "FROM run_execution_starts WHERE session_key = ? AND generation_id = ? "
        "AND start_sequence <= ?), 0) "
        "ORDER BY start_sequence, record_key",
        (key, state["generation_id"], last, key, state["generation_id"], first),
    ).fetchall()
    summaries = connection.execute(
        "SELECT m.seq, r.run_id, s.start_sequence FROM messages m "
        "JOIN run_summaries r ON r.message_key = m.message_key "
        "LEFT JOIN run_execution_starts s ON s.session_key = m.session_key "
        "AND s.run_id = r.run_id "
        "WHERE m.session_key = ? AND m.role = 'run_summary' AND m.active = 1 "
        "AND m.seq >= ? AND m.seq <= "
        "COALESCE((SELECT MIN(seq) FROM messages WHERE session_key = ? "
        "AND active = 1 AND role = 'run_summary' AND seq >= ?), ?) ORDER BY m.seq",
        (key, first, key, last, last),
    ).fetchall()
    start_sequences = [int(row["start_sequence"]) for row in starts]
    summary_sequences = [int(row["seq"]) for row in summaries]
    # A recorded Run stops at its own summary, even if unrelated records are
    # appended before the next Run. Include an earlier summary for this check.
    ends = {
        str(row["run_id"]): int(row["seq"])
        for row in connection.execute(
            "SELECT r.run_id, m.seq FROM messages m "
            "JOIN run_summaries r ON r.message_key = m.message_key "
            "WHERE m.session_key = ? AND m.role = 'run_summary' AND m.seq >= ? AND m.seq <= ?",
            (key, start_sequences[0] if starts else first, last),
        )
    }
    result: list[str | None] = []
    for row in rows:
        sequence = int(row["seq"])
        start_index = bisect.bisect_right(start_sequences, sequence) - 1
        summary_index = bisect.bisect_left(summary_sequences, sequence)
        run_id: str | None = None
        if start_index >= 0:
            candidate = str(starts[start_index]["run_id"])
            if sequence <= ends.get(candidate, sequence):
                run_id = candidate
        if summary_index < len(summaries):
            summary = summaries[summary_index]
            if (
                summary["start_sequence"] is None
                and (start_index < 0 or summary["run_id"] == run_id)
                or summary["start_sequence"] is not None
                and sequence >= summary["start_sequence"]
            ):
                run_id = str(summary["run_id"])
        result.append(run_id)
    return tuple(result)
