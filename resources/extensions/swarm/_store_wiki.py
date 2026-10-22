"""Swarm-owned Wiki transactions and bounded, revision-stable reads."""

from __future__ import annotations

import re
import sqlite3
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from difflib import SequenceMatcher
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
# A passage edit's evidence shows where its text meets the page and this many lines
# after it, within this many characters; a longer line keeps this many characters on
# each side of the change.
_SEAM_AFTER_LINES = 2
_SEAM_CHARS = 4000
_SEAM_LINE_CHARS = 400
_SEAM_CUT_CHARS = 150
# A restore's evidence shows its changed passage and the lines after it, within this
# many characters; a restore that changed nothing shows the page's start.
_EXCERPT_CHARS = 4000
_EXCERPT_AFTER_LINES = 2
_EXCERPT_AFTER_CHARS = 600
_EXCERPT_START_CHARS = 2000
_EXCERPT_START_LINES = 8
_TEXT_CHARS = 200000
_LINE_BREAK = re.compile(r"\r\n|\n|\r")
# A line with its line break, if any.
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+\Z")
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
                start = min(found) if found else 0
                if found and len(content) != len(row["content"]):
                    # Case folding can expand a character (for example, sharp s to ss).
                    # The excerpt indexes the original content, not that folded copy.
                    folded_offset = 0
                    for original_offset, character in enumerate(row["content"]):
                        folded_offset += len(character.casefold())
                        if folded_offset > start:
                            start = original_offset
                            break
                start = max(0, start - 80)
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

    def finish(
        result: Json, *, changed: tuple[int, int] | None = None, passages: list[Json] | None = None
    ) -> Json:
        # Evidence comes from the exact saved revision and stays in the receipt. Text the
        # caller wrote is evidence only where the page differs from it, so a create or
        # whole-content update reports the saved size, and a passage edit shows where
        # its text meets the page.
        if result["deleted"]:
            pass
        elif action == "restore":
            saved = _page(connection, swarm_id, result["page_id"], result["revision"])
            result["content_excerpt"] = _content_excerpt(saved["content"], 1, changed)
        elif result.get("unchanged"):
            pass
        elif passages:
            saved = _page(connection, swarm_id, result["page_id"], result["revision"])
            seam = _seam(saved["content"], passages)
            if seam is not None:
                result["seam"] = seam
        elif action == "create" or "content" in arguments:
            saved = _page(connection, swarm_id, result["page_id"], result["revision"])
            result["size"] = {
                "lines": _line_count(saved["content"]),
                "chars": len(saved["content"]),
            }
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
    passages: list[Json] = []
    replacement_author_id = author_id
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
        # A title or passage change cannot make inherited content the caller's
        # own snapshot. Only a whole submitted body grants unbased replacement;
        # a peer's content edit revokes it until another based whole-body write.
        if (
            "content" in arguments
            and expected is None
            and (
                actor is None
                or current["author_id"] != author_id
                or current["replacement_author_id"] != author_id
            )
        ):
            raise SwarmStoreError("invalid_arguments", field="expected_revision", details=state)
        if "content" not in arguments:
            replacement_author_id = source["replacement_author_id"]
        if edits:
            content, outcomes, notes, misses, passages = _apply_edits(
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
            if content != current["content"] and replacement_author_id != author_id:
                replacement_author_id = None
        if len(content) > _TEXT_CHARS:
            raise SwarmStoreError("invalid_arguments", field="content")
        revision = current["revision"] + 1
        connection.execute("UPDATE wiki_pages SET revision=? WHERE id=?", (revision, page_id))
    connection.execute(
        "INSERT INTO "
        "wiki_revisions(swarm_id,page_id,revision,title,content,deleted,author_id,author_name,author_kind,created_at,replacement_author_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
            replacement_author_id,
        ),
    )
    result = metadata(_page(connection, swarm_id, page_id))
    if line is not None:
        result["line"] = line
    if "old_text" in arguments and passages:
        passage = passages[0]
        # The lines old_text covered in the revision before, and those its replacement covers now.
        result["replaced"] = {
            "old": list(_line_range(previous_content, passage["before"])),
            "new": list(_line_range(content, passage["after"], ends_line=passage["ends_line"])),
        }
    if notes:
        result["notes"] = notes
    if "edits" in arguments:
        result["edits"] = outcomes
    return finish(result, changed=_changed_span(previous_content, content), passages=passages)


def _passage_edits(arguments: Json) -> list[Json]:
    """Return an update's passage edits: its ``edits``, or its one old_text."""

    if "edits" in arguments:
        return list(arguments["edits"])
    if "old_text" in arguments:
        return [{"old_text": arguments["old_text"], "new_text": arguments["new_text"]}]
    return []


