"""Versioned questions and independently replaceable participant positions."""

from __future__ import annotations

import sqlite3

from core.utils.ids import new_id

from ._store_database import SwarmDatabase
from ._store_records import _assert_epoch, _assert_mutable, _participant, _request_replay
from ._store_values import Json, SwarmStoreError, _dump, _hash, _load, _now, _request_id

READS = {"list", "read", "history"}
FIELDS = {
    "list": {"query", "include_archived", "cursor", "limit"},
    "read": {"question_id", "section", "cursor", "limit"},
    "history": {"question_id", "cursor", "limit"},
    "create": {"title", "text", "request_id"},
    "update": {"question_id", "title", "text", "archived", "expected_revision", "request_id"},
    "add_option": {"question_id", "title", "text", "expected_revision", "request_id"},
    "update_option": {
        "question_id",
        "option_id",
        "title",
        "text",
        "withdrawn",
        "expected_revision",
        "request_id",
    },
    "position": {
        "question_id",
        "option_id",
        "note",
        "position_revision",
        "expected_revision",
        "request_id",
    },
    "withdraw": {"question_id", "position_revision", "request_id"},
    "reconsider": {"question_id", "expected_revision", "request_id"},
}


def _validate(args: Json) -> str:
    action = args.get("action")
    if not isinstance(action, str) or action not in FIELDS:
        raise SwarmStoreError("invalid_arguments", field="action")
    for field in args.keys() - {"action", *FIELDS[action]}:
        raise SwarmStoreError("inapplicable_field", field=field)
    required = {"question_id"} if action not in {"list", "create"} else set()
    if action not in READS:
        required.add("request_id")
        if action not in {"create", "withdraw"}:
            required.add("expected_revision")
    if action in {"create", "add_option"}:
        required.add("title")
    if action in {"position", "withdraw"}:
        required.add("position_revision")
    if action == "update_option":
        required.add("option_id")
    for field in required - args.keys():
        raise SwarmStoreError("invalid_arguments", field=field)
    for field, value in args.items():
        if field in {"expected_revision", "position_revision", "limit"}:
            if type(value) is not int or not (
                0 if field == "position_revision" else 1
            ) <= value <= (100 if field == "limit" else 2**53 - 1):
                raise SwarmStoreError("invalid_arguments", field=field)
        elif field in {"archived", "withdrawn", "include_archived"}:
            if type(value) is not bool:
                raise SwarmStoreError("invalid_arguments", field=field)
        elif (
            not isinstance(value, str)
            or (field not in {"text", "note"} and not value.strip())
            or len(value) > (240 if field == "title" else 32000 if field == "cursor" else 4000)
        ):
            raise SwarmStoreError("invalid_arguments", field=field)
    if args.get("section", "options") not in {"options", "positions"}:
        raise SwarmStoreError("invalid_arguments", field="section")
    if action in {"update", "update_option"} and not {
        "title",
        "text",
        "archived",
        "withdrawn",
    }.intersection(args):
        raise SwarmStoreError("invalid_arguments", field="title")
    if action not in READS:
        _request_id(args["request_id"])
    return action


def _head(connection: sqlite3.Connection, sid: str, qid: str) -> Json:
    row = connection.execute(
        "SELECT document FROM decision_questions WHERE id=? AND swarm_id=?", (qid, sid)
    ).fetchone()
    if row is None:
        raise SwarmStoreError("decision_not_found")
    return _load(row["document"])


def _summary(question: Json) -> Json:
    label = question["title"].replace("[", "").replace("]", "")
    return {key: value for key, value in question.items() if key != "options"} | {
        "link": f"[{label}](#decision/{question['question_id']}/{question['revision']})"
    }


def _positions(connection: sqlite3.Connection, qid: str, high: int) -> list[Json]:
    rows = connection.execute(
        "SELECT e.document FROM decision_events e JOIN (SELECT actor_id, MAX(id) AS last "
        "FROM decision_events WHERE question_id=? AND id<=? AND kind IN ('position','withdraw') "
        "GROUP BY actor_id) p ON p.last=e.id ORDER BY e.id DESC",
        (qid, high),
    ).fetchall()
    return [_load(row[0])["position"] for row in rows]


def _position_view(position: Json, question: Json) -> Json:
    option = next(
        (item for item in question["options"] if item["option_id"] == position["option_id"]), None
    )
    return position | {
        "needs_review": not position["withdrawn"]
        and position["question_revision"] != question["revision"],
        "option_changed": option is not None and position["option_revision"] != option["revision"],
        "option_withdrawn": bool(option and option["withdrawn"]),
    }


