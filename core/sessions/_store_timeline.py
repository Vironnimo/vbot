"""Timeline identity and incremental reads within the Session read transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from core.sessions import _store_values
from core.sessions._types import JsonObject


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
    return sequence >= int(state["history_reset_sequence"])


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
    """Run membership is written with each entity, never reconstructed."""
    return tuple(row["owner_run_id"] for row in rows)


def page_runs(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    rows: Sequence[sqlite3.Row],
    *,
    through: int,
    incremental: bool,
) -> tuple[JsonObject, ...]:
    identities = {row["owner_run_id"] for row in rows if row["owner_run_id"] is not None}
    if not identities:
        return ()
    floor = min(int(row["seq"]) for row in rows)
    result = []
    for run_id in identities:
        run = connection.execute(
            "SELECT run_id,status,start_sequence,terminal_sequence FROM runs "
            "WHERE session_key=? AND run_id=?",
            (state["session_key"], run_id),
        ).fetchone()
        assert run is not None
        terminal = run["terminal_sequence"]
        result.append(
            {
                "run_id": run_id,
                "status": run["status"],
                "start_sequence": run["start_sequence"],
                "terminal_sequence": terminal,
                "complete": terminal is not None
                and terminal < through
                and (
                    incremental
                    or int(run["start_sequence"]) >= floor
                    or not connection.execute(
                        "SELECT 1 FROM history_records WHERE session_key=? "
                        "AND owner_run_id=? AND active=1 AND seq<? "
                        "AND role NOT IN ('system','note','history_edit') LIMIT 1",
                        (state["session_key"], run_id, floor),
                    ).fetchone()
                ),
            }
        )
    return tuple(result)