def _apply_edits(
    connection: sqlite3.Connection, page_id: str, current: int, content: str, edits: list[Json]
) -> tuple[str, list[Json], list[str], list[tuple[int, Json]], list[Json]]:
    """Apply passage edits in order, each to the content the earlier ones left.

    Return that content; for each edit that applied, its number, its line in
    that content and whether it changed the page; the notes on its passages;
    each miss with its edit's number; and, for each edit that changed the page,
    its passage: what it replaced (``before``, in the content it was applied to)
    and, in the returned content, the text it wrote (``after``) and the part of
    that text that differs from the replaced passage (``changed``), its new_text,
    and whether the replaced passage ended a line. A later edit that overlaps an
    earlier one's text joins it, which then has no new_text to compare.
    """

    outcomes: list[Json] = []
    notes: list[str] = []
    misses: list[tuple[int, Json]] = []
    passages: list[Json] = []
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
        if edit.before is not None and edit.after is not None:
            for passage in passages:
                joined = _overlaps(passage["after"], edit.before)
                passage["after"] = _moved(passage["after"], edit.before, edit.after)
                passage["changed"] = _moved(passage["changed"], edit.before, edit.after)
                if joined:
                    passage.update(new_text=None, between=False)
            start, end = edit.before
            old = _LINE.findall(content[start:end])
            new = _LINE.findall(edit.content[edit.after[0] : edit.after[1]])
            # Lines both share at the start and at the end are context, not change.
            same_start = _shared_start(old, new)
            same_end = _shared_start(old[same_start:][::-1], new[same_start:][::-1])
            point = edit.after[0] + sum(map(len, new[:same_start]))
            removed = old[same_start : len(old) - same_end]
            passages.append(
                {
                    "before": edit.before,
                    "after": edit.after,
                    "changed": (point, edit.after[1] - sum(map(len, new[len(new) - same_end :]))),
                    "new_text": item["new_text"],
                    "ends_line": content[end - 1] in "\r\n",
                    # The edit only removed whole lines, so no line of the page changed.
                    "between": len(new) == same_start + same_end
                    and bool(removed)
                    and removed[-1].endswith(("\n", "\r"))
                    and (point == 0 or edit.content[point - 1] in "\r\n"),
                }
            )
        content = edit.content
        notes.extend(edit.notes)
    return content, outcomes, notes, misses, passages


def _shared_start(first: Sequence[str], second: Sequence[str]) -> int:
    """Return how many leading items, such as characters or lines, two sequences share."""

    return next(
        (
            index
            for index, (one, other) in enumerate(zip(first, second, strict=False))
            if one != other
        ),
        min(len(first), len(second)),
    )


def _overlaps(span: tuple[int, int], replaced: tuple[int, int]) -> bool:
    """Whether replacing ``replaced`` changes text inside ``span``."""

    return replaced[0] < span[1] and replaced[1] > span[0]


def _moved(
    span: tuple[int, int], before: tuple[int, int], after: tuple[int, int]
) -> tuple[int, int]:
    """Return where text at ``span`` lies once ``before`` was replaced by the text at ``after``.

    A replacement that overlaps the span joins it: the span then covers both.
    """

    start, end = span
    shift = (after[1] - after[0]) - (before[1] - before[0])
    if before[1] <= start:
        return start + shift, end + shift
    if before[0] >= end:
        return span
    return min(start, after[0]), max(end + shift, after[1])


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

    start = _shared_start(before, after)
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


def _line_starts(content: str) -> list[int]:
    """Return where each line of ``content`` starts; after a final line break, its end."""

    return [0, *(match.end() for match in _LINE_BREAK.finditer(content))]


def _line_count(content: str) -> int:
    """Return how many lines ``content`` holds; a final line break ends the last one."""

    starts = _line_starts(content)
    return len(starts) - (starts[-1] == len(content))


def _line_range(content: str, span: tuple[int, int], *, ends_line: bool = False) -> tuple[int, int]:
    """Return the first and last line of ``content`` that ``span`` touches.

    An empty span left by removing whole lines (the removed text ended a line, and
    the span starts one) touches none: its last line comes before its first.
    """

    starts = _line_starts(content)
    start, end = span
    first = bisect_right(starts, start)
    if end > start:
        return first, bisect_right(starts, end - 1)
    if ends_line and start in starts:
        return first, first - 1
    return first, first