def _read(
    db: SwarmDatabase, connection: sqlite3.Connection, sid: str, actor_id: str, args: Json
) -> Json:
    action = args["action"]
    high = connection.execute(
        "SELECT COALESCE(MAX(id),0) FROM decision_events WHERE swarm_id=?", (sid,)
    ).fetchone()[0]
    scope = _dump(
        {"swarm_id": sid, **{key: value for key, value in args.items() if key != "cursor"}}
    )
    offset = 0
    if args.get("cursor"):
        offset, high = db._cursor(args["cursor"], "decisions", scope, high)
    limit = args.get("limit", 20)
    if action == "list":
        rows = connection.execute(
            "SELECT e.document FROM decision_events e JOIN (SELECT question_id,MAX(id) AS last "
            "FROM decision_events WHERE swarm_id=? AND id<=? AND kind NOT IN "
            "('position','withdraw') "
            "GROUP BY question_id) q ON q.last=e.id WHERE (? OR "
            "json_extract(e.document,'$.question.archived')=0) "
            "AND (instr(casefold(json_extract(e.document,'$.question.title')),?)>0 OR "
            "instr(casefold(json_extract(e.document,'$.question.text')),?)>0) "
            "ORDER BY e.id DESC LIMIT ? OFFSET ?",
            (
                sid,
                high,
                args.get("include_archived", False),
                args.get("query", "").casefold(),
                args.get("query", "").casefold(),
                limit + 1,
                offset,
            ),
        ).fetchall()
        entries = [_summary(_load(row[0])["question"]) for row in rows]
        result: Json = {}
    else:
        current = _head(connection, sid, args["question_id"])
        qid = current["question_id"]
        row = connection.execute(
            (
                "SELECT document FROM decision_events WHERE question_id=? AND id<=? "
                "AND kind NOT IN ('position','withdraw') ORDER BY id DESC LIMIT 1"
            ),
            (qid, high),
        ).fetchone()
        question = _load(row[0])["question"]
        positions = _positions(connection, qid, high)
        own = next((item for item in positions if item["author"]["id"] == actor_id), None)
        result = {
            "question": _summary(question),
            "current_revision": current["revision"],
            "my_position": _position_view(own, question) if own else None,
            "position_revision": own["revision"] if own else 0,
        }
        if action == "history":
            rows = connection.execute(
                (
                    "SELECT id,kind,author,created_at,document FROM decision_events WHERE"
                    " question_id=? AND id<=? ORDER BY id DESC LIMIT ? OFFSET ?"
                ),
                (qid, high, limit + 1, offset),
            ).fetchall()
            entries = [
                {
                    "event_id": row["id"],
                    "action": row["kind"],
                    "author": _load(row["author"]),
                    "created_at": row["created_at"],
                    "change": _load(row["document"])["change"],
                }
                for row in rows
            ]
        elif args.get("section") == "positions":
            entries = [_position_view(item, question) for item in positions][
                offset : offset + limit + 1
            ]
        else:
            entries = []
            result["positions"] = [_position_view(item, question) for item in positions[:20]]
            if len(positions) > 20:
                follow = {"action": "read", "question_id": qid, "section": "positions"}
                follow_scope = _dump({"swarm_id": sid, **follow})
                result["positions_next_call"] = {
                    "tool": "swarm_decisions",
                    "arguments": {
                        **follow,
                        "cursor": db._make_cursor("decisions", follow_scope, high, 20),
                    },
                }
            for option in question["options"][offset : offset + limit + 1]:
                supporters = [
                    p
                    for p in positions
                    if not p["withdrawn"] and p["option_id"] == option["option_id"]
                ]
                entries.append(
                    option
                    | {
                        "support": len(supporters),
                        "reviewed_support": sum(
                            p["question_revision"] == question["revision"] for p in supporters
                        ),
                    }
                )
        participant_count = connection.execute(
            "SELECT COUNT(*) FROM participants WHERE swarm_id=?", (sid,)
        ).fetchone()[0]
        result["participation"] = {
            "not_positioned": participant_count
            - sum(not p["withdrawn"] and p["author"]["kind"] == "participant" for p in positions),
            "positions": sum(not p["withdrawn"] for p in positions),
            "needs_review": sum(
                not p["withdrawn"] and p["question_revision"] != question["revision"]
                for p in positions
            ),
            "participants": connection.execute(
                "SELECT COUNT(*) FROM participants WHERE swarm_id=?", (sid,)
            ).fetchone()[0],
        }
    result.update(entries=entries[:limit], has_more=len(entries) > limit)
    if result["has_more"]:
        result["next_call"] = {
            "tool": "swarm_decisions",
            "arguments": {
                **args,
                "cursor": db._make_cursor("decisions", scope, high, offset + limit),
            },
        }
    return result


