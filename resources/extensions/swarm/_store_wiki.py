"""Swarm-owned Wiki transactions and bounded, revision-stable reads."""

from __future__ import annotations

import sqlite3
from itertools import islice
from typing import cast

from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

from ._store_database import (
    ALIASED_WIKI_REVISION_COLUMNS,
    WIKI_PAGE_COLUMNS,
    WIKI_REVISION_COLUMNS,
    SwarmDatabase,
)
from ._store_records import _assert_epoch, _assert_mutable, _participant
from ._store_values import Json, SwarmStoreError, _dump, _hash, _load, _request_id
from ._wiki_edit import EditMiss, apply_text_edit

MUTATIONS = {"create", "update", "delete", "restore"}
_FIELDS = {
    "list": {"query", "include_deleted", "cursor", "limit"},
    "read": {"page_id", "revision", "offset", "limit"},
    "history": {"page_id", "cursor", "limit"},
    "create": {"title", "content", "request_id"},
    "update": {
        "page_id",
        "title",
        "content",
        "old_text",
        "new_text",
        "expected_revision",
        "request_id",
    },
    "delete": {"page_id", "expected_revision", "request_id"},
    "restore": {"page_id", "revision", "expected_revision", "request_id"},
}


def _validate(arguments: Json) -> str:
    action = arguments.get("action")
    if not isinstance(action, str) or action not in _FIELDS:
        raise SwarmStoreError("invalid_arguments", field="action")
    for field in arguments.keys() - {"action", *_FIELDS[action]}:
        raise SwarmStoreError("inapplicable_field", field=field)
    required = {"page_id"} if action not in {"list", "create"} else set()
    if action in MUTATIONS:
        required.add("request_id")
    if action == "update" and "content" in arguments:
        # Replacing the whole page must not silently discard a peer's newer revision.
        required.add("expected_revision")
    if action == "create":
        required.update({"title", "content"})
    if action == "restore":
        required.add("revision")
    for field in required - arguments.keys():
        raise SwarmStoreError("invalid_arguments", field=field)
    for field, value in arguments.items():
        if field in {"revision", "expected_revision", "offset", "limit"}:
            minimum = 0 if field == "offset" else 1
            maximum = (
                20000
                if field == "limit" and action == "read"
                else 100
                if field == "limit"
                else 2**53 - 1
            )
            if type(value) is not int or not minimum <= value <= maximum:
                raise SwarmStoreError("invalid_arguments", field=field)
        elif field == "include_deleted":
            if type(value) is not bool:
                raise SwarmStoreError("invalid_arguments", field=field)
        elif (
            not isinstance(value, str)
            or (field not in {"content", "old_text", "new_text"} and not value.strip())
            or (field == "old_text" and not value)
            or len(value)
            > (
                200000
                if field in {"content", "old_text", "new_text"}
                else 240
                if field == "title"
                else 4096
            )
        ):
            raise SwarmStoreError("invalid_arguments", field=field)
    if action in MUTATIONS:
        _request_id(arguments["request_id"])
    if action == "update":
        if not {"title", "content", "old_text"}.intersection(arguments):
            raise SwarmStoreError("invalid_arguments", field="content")
        if ("old_text" in arguments) != ("new_text" in arguments) or (
            "old_text" in arguments and "content" in arguments
        ):
            raise SwarmStoreError("invalid_arguments", field="old_text")
    return action


def _page(
    connection: sqlite3.Connection, swarm_id: str, page_id: str, revision: int | None = None
) -> sqlite3.Row:
    page = connection.execute(
        f"SELECT {WIKI_PAGE_COLUMNS} FROM wiki_pages WHERE id=? AND swarm_id=?",
        (page_id, swarm_id),
    ).fetchone()
    if page is None:
        raise SwarmStoreError("wiki_page_not_found")
    row = connection.execute(
        f"SELECT {WIKI_REVISION_COLUMNS} FROM wiki_revisions WHERE page_id=? AND revision=?",
        (page_id, page["revision"] if revision is None else revision),
    ).fetchone()
    if row is None:
        raise SwarmStoreError("wiki_revision_not_found")
    return cast(sqlite3.Row, row)


def _metadata(row: sqlite3.Row) -> Json:
    label = " ".join(row["title"].split()).replace("[", "").replace("]", "")
    return {
        "page_id": row["page_id"],
        "title": row["title"],
        "revision": row["revision"],
        "deleted": bool(row["deleted"]),
        "updated_at": row["created_at"],
        "author": {"id": row["author_id"], "name": row["author_name"], "kind": row["author_kind"]},
        "link": f"[{label}](#wiki/{row['page_id']})",
    }