def _seam(content: str, passages: list[Json]) -> Json | None:
    """Show where each passage edit's text meets the saved page.

    For each edit: the line before its first changed line, its first and last
    changed lines, and the lines after it, where text the edit left in place
    would meet it (Swarm run 2026-10-13). Lines between that hold the edit's
    new_text as sent appear only as their range: echoing them cost every
    passage edit its own text again (Swarm run 2026-10-20). Lines the page
    holds other than as sent appear in full. Long lines keep the text around
    the change; the whole seam keeps at most ``_SEAM_CHARS`` characters and
    then names the character offset to read on from.
    """

    starts = _line_starts(content)
    lines = [line.rstrip("\r\n") for line in _LINE.findall(content)]
    total = len(lines)
    # Each shown line with the columns its excerpt must keep, and the lines as sent.
    shown: dict[int, set[int]] = {}
    sent: set[int] = set()

    def show(line: int, column: int) -> None:
        if 1 <= line <= total:
            shown.setdefault(line, set()).add(column)

    for passage in passages:
        start, end = passage["changed"]
        first = bisect_right(starts, start)
        if end > start:
            last = bisect_right(starts, end - 1)
        else:
            last = first - 1 if passage["between"] else first
        show(first - 1, len(lines[first - 2]) if first >= 2 else 0)
        if last >= first:
            show(first, start - starts[first - 1])
            show(last, end - starts[last - 1])
        held, differs = _held_lines(content, passage, starts)
        for line in range(first + 1, last):
            if line in held:
                sent.add(line)
            else:
                show(line, differs.get(line, 0))
        for line in range(last + 1, last + 1 + _SEAM_AFTER_LINES):
            show(line, 0)
    if not shown:
        return None
    sent -= shown.keys()
    low, high = min(shown.keys() | sent), max(shown.keys() | sent)
    parts: list[Json] = []
    seam: Json = {"first": low, "last": high, "lines": total, "parts": parts}
    size = 0
    line = low
    while line <= high:
        if line not in shown:
            # A run of lines as sent, or of lines no edit touched.
            kind = "sent" if line in sent else "unchanged"
            end = line
            while end < high and end + 1 not in shown and (end + 1 in sent) == (kind == "sent"):
                end += 1
            parts.append({"lines": [line, end], "kind": kind})
            line = end + 1
            continue
        part = {"line": line, **_clip(lines[line - 1], shown[line])}
        cost = len(part.get("text", "")) + sum(len(piece) for _, piece in part.get("pieces", ()))
        if parts and size + cost > _SEAM_CHARS:
            parts.append({"lines": [line, high], "kind": "cut"})
            seam["offset"] = starts[line - 1]
            break
        parts.append(part)
        size += cost + 1
        line += 1
    return seam


def _held_lines(content: str, passage: Json, starts: list[int]) -> tuple[set[int], dict[int, int]]:
    """Return the page lines of a passage's text that hold its new_text as sent.

    Also return, for each line that differs from the line of new_text it
    replaced, the column where it first differs.
    """

    new_text = passage["new_text"]
    if new_text is None:
        return set(), {}
    start, end = passage["after"]
    first = bisect_right(starts, start)
    written = _LINE_BREAK.split(content[start:end])
    sent = _LINE_BREAK.split(new_text)
    if written == sent:
        return {first + index for index in range(len(written))}, {}
    held: set[int] = set()
    differs: dict[int, int] = {}
    matcher = SequenceMatcher(None, sent, written, autojunk=False)
    for tag, sent_start, sent_end, written_start, written_end in matcher.get_opcodes():
        for index in range(written_start, written_end):
            other = sent_start + index - written_start
            if tag == "equal":
                held.add(first + index)
            elif tag == "replace" and other < sent_end:
                differs[first + index] = _shared_start(written[index], sent[other])
    return held, differs


def _clip(text: str, columns: set[int]) -> Json:
    """Return a page line, or for a long one the text around each column it must keep."""

    if len(text) <= _SEAM_LINE_CHARS:
        return {"text": text}
    width = 2 * _SEAM_CUT_CHARS
    windows: list[list[int]] = []
    for column in sorted(columns):
        start = max(0, min(column - _SEAM_CUT_CHARS, len(text) - width))
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], start + width)
        else:
            windows.append([start, start + width])
    return {"pieces": [[start, text[start:end]] for start, end in windows], "chars": len(text)}


def _excerpt(content: str, index: int, offset: int, end: int) -> Json:
    snippet = content[offset:end]
    return {
        "line": index + 1,
        "offset": offset,
        "content": snippet,
        "total_chars": len(content),
    }
