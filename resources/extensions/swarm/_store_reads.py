"""Store reads."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import re
import sqlite3
from difflib import get_close_matches

from ._store_database import (
    ALIASED_DISCUSSION_COLUMNS,
    ALIASED_POST_COLUMNS,
    POST_COLUMNS,
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
    Json,
    Page,
    SwarmStoreError,
    _load,
    _page,
    _post,
)


def _list_discussions(
    db: SwarmDatabase, swarm_id: str, participant_id: str, cursor: str | None, limit: int
) -> Page:
    with db._read() as connection:
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
            f"SELECT {ALIASED_DISCUSSION_COLUMNS}, EXISTS(SELECT 1 FROM memberships m WHERE m.discussion_id=d.id AND m.participant_id=?) AS joined, (SELECT COUNT(*) FROM memberships m WHERE m.discussion_id=d.id) AS member_count, "
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
            db._make_cursor(
                "discussions", f"{swarm_id}:{participant_id}", high_water, offset + limit
            ),
        )


def _list_human_discussions(
    db: SwarmDatabase, swarm_id: str, cursor: str | None, limit: int
) -> Page:
    with db._read() as connection:
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
            f"SELECT {ALIASED_DISCUSSION_COLUMNS}, (SELECT COUNT(*) FROM memberships m WHERE m.discussion_id=d.id) AS member_count "
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
    before: str | None = None,
) -> Page:
    with db._read() as connection:
        _participant(connection, swarm_id, participant_id)
        if message_id is not None:
            row = connection.execute(
                f"SELECT {ALIASED_POST_COLUMNS},d.title AS discussion_title,r.route_class FROM posts p "
                "JOIN discussions d ON d.id=p.discussion_id "
                "LEFT JOIN recipients r ON r.post_id=p.id AND r.participant_id=? "
                "WHERE p.id=? AND p.swarm_id=?",
                (participant_id, message_id, swarm_id),
            ).fetchone()
            if row is None:
                raise SwarmStoreError("message_not_found", field="message_id")
            return Page((_post(row),), False, None)
        before_sequence = None
        if before is not None:
            anchor = connection.execute(
                "SELECT discussion_id,sequence FROM posts WHERE id=? AND swarm_id=?",
                (before, swarm_id),
            ).fetchone()
            if anchor is None:
                raise SwarmStoreError("message_not_found", field="before")
            if discussion_id is not None and discussion_id != anchor["discussion_id"]:
                _discussion(connection, swarm_id, discussion_id)
                raise SwarmStoreError("before_discussion_mismatch", field="before")
            discussion_id, before_sequence = anchor["discussion_id"], int(anchor["sequence"])
        discussion_id = discussion_id or _main(connection, swarm_id)
        _discussion(connection, swarm_id, discussion_id)
        return _post_page(
            db,
            connection,
            swarm_id,
            participant_id,
            discussion_id,
            cursor,
            limit,
            before_sequence=before_sequence,
        )


def _post_suggestions(db: SwarmDatabase, swarm_id: str, value: str) -> list[Json]:
    """Return posts a mistyped post reference may mean; callers decide whether to use them.

    A number, "#number", or "pst_number" names the post with that Board sequence
    number. Otherwise post IDs close to the value qualify.
    """

    with db._read() as connection:
        number = re.fullmatch(r"(?:pst_)?#?(\d+)", value.strip())
        parameters: list[int | str]
        if number is not None:
            where, parameters = "p.sequence=?", [int(number.group(1))]
        else:
            ids = [
                str(row[0])
                for row in connection.execute("SELECT id FROM posts WHERE swarm_id=?", (swarm_id,))
            ]
            close = get_close_matches(value.strip(), ids, n=3, cutoff=0.85)
            if not close:
                return []
            where, parameters = f"p.id IN ({','.join('?' * len(close))})", list(close)
        rows = connection.execute(
            "SELECT p.id,p.sequence,p.discussion_id,p.author_name,p.text,d.title AS discussion_title "
            f"FROM posts p JOIN discussions d ON d.id=p.discussion_id WHERE p.swarm_id=? AND {where}",
            (swarm_id, *parameters),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "by_number": number is not None,
                "discussion_id": row["discussion_id"],
                "discussion_title": row["discussion_title"],
                "author_name": row["author_name"],
                "text": row["text"],
            }
            for row in rows
        ]


def _read_human_posts(
    db: SwarmDatabase,
    swarm_id: str,
    discussion_id: str | None,
    message_id: str | None,
    cursor: str | None,
    limit: int,
) -> Page:
    with db._read() as connection:
        if connection.execute("SELECT 1 FROM swarms WHERE id=?", (swarm_id,)).fetchone() is None:
            raise SwarmStoreError("swarm_not_found")
        if message_id is not None:
            row = connection.execute(
                f"SELECT {POST_COLUMNS} FROM posts WHERE id=? AND swarm_id=?",
                (message_id, swarm_id),
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
    *,
    before_sequence: int | None = None,
) -> Page:
    high_water = _post_high_water(connection, swarm_id, discussion_id)
    scope = f"{swarm_id}:{participant_id}:{discussion_id}"
    if before_sequence is not None:
        # The page ends just before a known post; its cursor continues from there.
        offset, high_water = 0, min(high_water, before_sequence - 1)
    elif cursor:
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
        f"SELECT {ALIASED_POST_COLUMNS},d.title AS discussion_title,r.route_class "
        "FROM posts p INDEXED BY posts_discussion_page "
        "JOIN discussions d ON d.id=p.discussion_id "
        "LEFT JOIN recipients r ON r.post_id=p.id AND r.participant_id=? "
        "WHERE p.swarm_id=? AND p.discussion_id=? AND p.sequence>0 AND p.sequence<=? "
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
        f"SELECT {POST_COLUMNS} FROM posts INDEXED BY posts_discussion_page "
        "WHERE swarm_id=? AND discussion_id=? AND sequence>0 AND sequence<=? "
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
