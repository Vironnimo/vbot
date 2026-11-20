"""Per-Run read state and Schedule provenance, in the Session transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from core.sessions import _store_values
from core.sessions._types import JsonObject, SessionAddress
from core.sessions.notifications import NotificationPolicy


def backfill(connection: sqlite3.Connection) -> None:
    # Only the old latest result has a known read state. Never turn the whole
    # conversation's past into newly unread results during an update.
    connection.execute(
        "INSERT OR IGNORE INTO run_notifications (run_key, session_key, eligible, is_read) "
        "SELECT r.run_key, r.session_key, 1, "
        "CASE WHEN s.read_completion_run_key = r.run_key THEN 1 ELSE 0 END "
        "FROM sessions AS s JOIN runs AS r ON r.run_key = s.latest_completion_run_key "
        "WHERE r.inherited = 0 AND r.completed_at IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM entries AS e WHERE e.entry_key = r.end_entry_key "
        "AND e.superseded_at_seq IS NOT NULL)"
    )


def set_policy(connection: sqlite3.Connection, address: SessionAddress, policy: str) -> bool:
    policy = NotificationPolicy(policy)
    key = int(_store_values._require_live(connection, address)["session_key"])
    return (
        connection.execute(
            "UPDATE sessions SET notification_policy = ?, state_revision = state_revision + 1 "
            "WHERE session_key = ? AND notification_policy != ?",
            (policy, key, policy),
        ).rowcount
        > 0
    )


def mark_runs_read(
    connection: sqlite3.Connection, address: SessionAddress, run_ids: Sequence[str]
) -> list[str]:
    key = int(_store_values._require_live(connection, address)["session_key"])
    changed = _mark_read(
        connection,
        key,
        "r.run_id IN (SELECT value FROM json_each(?))",
        (_store_values._json_list(run_ids),),
    )
    if changed:
        _store_values._touch_state(connection, key)
    return changed


def mark_runs_read_through(
    connection: sqlite3.Connection, address: SessionAddress, run_id: str
) -> list[str]:
    key = int(_store_values._require_live(connection, address)["session_key"])
    boundary = connection.execute(
        "SELECT e.seq FROM runs AS r JOIN entries AS e ON e.entry_key = r.end_entry_key "
        "WHERE r.session_key = ? AND r.run_id = ? AND r.inherited = 0 "
        "AND e.superseded_at_seq IS NULL",
        (key, run_id),
    ).fetchone()
    if boundary is None:
        return []
    changed = mark_through_sequence(connection, key, boundary["seq"])
    if changed:
        _store_values._touch_state(connection, key)
    return changed


def mark_through_sequence(
    connection: sqlite3.Connection, session_key: int, through: int
) -> list[str]:
    return _mark_read(connection, session_key, "e.seq <= ?", (through,))


def _mark_read(
    connection: sqlite3.Connection, session_key: int, predicate: str, params: tuple[str | int, ...]
) -> list[str]:
    rows = connection.execute(
        "UPDATE run_notifications SET is_read = 1 WHERE session_key = ? AND is_read = 0 "
        "AND EXISTS (SELECT 1 FROM runs AS r "
        "JOIN entries AS e ON e.entry_key = r.end_entry_key "
        "WHERE r.run_key = run_notifications.run_key "
        "AND r.session_key = run_notifications.session_key "
        "AND r.inherited = 0 AND e.superseded_at_seq IS NULL "
        f"AND {predicate}) "
        "RETURNING (SELECT run_id FROM runs WHERE runs.run_key = run_notifications.run_key)",
        (session_key, *params),
    ).fetchall()
    if rows:
        # Maintain the Generation 1 legacy read field for older writers and readers.
        connection.execute(
            "UPDATE sessions SET read_completion_run_key = latest_completion_run_key "
            "WHERE session_key = ? AND EXISTS (SELECT 1 FROM run_notifications AS n "
            "WHERE n.run_key = latest_completion_run_key AND n.is_read = 1)",
            (session_key,),
        )
    return [row[0] for row in rows]


def schedule_activity(
    connection: sqlite3.Connection, busy: Sequence[SessionAddress] = ()
) -> dict[str, JsonObject]:
    activity = {
        row["schedule_id"]: dict(row)
        for row in connection.execute(
            "SELECT n.schedule_id, MAX(r.started_at) AS last_activity_at, "
            "MAX(CASE WHEN r.completed_at IS NOT NULL THEN r.run_key END) AS read_through, "
            "COUNT(*) AS run_count, "
            "SUM(n.eligible = 1 AND n.is_read = 0) AS unread_count, "
            "SUM(n.eligible = 1 AND n.is_read = 0 AND "
            "r.status IN ('failed', 'interrupted')) AS unread_error_count "
            "FROM run_notifications AS n JOIN runs AS r ON r.run_key = n.run_key "
            "JOIN sessions AS s ON s.session_key = n.session_key "
            "WHERE n.schedule_id IS NOT NULL AND s.state = 'live' "
            "AND NOT EXISTS (SELECT 1 FROM entries AS e WHERE e.entry_key = r.end_entry_key "
            "AND e.superseded_at_seq IS NOT NULL) "
            "GROUP BY n.schedule_id"
        )
    }

    for row in activity.values():
        row["visible_unread_count"] = row["unread_count"]
        row["visible_unread_error_count"] = row["unread_error_count"]
    # Work state is process-local and owned by SessionActivity. Its snapshot
    # filters attention, never durable read state or the history itself.
    for address in busy:
        for row in connection.execute(
            "SELECT n.schedule_id, COUNT(*) AS unread_count, "
            "SUM(r.status IN ('failed', 'interrupted')) AS unread_error_count "
            "FROM run_notifications AS n JOIN runs AS r ON r.run_key = n.run_key "
            "WHERE n.session_key = (SELECT session_key FROM sessions WHERE state = 'live' "
            "AND project_id = ? AND agent_id = ? AND session_id = ?) "
            "AND n.eligible = 1 AND n.is_read = 0 AND n.schedule_id IS NOT NULL "
            "GROUP BY n.schedule_id",
            (address.project_id or "", address.agent_id, address.session_id),
        ):
            if item := activity.get(row["schedule_id"]):
                item["visible_unread_count"] -= row["unread_count"]
                item["visible_unread_error_count"] -= row["unread_error_count"]
    return activity


def schedule_runs(
    connection: sqlite3.Connection, schedule_id: str, *, before: int | None, limit: int
) -> JsonObject:
    rows = connection.execute(
        "SELECT r.run_key, r.run_id, r.status, r.started_at, r.completed_at, "
        "r.duration_ms, s.agent_id, s.project_id, s.session_id, "
        "COALESCE(s.title, s.auto_title, s.session_id) AS title, "
        "n.eligible = 1 AND n.is_read = 0 AS unread "
        "FROM run_notifications AS n JOIN runs AS r ON r.run_key = n.run_key "
        "JOIN sessions AS s ON s.session_key = n.session_key "
        "WHERE n.schedule_id = ? AND s.state = 'live' "
        "AND NOT EXISTS (SELECT 1 FROM entries AS e WHERE e.entry_key = r.end_entry_key "
        "AND e.superseded_at_seq IS NOT NULL) "
        "AND (? IS NULL OR n.run_key < ?) ORDER BY n.run_key DESC LIMIT ?",
        (schedule_id, before, before, limit + 1),
    ).fetchall()
    return {
        "runs": [dict(row) | {"unread": bool(row["unread"])} for row in rows[:limit]],
        "next_before": rows[limit - 1]["run_key"] if len(rows) > limit else None,
    }


def mark_schedule_read(
    connection: sqlite3.Connection, schedule_id: str, through: int
) -> list[tuple[SessionAddress, str]]:
    rows = connection.execute(
        "SELECT s.project_id, s.agent_id, s.session_id, r.run_id FROM run_notifications AS n "
        "JOIN runs AS r ON r.run_key = n.run_key "
        "JOIN sessions AS s ON s.session_key = n.session_key "
        "WHERE n.schedule_id = ? AND n.run_key <= ? AND n.is_read = 0 "
        "AND r.completed_at IS NOT NULL AND s.state = 'live'",
        (schedule_id, through),
    ).fetchall()
    changed = []
    for row in rows:
        address = SessionAddress(row["project_id"] or None, row["agent_id"], row["session_id"])
        for run_id in mark_runs_read(connection, address, [row["run_id"]]):
            changed.append((address, run_id))
    return changed


def adopt_schedule_runs(
    connection: sqlite3.Connection, sources: Sequence[tuple[str, str, str | None, str | None, str]]
) -> None:
    for schedule_id, agent_id, project_id, run_id, policy in sources:
        row = connection.execute(
            "SELECT r.run_key, r.session_key FROM runs AS r "
            "JOIN sessions AS s ON s.session_key = r.session_key "
            "WHERE s.agent_id = ? AND s.project_id = ? AND r.run_id = ? AND r.inherited = 0",
            (agent_id, project_id or "", run_id),
        ).fetchone()
        if row is None:
            continue
        changed = connection.execute(
            "INSERT INTO run_notifications (run_key, session_key, schedule_id, policy) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_key) DO UPDATE SET schedule_id = excluded.schedule_id, "
            "policy = excluded.policy "
            "WHERE run_notifications.schedule_id IS NULL",
            (*row, schedule_id, policy),
        ).rowcount
        if changed:
            _store_values._touch_state(connection, row["session_key"])


def run_status(connection: sqlite3.Connection, address: SessionAddress, run_id: str) -> JsonObject:
    key = int(_store_values._require_live(connection, address)["session_key"])
    row = connection.execute(
        "SELECT n.schedule_id, n.eligible, n.is_read, r.status, s.notification_policy AS policy "
        "FROM run_notifications AS n JOIN runs AS r ON r.run_key = n.run_key "
        "JOIN sessions AS s ON s.session_key = n.session_key "
        "WHERE n.session_key = ? AND r.run_id = ?",
        (key, run_id),
    ).fetchone()
    return dict(row) if row is not None else {"eligible": False, "is_read": True}
