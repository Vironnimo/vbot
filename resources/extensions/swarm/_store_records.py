"""Store records."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import sqlite3

from ._store_values import (
    _MUTABLE_SWARM_STATES,
    Json,
    SwarmStoreError,
    _dump,
    _load,
    _now,
    _post,
)


def _request_replay(
    connection: sqlite3.Connection, scope: str, request_id: str, payload_hash: str
) -> Json | None:
    row = connection.execute(
        "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
        (scope, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["payload_hash"] != payload_hash:
        raise SwarmStoreError("request_conflict")
    value = _load(row["outcome"])
    value["replayed"] = True
    return value


def _record_lifecycle_request(
    connection: sqlite3.Connection,
    scope: str,
    request_id: str,
    payload_hash: str,
    swarm_id: str,
    kind: str,
    actor: str,
    old_state: str,
    result: Json,
) -> None:
    connection.execute(
        "INSERT INTO swarm_events(swarm_id,kind,actor,old_json,new_json,settings_revision,created_at) VALUES(?,?,?,?,?,?,?)",
        (swarm_id, kind, actor, _dump({"state": old_state}), _dump(result), None, _now()),
    )
    connection.execute(
        "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
        (scope, request_id, payload_hash, _dump(result)),
    )


def _pending_rows_from_rows(
    rows: list[sqlite3.Row], limit: int, batch_chars: int
) -> list[sqlite3.Row]:
    selected: list[sqlite3.Row] = []
    chars = 0
    for row in rows[:limit]:
        size = len(str(row["text"]))
        if selected and chars + size > batch_chars:
            break
        selected.append(row)
        chars += size
    return selected


def _pending_rows(
    connection: sqlite3.Connection,
    swarm_id: str,
    participant_id: str,
    limit: int,
    batch_chars: int,
) -> list[sqlite3.Row]:
    candidates = connection.execute(
        "SELECT p.*,r.route_class,d.title AS discussion_title "
        "FROM recipients r JOIN posts p ON p.id=r.post_id "
        "JOIN discussions d ON d.id=p.discussion_id "
        "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL ORDER BY p.sequence LIMIT ?",
        (swarm_id, participant_id, limit),
    ).fetchall()
    selected: list[sqlite3.Row] = []
    char_count = 0
    for row in candidates:
        text_size = len(str(row["text"]))
        if selected and char_count + text_size > batch_chars:
            break
        selected.append(row)
        char_count += text_size
    return selected


def _human_posts(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Json]:
    values = [_post(row) for row in rows]
    if not values:
        return values
    # Creation receipts identify announcements independently of their body text.
    placeholders = ",".join("?" for _ in values)
    announcements = {
        row["post_id"]: {
            "discussion_id": row["discussion_id"],
            "title": row["title"],
            "opening_post_id": row["opening_post_id"],
        }
        for row in connection.execute(
            "SELECT p.id AS post_id,d.id AS discussion_id,d.title,"
            "json_extract(q.outcome,'$.opening_post_id') AS opening_post_id "
            "FROM posts p JOIN requests q ON q.scope='create:'||p.swarm_id||':'||p.author_id "
            "AND json_extract(q.outcome,'$.main_announcement_id')=p.id "
            "JOIN discussions d ON d.id=json_extract(q.outcome,'$.discussion_id') "
            "AND d.swarm_id=p.swarm_id "
            f"WHERE p.id IN ({placeholders})",
            [value["id"] for value in values],
        )
    }
    for value in values:
        if value["id"] in announcements:
            value["discussion_announcement"] = announcements[value["id"]]
    return values


def _participant(connection: sqlite3.Connection, swarm_id: str, participant_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM participants WHERE swarm_id=? AND id=?", (swarm_id, participant_id)
    ).fetchone()
    if row is None:
        raise SwarmStoreError("participant_not_found")
    return row


def _pending_count(connection: sqlite3.Connection, swarm_id: str, participant_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM recipients r JOIN posts p ON p.id=r.post_id "
            "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
            (swarm_id, participant_id),
        ).fetchone()[0]
    )


def _assert_mutable(connection: sqlite3.Connection, swarm_id: str) -> None:
    row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
    if row is None:
        raise SwarmStoreError("swarm_not_found")
    epoch = connection.execute(
        "SELECT is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
    ).fetchone()
    if row["state"] not in _MUTABLE_SWARM_STATES or epoch is None or not epoch["is_open"]:
        raise SwarmStoreError("swarm_closed")


def _assert_epoch(connection: sqlite3.Connection, swarm_id: str, expected_epoch: int) -> None:
    _assert_mutable(connection, swarm_id)
    row = connection.execute(
        "SELECT epoch FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
    ).fetchone()
    if row is None or int(row["epoch"]) != expected_epoch:
        raise SwarmStoreError("stale_epoch")


def _discussion(connection: sqlite3.Connection, swarm_id: str, discussion_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM discussions WHERE swarm_id=? AND id=?", (swarm_id, discussion_id)
    ).fetchone()
    if row is None:
        raise SwarmStoreError("discussion_not_found")
    return row


def _main(connection: sqlite3.Connection, swarm_id: str) -> str:
    return str(
        connection.execute(
            "SELECT id FROM discussions WHERE swarm_id=? AND is_main=1", (swarm_id,)
        ).fetchone()[0]
    )


def _discussion_high_water(connection: sqlite3.Connection, swarm_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM discussions WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    )


def _post_high_water(connection: sqlite3.Connection, swarm_id: str, discussion_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM posts WHERE swarm_id=? AND discussion_id=?",
            (swarm_id, discussion_id),
        ).fetchone()[0]
    )
