"""Store reads."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import sqlite3

from ._store_database import (
    SwarmDatabase,
)
from ._store_records import (
    _discussion,
    _discussion_high_water,
    _human_posts,
    _main,
    _participant,
    _pending_rows_from_rows,
    _post_high_water,
)
from ._store_values import (
    Page,
    SwarmStoreError,
    _load,
    _page,
    _post,
)


def _list_discussions(
    db: SwarmDatabase, swarm_id: str, participant_id: str, cursor: str | None, limit: int
) -> Page:
    connection = db._require_connection()
    _participant(connection, swarm_id, participant_id)
    high_water = _discussion_high_water(connection, swarm_id)
    if cursor:
        offset, frozen_high_water = db._cursor(
            cursor, "discussions", f"{swarm_id}:{participant_id}", high_water
        )
        if frozen_high_water is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen_high_water
    else:
        offset = 0
    rows = connection.execute(
        "SELECT d.*, EXISTS(SELECT 1 FROM memberships m WHERE m.discussion_id=d.id AND m.participant_id=?) AS joined, (SELECT COUNT(*) FROM memberships m WHERE m.discussion_id=d.id) AS member_count, "
        "(SELECT COUNT(*) FROM recipients r JOIN posts p ON p.id=r.post_id WHERE p.discussion_id=d.id AND r.participant_id=? AND r.delivered_at IS NULL) AS pending_count "
        "FROM discussions d WHERE d.swarm_id=? AND d.sequence<=? ORDER BY d.is_main DESC,d.sequence LIMIT ? OFFSET ?",
        (participant_id, participant_id, swarm_id, high_water, limit + 1, offset),
    ).fetchall()
    entries = [
        {
            "id": r["id"],
            "title": r["title"],
            "joined": bool(r["joined"]),
            "member_count": r["member_count"],
            "pending_count": r["pending_count"],
        }
        for r in rows
    ]
    return _page(
        entries,
        limit,
        db._make_cursor("discussions", f"{swarm_id}:{participant_id}", high_water, offset + limit),
    )


def _list_human_discussions(
    db: SwarmDatabase, swarm_id: str, cursor: str | None, limit: int
) -> Page:
    connection = db._require_connection()
    if connection.execute("SELECT 1 FROM swarms WHERE id=?", (swarm_id,)).fetchone() is None:
        raise SwarmStoreError("swarm_not_found")
    high_water = _discussion_high_water(connection, swarm_id)
    scope = f"human:{swarm_id}"
    if cursor:
        offset, frozen_high_water = db._cursor(cursor, "human_discussions", scope, high_water)
        if frozen_high_water is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen_high_water
    else:
        offset = 0
    rows = connection.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM memberships m WHERE m.discussion_id=d.id) AS member_count "
        "FROM discussions d WHERE d.swarm_id=? AND d.sequence<=? "
        "ORDER BY d.is_main DESC,d.sequence LIMIT ? OFFSET ?",
        (swarm_id, high_water, limit + 1, offset),
    ).fetchall()
    return _page(
        [
            {
                "id": row["id"],
                "title": row["title"],
                "member_count": row["member_count"],
            }
            for row in rows
        ],
        limit,
        db._make_cursor("human_discussions", scope, high_water, offset + limit),
    )


def _read_posts(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    discussion_id: str | None,
    message_id: str | None,
    cursor: str | None,
    limit: int,
) -> Page:
    connection = db._require_connection()
    _participant(connection, swarm_id, participant_id)
    if message_id is not None:
        row = connection.execute(
            "SELECT p.*,d.title AS discussion_title,r.route_class FROM posts p "
            "JOIN discussions d ON d.id=p.discussion_id "
            "LEFT JOIN recipients r ON r.post_id=p.id AND r.participant_id=? "
            "WHERE p.id=? AND p.swarm_id=?",
            (participant_id, message_id, swarm_id),
        ).fetchone()
        if row is None:
            raise SwarmStoreError("message_not_found")
        return Page((_post(row),), False, None)
    discussion_id = discussion_id or _main(connection, swarm_id)
    _discussion(connection, swarm_id, discussion_id)
    return _post_page(db, connection, swarm_id, participant_id, discussion_id, cursor, limit)


def _read_human_posts(
    db: SwarmDatabase,
    swarm_id: str,
    discussion_id: str | None,
    message_id: str | None,
    cursor: str | None,
    limit: int,
) -> Page:
    connection = db._require_connection()
    if connection.execute("SELECT 1 FROM swarms WHERE id=?", (swarm_id,)).fetchone() is None:
        raise SwarmStoreError("swarm_not_found")
    if message_id is not None:
        row = connection.execute(
            "SELECT * FROM posts WHERE id=? AND swarm_id=?", (message_id, swarm_id)
        ).fetchone()
        if row is None:
            raise SwarmStoreError("message_not_found")
        return Page(tuple(_human_posts(connection, [row])), False, None)
    discussion_id = discussion_id or _main(connection, swarm_id)
    _discussion(connection, swarm_id, discussion_id)
    return _human_post_page(db, connection, swarm_id, discussion_id, cursor, limit)


def _post_page(
    db: SwarmDatabase,
    connection: sqlite3.Connection,
    swarm_id: str,
    participant_id: str,
    discussion_id: str,
    cursor: str | None,
    limit: int,
) -> Page:
    high_water = _post_high_water(connection, swarm_id, discussion_id)
    scope = f"{swarm_id}:{participant_id}:{discussion_id}"
    if cursor:
        offset, frozen_high_water = db._cursor(cursor, "posts", scope, high_water)
        if frozen_high_water is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen_high_water
    else:
        offset = 0
    batch_chars = _load(
        connection.execute(
            "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    )["batch_chars"]
    rows = connection.execute(
        "SELECT p.*,d.title AS discussion_title,r.route_class FROM posts p "
        "JOIN discussions d ON d.id=p.discussion_id "
        "LEFT JOIN recipients r ON r.post_id=p.id AND r.participant_id=? "
        "WHERE p.swarm_id=? AND p.discussion_id=? AND p.sequence<=? "
        "ORDER BY p.sequence DESC LIMIT ? OFFSET ?",
        (participant_id, swarm_id, discussion_id, high_water, limit + 1, offset),
    ).fetchall()
    selected = _pending_rows_from_rows(rows, limit, batch_chars)
    values = [_post(row) for row in reversed(selected)]
    more = len(rows) > len(selected)
    return Page(
        tuple(values),
        more,
        db._make_cursor("posts", scope, high_water, offset + len(selected)) if more else None,
    )


def _human_post_page(
    db: SwarmDatabase,
    connection: sqlite3.Connection,
    swarm_id: str,
    discussion_id: str,
    cursor: str | None,
    limit: int,
) -> Page:
    high_water = _post_high_water(connection, swarm_id, discussion_id)
    scope = f"human:{swarm_id}:{discussion_id}"
    if cursor:
        offset, frozen_high_water = db._cursor(cursor, "human_posts", scope, high_water)
        if frozen_high_water is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen_high_water
    else:
        offset = 0
    batch_chars = _load(
        connection.execute(
            "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    )["batch_chars"]
    rows = connection.execute(
        "SELECT * FROM posts WHERE swarm_id=? AND discussion_id=? AND sequence<=? "
        "ORDER BY sequence DESC LIMIT ? OFFSET ?",
        (swarm_id, discussion_id, high_water, limit + 1, offset),
    ).fetchall()
    selected = _pending_rows_from_rows(rows, limit, batch_chars)
    values = _human_posts(connection, list(reversed(selected)))
    more = len(rows) > len(selected)
    return Page(
        tuple(values),
        more,
        db._make_cursor("human_posts", scope, high_water, offset + len(selected)) if more else None,
    )