def _read(row: sqlite3.Row, arguments: Json, current_revision: int) -> Json:
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 12000)
    content = row["content"]
    if offset > len(content):
        raise SwarmStoreError("invalid_arguments", field="offset")
    result = {
        **_metadata(row),
        "current_revision": current_revision,
        "content": content[offset : offset + limit],
        "offset": offset,
        "total_chars": len(content),
    }
    if offset + limit < len(content):
        result["next_call"] = {
            "tool": "swarm_wiki",
            "arguments": {**arguments, "revision": row["revision"], "offset": offset + limit},
        }
    return result


def wiki_pages(db: SwarmDatabase, swarm_id: str) -> list[Json]:
    """Return every page's ID, current title and deletion state, newest change first."""

    with db._read() as connection:
        rows = connection.execute(
            "SELECT p.id,r.title,r.deleted FROM wiki_pages p JOIN wiki_revisions r "
            "ON r.page_id=p.id AND r.revision=p.revision WHERE p.swarm_id=? ORDER BY r.id DESC",
            (swarm_id,),
        ).fetchall()
    return [
        {"page_id": row["id"], "title": row["title"], "deleted": bool(row["deleted"])}
        for row in rows
    ]


def wiki_contents(
    db: SwarmDatabase, swarm_id: str, page_id: str, revisions: list[int]
) -> dict[int, str]:
    """Return the complete content of the named revisions that exist."""

    with db._read() as connection:
        rows = connection.execute(
            "SELECT revision,content FROM wiki_revisions WHERE swarm_id=? AND page_id=? "
            f"AND revision IN ({','.join('?' * len(revisions))})",
            (swarm_id, page_id, *revisions),
        ).fetchall()
    return {int(row["revision"]): str(row["content"]) for row in rows}


def wiki(
    db: SwarmDatabase,
    swarm_id: str,
    actor_id: str | None,
    arguments: Json,
    expected_epoch: int | None,
) -> Json:
    action = _validate(arguments)

    def operation(connection: sqlite3.Connection) -> Json:
        swarm = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if swarm is None:
            raise SwarmStoreError("swarm_not_found")
        actor = _participant(connection, swarm_id, actor_id) if actor_id is not None else None
        if actor is not None and expected_epoch is not None:
            _assert_epoch(connection, swarm_id, expected_epoch)
        if action in MUTATIONS:
            if actor is not None:
                _assert_mutable(connection, swarm_id)
            elif swarm["state"] in {"stopping", "deleting"}:
                raise SwarmStoreError("swarm_closed")
            return _mutate(connection, swarm_id, actor, arguments)
        if action == "read":
            current = _page(connection, swarm_id, arguments["page_id"])
            row = _page(connection, swarm_id, arguments["page_id"], arguments.get("revision"))
            return _read(row, arguments, current["revision"])
        if action == "history":
            _page(connection, swarm_id, arguments["page_id"])
        high = connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM wiki_revisions WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
        scope = _dump(
            {
                "swarm_id": swarm_id,
                **{key: value for key, value in arguments.items() if key != "cursor"},
            }
        )
        offset = 0
        if arguments.get("cursor"):
            offset, high = db._cursor(arguments["cursor"], "wiki", scope, high)
        limit = arguments.get("limit", 20)
        if action == "history":
            rows = connection.execute(
                f"SELECT {WIKI_REVISION_COLUMNS} FROM wiki_revisions "
                "WHERE swarm_id=? AND page_id=? AND id<=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (swarm_id, arguments["page_id"], high, limit + 1, offset),
            ).fetchall()
        else:
            # Unicode case folding happens in Python: SQLite's lower() folds
            # ASCII only, and the read connections carry no custom functions.
            query = arguments.get("query", "").casefold()
            latest = connection.execute(
                f"SELECT {ALIASED_WIKI_REVISION_COLUMNS} FROM wiki_revisions r "
                "JOIN (SELECT page_id,MAX(id) AS latest FROM wiki_revisions "
                "WHERE swarm_id=? AND id<=? GROUP BY page_id) p ON r.id=p.latest "
                "WHERE (? OR r.deleted=0) ORDER BY r.id DESC",
                (swarm_id, high, arguments.get("include_deleted", False)),
            )
            matches = (
                row
                for row in latest
                if query in row["title"].casefold() or query in row["content"].casefold()
            )
            rows = list(islice(matches, offset, offset + limit + 1))
        entries = []
        for row in rows[:limit]:
            value = _metadata(row)
            if action == "list":
                query = arguments.get("query", "").casefold()
                start = max(0, row["content"].casefold().find(query) - 80) if query else 0
                value["excerpt"] = row["content"][start : start + 320]
            entries.append(value)
        result: Json = {"entries": entries, "has_more": len(rows) > limit}
        if result["has_more"]:
            result["next_call"] = {
                "tool": "swarm_wiki",
                "arguments": {
                    **arguments,
                    "cursor": db._make_cursor("wiki", scope, high, offset + limit),
                },
            }
        return result

    if action in MUTATIONS:
        return cast(Json, db._write(operation))
    with db._read() as connection:
        return operation(connection)