def decisions(
    db: SwarmDatabase, sid: str, actor_id: str | None, args: Json, expected_epoch: int | None
) -> Json:
    action = _validate(args)

    def operation(connection: sqlite3.Connection) -> Json:
        swarm = connection.execute("SELECT state FROM swarms WHERE id=?", (sid,)).fetchone()
        if swarm is None:
            raise SwarmStoreError("swarm_not_found")
        actor = _participant(connection, sid, actor_id) if actor_id else None
        author = {
            "id": actor_id or "user",
            "name": actor["display_name"] if actor else "User",
            "kind": "participant" if actor else "user",
        }
        if actor and expected_epoch is not None:
            _assert_epoch(connection, sid, expected_epoch)
        if action in READS:
            return _read(db, connection, sid, author["id"], args)
        if actor:
            _assert_mutable(connection, sid)
        elif swarm["state"] in {"stopping", "deleting"}:
            raise SwarmStoreError("swarm_closed")
        scope = f"decisions:{sid}:{author['id']}"
        payload_hash = _hash(args)
        replay = _request_replay(connection, scope, args["request_id"], payload_hash)
        if replay is not None:
            return replay
        now = _now()
        if action == "create":
            qid = new_id("dec")
            while connection.execute(
                "SELECT 1 FROM decision_questions WHERE id=?", (qid,)
            ).fetchone():
                qid = new_id("dec")
            question = {
                "question_id": qid,
                "revision": 1,
                "title": args["title"],
                "text": args.get("text", ""),
                "archived": False,
                "options": [],
                "updated_at": now,
            }
            connection.execute(
                "INSERT INTO decision_questions VALUES(?,?,?)", (qid, sid, _dump(question))
            )
        else:
            question = _head(connection, sid, args["question_id"])
            qid = question["question_id"]
            if action != "withdraw" and args["expected_revision"] != question["revision"]:
                raise SwarmStoreError("decision_conflict")
            if question["archived"] and action not in {"update", "withdraw", "reconsider"}:
                raise SwarmStoreError("decision_archived")
        position = None
        if action in {"position", "withdraw"}:
            row = connection.execute(
                "SELECT document FROM decision_positions WHERE question_id=? AND actor_id=?",
                (qid, author["id"]),
            ).fetchone()
            previous = _load(row[0]) if row else None
            if args["position_revision"] != (previous["revision"] if previous else 0):
                raise SwarmStoreError("position_conflict")
            option = next(
                (o for o in question["options"] if o["option_id"] == args.get("option_id")), None
            )
            if "option_id" in args and option is None:
                raise SwarmStoreError("option_not_found")
            if option and option["withdrawn"]:
                raise SwarmStoreError("option_withdrawn")
            position = {
                "author": author,
                "revision": args["position_revision"] + 1,
                "question_revision": question["revision"],
                "option_id": args.get("option_id"),
                "option_revision": option["revision"] if option else None,
                "note": args.get("note", ""),
                "withdrawn": action == "withdraw",
                "updated_at": now,
            }
            connection.execute(
                (
                    "INSERT INTO decision_positions VALUES(?,?,?) ON "
                    "CONFLICT(question_id,actor_id) DO UPDATE SET "
                    "document=excluded.document"
                ),
                (qid, author["id"], _dump(position)),
            )
        elif action != "create":
            if action == "update":
                question.update(
                    {key: args[key] for key in ("title", "text", "archived") if key in args}
                )
            elif action == "reconsider":
                question["archived"] = False
            elif action == "add_option":
                if len(question["options"]) >= 50:
                    raise SwarmStoreError("decision_option_limit")
                oid = new_id("opt")
                while any(item["option_id"] == oid for item in question["options"]):
                    oid = new_id("opt")
                question["options"].append(
                    {
                        "option_id": oid,
                        "revision": 1,
                        "title": args["title"],
                        "text": args.get("text", ""),
                        "withdrawn": False,
                    }
                )
            elif action == "update_option":
                option = next(
                    (o for o in question["options"] if o["option_id"] == args["option_id"]), None
                )
                if option is None:
                    raise SwarmStoreError("option_not_found")
                option.update(
                    {key: args[key] for key in ("title", "text", "withdrawn") if key in args}
                )
                option["revision"] += 1
            question["revision"] += 1
            question["updated_at"] = now
            connection.execute(
                "UPDATE decision_questions SET document=? WHERE id=?", (_dump(question), qid)
            )
        event = {
            "change": {
                key: value for key, value in args.items() if key not in {"action", "request_id"}
            }
        }
        event["position" if position else "question"] = position if position else question
        connection.execute(
            (
                "INSERT INTO "
                "decision_events(swarm_id,question_id,kind,actor_id,author,created_at,document)"
                " VALUES(?,?,?,?,?,?,?)"
            ),
            (sid, qid, action, author["id"], _dump(author), now, _dump(event)),
        )
        result = {"question": _summary(question)}
        if action == "add_option":
            result["option"] = question["options"][-1]
        if position:
            result["my_position"] = _position_view(position, question)
            result["position_revision"] = position["revision"]
        connection.execute(
            "INSERT INTO requests VALUES(?,?,?,?)",
            (scope, args["request_id"], payload_hash, _dump(result)),
        )
        return result

    return operation(db._require_connection()) if action in READS else db._write(operation)
