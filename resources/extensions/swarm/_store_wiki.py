"""Swarm-owned Wiki transactions and bounded, revision-stable reads."""

from __future__ import annotations

import re
import sqlite3
from bisect import bisect_right
from collections.abc import Iterator
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
from ._store_values import (
    Json,
    SwarmStoreError,
    _dump,
    _hash,
    _load,
    _request_id,
    wiki_number,
    wiki_ref,
)
from ._wiki_edit import TextEdit, apply_text_edit, held_in_place, holds_passage

MUTATIONS = {"create", "update", "delete", "restore"}
# A passage edit that finds nothing looks this many revisions back for one that held old_text.
_ORIGIN_REVISIONS = 20
# Passage edits one update applies together, each as old_text and new_text.
_MAX_EDITS = 100
# A change's evidence shows its whole changed passage and the lines after it, where
# leftover text would meet it, within this many characters. Create shows the page's
# start: the caller wrote all of it.
_EXCERPT_CHARS = 4000
_EXCERPT_AFTER_LINES = 2
_EXCERPT_AFTER_CHARS = 600
_EXCERPT_START_CHARS = 2000
_EXCERPT_START_LINES = 8
_TEXT_CHARS = 200000
_LINE_BREAK = re.compile(r"\r\n|\n|\r")
# Punctuation around a search word, as in a quoted phrase or a list of terms.
_WORD_EDGE = "\"'.,;:!?()[]{}<>“”„‘’«»"
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
        "edits",
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
        elif field == "edits":
            if (
                not isinstance(value, list)
                or not 1 <= len(value) <= _MAX_EDITS
                or not all(map(_valid_edit, value))
            ):
                raise SwarmStoreError("invalid_arguments", field=field)
        elif (
            not isinstance(value, str)
            or (field not in {"content", "old_text", "new_text"} and not value.strip())
            or (field == "old_text" and not value)
            or len(value)
            > (
                _TEXT_CHARS
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
        if not {"title", "content", "old_text", "edits"}.intersection(arguments):
            raise SwarmStoreError("invalid_arguments", field="content")
        if ("old_text" in arguments) != ("new_text" in arguments) or (
            "old_text" in arguments and "content" in arguments
        ):
            raise SwarmStoreError("invalid_arguments", field="old_text")
        if "edits" in arguments and {"old_text", "new_text", "content"} & arguments.keys():
            raise SwarmStoreError("invalid_arguments", field="edits")
    return action


def _valid_edit(item: object) -> bool:
    """Whether one of an update's ``edits`` is an old_text with its new_text."""

    return (
        isinstance(item, dict)
        and item.keys() == {"old_text", "new_text"}
        and all(isinstance(value, str) and len(value) <= _TEXT_CHARS for value in item.values())
        and bool(item["old_text"])
    )


def _page_numbers(connection: sqlite3.Connection, swarm_id: str) -> dict[str, int]:
    """Number the Swarm's pages in creation order: the first page created is "w1".

    A page is never removed on its own (delete saves a revision), and a new page's
    first revision gets a higher ID than every saved revision, so a page keeps its
    number and no number is reused.
    """

    rows = connection.execute(
        "SELECT page_id FROM wiki_revisions WHERE swarm_id=? GROUP BY page_id ORDER BY MIN(id)",
        (swarm_id,),
    ).fetchall()
    return {row["page_id"]: number for number, row in enumerate(rows, 1)}


def newest_wiki_page_number(db: SwarmDatabase, swarm_id: str) -> int | None:
    """Return the number of the Swarm's newest page, or None without pages."""

    with db._read() as connection:
        count = connection.execute(
            "SELECT COUNT(DISTINCT page_id) FROM wiki_revisions WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    # Numbers are dense in creation order (see _page_numbers).
    return int(count) or None


def _page_id(connection: sqlite3.Connection, swarm_id: str, reference: str) -> str:
    """Return the ID of the page ``reference`` numbers ("w3"); any other value unchanged."""

    number = wiki_number(reference)
    if number is None:
        return reference
    numbered = {value: page_id for page_id, value in _page_numbers(connection, swarm_id).items()}
    return numbered.get(number, reference)


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


def _metadata(row: sqlite3.Row, numbers: dict[str, int]) -> Json:
    label = " ".join(row["title"].split()).replace("[", "").replace("]", "")
    number = numbers[row["page_id"]]
    return {
        "page_id": row["page_id"],
        "number": number,
        "title": row["title"],
        "revision": row["revision"],
        "deleted": bool(row["deleted"]),
        "updated_at": row["created_at"],
        "author": {"id": row["author_id"], "name": row["author_name"], "kind": row["author_kind"]},
        "link": f"[{label}](#wiki/{wiki_ref(number)})",
    }


def _search(rows: list[sqlite3.Row], query: str) -> tuple[list[str], Iterator[sqlite3.Row]]:
    """Return the searched terms and the matching pages, lazily.

    A query no page contains as written finds the pages that contain all of
    its words, in title or content. More than one term means that fallback ran.
    """

    def containing(terms: list[str]) -> Iterator[sqlite3.Row]:
        # Unicode case folding happens in Python: SQLite's lower() folds ASCII
        # only, and the read connections carry no custom functions.
        for row in rows:
            title, content = row["title"].casefold(), row["content"].casefold()
            if all(term in title or term in content for term in terms):
                yield row

    phrase = [query.casefold()] if query else []
    words = query_words(query)
    if len(words) > 1 and next(containing(phrase), None) is None:
        return words, containing(words)
    return phrase, containing(phrase)


def query_words(query: str) -> list[str]:
    """Return a query's distinct casefolded words without edge punctuation.

    A query no text holds as written matches by these words instead.
    """

    words = [word.strip(_WORD_EDGE) for word in query.casefold().split()]
    return list(dict.fromkeys(filter(None, words)))


def _read(
    row: sqlite3.Row, arguments: Json, current_revision: int, numbers: dict[str, int]
) -> Json:
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 20000)
    content = row["content"]
    if offset > len(content):
        raise SwarmStoreError("invalid_arguments", field="offset")
    result = {
        **_metadata(row, numbers),
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


def wiki_pages(db: SwarmDatabase, swarm_id: str, containing: str | None = None) -> list[Json]:
    """Return every page's ID, number, current title and deletion state, newest change first.

    With ``containing``, return only the live pages whose current content contains
    that exact text.
    """

    with db._read() as connection:
        rows = connection.execute(
            "SELECT p.id,r.title,r.deleted FROM wiki_pages p JOIN wiki_revisions r "
            "ON r.page_id=p.id AND r.revision=p.revision WHERE p.swarm_id=? "
            "AND (? IS NULL OR (r.deleted=0 AND instr(r.content,?)>0)) ORDER BY r.id DESC",
            (swarm_id, containing, containing),
        ).fetchall()
        numbers = _page_numbers(connection, swarm_id)
    return [
        {
            "page_id": row["id"],
            "number": numbers[row["id"]],
            "title": row["title"],
            "deleted": bool(row["deleted"]),
        }
        for row in rows
    ]


def wiki_contents(
    db: SwarmDatabase, swarm_id: str, page_id: str, revisions: list[int]
) -> dict[int, str]:
    """Return the complete content of the named revisions that exist."""

    with db._read() as connection:
        page_id = _page_id(connection, swarm_id, page_id)
        rows = connection.execute(
            "SELECT revision,content FROM wiki_revisions WHERE swarm_id=? AND page_id=? "
            f"AND revision IN ({','.join('?' * len(revisions))})",
            (swarm_id, page_id, *revisions),
        ).fetchall()
    return {int(row["revision"]): str(row["content"]) for row in rows}


def wiki_changes(
    db: SwarmDatabase, swarm_id: str, page_id: str, since: int, until: int
) -> Json | None:
    """Return what changed in one page from revision ``since`` to revision ``until``.

    The result holds both revisions' content and the names of everyone who saved
    a revision after ``since`` up to ``until``, most recent first. None means one
    of the two revisions does not exist.
    """

    with db._read() as connection:
        page_id = _page_id(connection, swarm_id, page_id)
        contents = {
            int(row["revision"]): str(row["content"])
            for row in connection.execute(
                "SELECT revision,content FROM wiki_revisions WHERE swarm_id=? AND page_id=? "
                "AND revision IN (?,?)",
                (swarm_id, page_id, since, until),
            )
        }
        authors = connection.execute(
            "SELECT author_name,MAX(revision) AS latest FROM wiki_revisions "
            "WHERE swarm_id=? AND page_id=? AND revision>? AND revision<=? "
            "GROUP BY author_id,author_name ORDER BY latest DESC",
            (swarm_id, page_id, since, until),
        ).fetchall()
    if since not in contents or until not in contents:
        return None
    return {
        "before": contents[since],
        "after": contents[until],
        "authors": [str(row["author_name"]) for row in authors],
    }


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
        # A page number names its page exactly. The operation uses the page's ID, while
        # a continuation repeats the call as it was made.
        call = (
            {**arguments, "page_id": _page_id(connection, swarm_id, arguments["page_id"])}
            if "page_id" in arguments
            else arguments
        )
        if action in MUTATIONS:
            if actor is not None:
                _assert_mutable(connection, swarm_id)
            elif swarm["state"] in {"stopping", "deleting"}:
                raise SwarmStoreError("swarm_closed")
            return _mutate(connection, swarm_id, actor, call)
        numbers = _page_numbers(connection, swarm_id)
        if action == "read":
            current = _page(connection, swarm_id, call["page_id"])
            row = _page(connection, swarm_id, call["page_id"], call.get("revision"))
            return _read(row, arguments, current["revision"], numbers)
        if action == "history":
            _page(connection, swarm_id, call["page_id"])
        high = connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM wiki_revisions WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
        scope = _dump(
            {
                "swarm_id": swarm_id,
                **{key: value for key, value in call.items() if key != "cursor"},
            }
        )
        offset = 0
        if call.get("cursor"):
            offset, high = db._cursor(call["cursor"], "wiki", scope, high)
        limit = call.get("limit", 20)
        if action == "history":
            rows = connection.execute(
                f"SELECT {WIKI_REVISION_COLUMNS} FROM wiki_revisions "
                "WHERE swarm_id=? AND page_id=? AND id<=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (swarm_id, call["page_id"], high, limit + 1, offset),
            ).fetchall()
        else:
            latest = connection.execute(
                f"SELECT {ALIASED_WIKI_REVISION_COLUMNS} FROM wiki_revisions r "
                "JOIN (SELECT page_id,MAX(id) AS latest FROM wiki_revisions "
                "WHERE swarm_id=? AND id<=? GROUP BY page_id) p ON r.id=p.latest "
                "WHERE (? OR r.deleted=0) ORDER BY r.id DESC",
                (swarm_id, high, arguments.get("include_deleted", False)),
            ).fetchall()
            terms, matches = _search(latest, arguments.get("query", ""))
            rows = list(islice(matches, offset, offset + limit + 1))
        entries = []
        for row in rows[:limit]:
            value = _metadata(row, numbers)
            if action == "list":
                content = row["content"].casefold()
                found = [index for index in map(content.find, terms) if index >= 0]
                start = max(0, min(found) - 80) if found else 0
                value["excerpt"] = row["content"][start : start + 320]
            entries.append(value)
        result: Json = {"entries": entries, "has_more": len(rows) > limit}
        if action == "list" and len(terms) > 1:
            result["matched"] = "words"
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
        outcome = _load(replay["outcome"])
        # Outcomes saved before pages had numbers carry only the page ID.
        outcome.setdefault("number", _page_numbers(connection, swarm_id)[outcome["page_id"]])
        return {**outcome, "replayed": True}

    def metadata(row: sqlite3.Row) -> Json:
        return _metadata(row, _page_numbers(connection, swarm_id))

    def finish(result: Json, *, changed: tuple[int, int] | None = None) -> Json:
        if not result["deleted"] and (
            action in {"create", "restore"} or {"old_text", "edits", "content"} & arguments.keys()
        ):
            saved = _page(connection, swarm_id, result["page_id"], result["revision"])
            result["content_excerpt"] = _content_excerpt(
                saved["content"],
                result.get("line", 1),
                None if action == "create" else changed,
            )
        connection.execute(
            "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
            (scope, arguments["request_id"], payload_hash, _dump(result)),
        )
        return result

    now = utc_now_timestamp()
    line = None
    previous_content = ""
    notes: list[str] = []
    outcomes: list[Json] = []
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
                {**metadata(_page(connection, swarm_id, duplicate["id"])), "unchanged": True}
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
        previous_content = current["content"]
        expected = arguments.get("expected_revision")
        edits = _passage_edits(arguments)
        # A passage edit from an older revision applies while its passage still matches.
        rebase = (
            action == "update"
            and bool(edits)
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
        if not edits and (title, content, deleted) == (
            current["title"],
            current["content"],
            current["deleted"],
        ):
            return finish({**metadata(current), "unchanged": True})
        if expected is not None and expected != current["revision"] and not rebase:
            raise SwarmStoreError(
                "wiki_revision_conflict", details={**state, "expected_revision": expected}
            )
        if current["deleted"] and action != "restore":
            raise SwarmStoreError("wiki_deleted", details=state)
        # Replacing the whole page must not silently discard a peer's revision. A
        # participant replacing a revision it saved itself discards no one else's change.
        if (
            "content" in arguments
            and expected is None
            and (actor is None or current["author_id"] != actor["id"])
        ):
            raise SwarmStoreError("invalid_arguments", field="expected_revision", details=state)
        if edits:
            content, outcomes, notes, misses = _apply_edits(
                connection, page_id, current["revision"], content, edits
            )
            if misses:
                base = {**state, "expected_revision": expected}
                raise SwarmStoreError(
                    "wiki_revision_conflict" if rebase else "wiki_edit_conflict",
                    # Each miss of several edits names its edit's number.
                    details={
                        **base,
                        "edits": [{"edit": number, **miss} for number, miss in misses],
                        "total": len(edits),
                    }
                    if "edits" in arguments
                    else {**base, **misses[0][1]},
                )
            line = outcomes[0]["line"]
            if content == current["content"] and title == current["title"]:
                return finish({**metadata(current), "unchanged": True, "line": line})
        if len(content) > _TEXT_CHARS:
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
    result = metadata(_page(connection, swarm_id, page_id))
    if line is not None:
        result["line"] = line
    if notes:
        result["notes"] = notes
    if "edits" in arguments:
        result["edits"] = outcomes
    return finish(result, changed=_changed_span(previous_content, content))


def _passage_edits(arguments: Json) -> list[Json]:
    """Return an update's passage edits: its ``edits``, or its one old_text."""

    if "edits" in arguments:
        return list(arguments["edits"])
    if "old_text" in arguments:
        return [{"old_text": arguments["old_text"], "new_text": arguments["new_text"]}]
    return []


def _apply_edits(
    connection: sqlite3.Connection, page_id: str, current: int, content: str, edits: list[Json]
) -> tuple[str, list[Json], list[str], list[tuple[int, Json]]]:
    """Apply passage edits in order, each to the content the earlier ones left.

    Return that content; for each edit that applied, its number, its line in
    that content and whether it changed the page; the notes on its passages;
    and each miss with its edit's number.
    """

    outcomes: list[Json] = []
    notes: list[str] = []
    misses: list[tuple[int, Json]] = []
    for number, item in enumerate(edits, 1):
        edit = _edit(connection, page_id, current, content, item["old_text"], item["new_text"])
        if not isinstance(edit, TextEdit):
            misses.append((number, edit))
            continue
        # An edit that adds or removes lines moves the lines after it.
        shift = len(_LINE_BREAK.findall(edit.content)) - len(_LINE_BREAK.findall(content))
        for outcome in outcomes:
            if outcome["line"] > edit.line:
                outcome["line"] += shift
        outcomes.append({"edit": number, "line": edit.line, "applied": edit.applied})
        content = edit.content
        notes.extend(edit.notes)
    return content, outcomes, notes, misses


def _edit(
    connection: sqlite3.Connection, page_id: str, current: int, content: str, old: str, new: str
) -> TextEdit | Json:
    """Apply one passage edit to ``content``, or return why it found no single passage.

    When old_text occurs nowhere, the newest of the last revisions that held it
    explains the miss: the next revision changed that passage. If ``content``
    already holds this edit where that revision had old_text, a peer made it,
    and the edit applies as already held.
    """

    edit = apply_text_edit(content, old, new)
    if isinstance(edit, TextEdit):
        return edit
    details: Json = {
        "occurrences": edit.occurrences,
        "similar": edit.similar,
        "lines": list(edit.lines),
        "passages": [
            {"line": item.line, "text": item.text, "truncated": item.truncated}
            for item in edit.passages
        ],
        "differences": list(edit.differences),
        "different_lines": edit.different_lines,
        "span": list(edit.span) if edit.span else None,
    }
    origin = None if edit.occurrences else _origin(connection, page_id, old, current)
    if origin is not None:
        revision, before, author = origin
        line = held_in_place(before, content, old, new)
        if line is not None:
            return TextEdit(content, line, False)
        details["origin"] = {"revision": revision, "author": author}
    return details


def _origin(
    connection: sqlite3.Connection, page_id: str, old: str, current: int
) -> tuple[int, str, str] | None:
    """Return the newest recent revision that holds ``old``, its content, and who
    saved the next revision, which changed that passage."""

    if not old.strip():
        return None
    rows = connection.execute(
        "SELECT revision,content FROM wiki_revisions WHERE page_id=? AND revision>=? "
        "AND revision<? ORDER BY revision DESC",
        (page_id, max(1, current - _ORIGIN_REVISIONS), current),
    ).fetchall()
    held = next((row for row in rows if holds_passage(row["content"], old)), None)
    if held is None:
        return None
    author = connection.execute(
        "SELECT author_name FROM wiki_revisions WHERE page_id=? AND revision=?",
        (page_id, held["revision"] + 1),
    ).fetchone()
    return int(held["revision"]), str(held["content"]), str(author["author_name"])


def _changed_span(before: str, after: str) -> tuple[int, int]:
    """Return where ``after`` differs from ``before``: its start and its end in ``after``.

    Text both share at the start and at the end lies outside; a pure deletion
    yields an empty span at the place of the deleted text.
    """

    start = next(
        (index for index, (old, new) in enumerate(zip(before, after, strict=False)) if old != new),
        min(len(before), len(after)),
    )
    shared = 0
    room = min(len(before), len(after)) - start
    while shared < room and before[-1 - shared] == after[-1 - shared]:
        shared += 1
    return start, len(after) - shared


def _content_excerpt(content: str, line: int, changed: tuple[int, int] | None) -> Json:
    """Bounded evidence from the exact saved revision, retained in its replay receipt.

    For a change, it holds the whole changed passage and the lines after it, so a
    seam with text the change left in place shows: one update replaced eight table
    rows with ten, and the two old rows it left below went unnoticed while the
    excerpt ended after eight lines (Swarm run 2026-10-13).
    """

    starts = [0, *(match.end() for match in _LINE_BREAK.finditer(content))]
    if changed is None:
        index = min(max(0, line - 1), len(starts) - 1)
        offset = starts[index]
        end = min(len(content), offset + _EXCERPT_START_CHARS)
        if index + _EXCERPT_START_LINES < len(starts):
            end = min(end, starts[index + _EXCERPT_START_LINES])
        return _excerpt(content, index, offset, end)
    anchor = min(changed[0], len(content))
    line_start = starts[bisect_right(starts, anchor) - 1]
    # A long line must show the change, not just its unchanged start. Keep a little
    # preceding context and exact page coordinates.
    offset = max(line_start, anchor - 400)
    if offset == len(content) and content:
        offset = max(0, offset - 400)
    index = bisect_right(starts, offset) - 1
    changed_end = max(anchor, min(changed[1], len(content)))
    last = bisect_right(starts, max(anchor, changed_end - 1)) - 1
    after = last + 1 + _EXCERPT_AFTER_LINES
    end = starts[after] if after < len(starts) else len(content)
    end = min(end, changed_end + _EXCERPT_AFTER_CHARS, offset + _EXCERPT_CHARS)
    return _excerpt(content, index, offset, end)


def _excerpt(content: str, index: int, offset: int, end: int) -> Json:
    snippet = content[offset:end]
    return {
        "line": index + 1,
        "offset": offset,
        "content": snippet,
        "total_chars": len(content),
    }