def _mutate(
    connection: sqlite3.Connection, swarm_id: str, actor: sqlite3.Row | None, arguments: Json
) -> Json:
    action = arguments["action"]
    author_id = actor["id"] if actor is not None else "user"
    scope = f"wiki:{swarm_id}:{author_id}"
    payload_hash = _hash(arguments)
    replay = connection.execute(
        "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
        (scope, arguments["request_id"]),
    ).fetchone()
    if replay is not None:
        if replay["payload_hash"] != payload_hash:
            raise SwarmStoreError("request_conflict")
        return {**_load(replay["outcome"]), "replayed": True}

    def finish(result: Json) -> Json:
        connection.execute(
            "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
            (scope, arguments["request_id"], payload_hash, _dump(result)),
        )
        return result

    now = utc_now_timestamp()
    line = None
    notes: list[str] = []
    if action == "create":
        title, content, deleted = arguments["title"], arguments["content"], 0
        duplicate = connection.execute(
            "SELECT p.id FROM wiki_pages p JOIN wiki_revisions r "
            "ON r.page_id=p.id AND r.revision=p.revision "
            "WHERE p.swarm_id=? AND r.deleted=0 AND r.title=? AND r.content=? LIMIT 1",
            (swarm_id, title, content),
        ).fetchone()
        if duplicate is not None:
            # A repeated create from a later Tool Call must not fork the page.
            return finish(
                {**_metadata(_page(connection, swarm_id, duplicate["id"])), "unchanged": True}
            )
        while True:
            page_id = new_id("wpg")
            if (
                connection.execute("SELECT 1 FROM wiki_pages WHERE id=?", (page_id,)).fetchone()
                is None
            ):
                break
        revision = 1
        connection.execute(
            "INSERT INTO wiki_pages(id,swarm_id,revision) VALUES(?,?,?)",
            (page_id, swarm_id, revision),
        )
    else:
        page_id = arguments["page_id"]
        current = _page(connection, swarm_id, page_id)
        expected = arguments.get("expected_revision")
        # A passage edit from an older revision applies while its passage still matches.
        rebase = (
            action == "update"
            and "old_text" in arguments
            and "title" not in arguments
            and expected is not None
            and expected < current["revision"]
        )
        source = (
            _page(connection, swarm_id, page_id, arguments["revision"])
            if action == "restore"
            else current
        )
        title = arguments.get("title", source["title"])
        content = arguments.get("content", source["content"])
        deleted = int(action == "delete")
        state = {"current_revision": current["revision"]}
        if "old_text" not in arguments and (title, content, deleted) == (
            current["title"],
            current["content"],
            current["deleted"],
        ):
            return finish({**_metadata(current), "unchanged": True})
        if expected is not None and expected != current["revision"] and not rebase:
            raise SwarmStoreError(
                "wiki_revision_conflict", details={**state, "expected_revision": expected}
            )
        if current["deleted"] and action != "restore":
            raise SwarmStoreError("wiki_deleted", details=state)
        if "old_text" in arguments:
            edit = apply_text_edit(content, arguments["old_text"], arguments["new_text"])
            if isinstance(edit, EditMiss):
                raise SwarmStoreError(
                    "wiki_revision_conflict" if rebase else "wiki_edit_conflict",
                    details={
                        **state,
                        "expected_revision": expected,
                        "occurrences": edit.occurrences,
                        "similar": edit.similar,
                        "lines": list(edit.lines),
                        "passages": [
                            {"line": item.line, "text": item.text, "truncated": item.truncated}
                            for item in edit.passages
                        ],
                    },
                )
            content, line, notes = edit.content, edit.line, list(edit.notes)
            if content == current["content"] and title == current["title"]:
                return finish({**_metadata(current), "unchanged": True, "line": line})
        if len(content) > 200000:
            raise SwarmStoreError("invalid_arguments", field="content")
        revision = current["revision"] + 1
        connection.execute("UPDATE wiki_pages SET revision=? WHERE id=?", (revision, page_id))
    connection.execute(
        "INSERT INTO "
        "wiki_revisions(swarm_id,page_id,revision,title,content,deleted,author_id,author_name,author_kind,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            swarm_id,
            page_id,
            revision,
            title,
            content,
            deleted,
            author_id,
            actor["display_name"] if actor is not None else "User",
            "participant" if actor is not None else "user",
            now,
        ),
    )
    result = _metadata(_page(connection, swarm_id, page_id))
    if line is not None:
        result["line"] = line
    if notes:
        result["notes"] = notes
    return finish(result)
