"""Store board."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import sqlite3

from core.utils.ids import new_id

from ._store_database import (
    SwarmDatabase,
)
from ._store_reads import (
    _post_page,
)
from ._store_records import (
    _assert_epoch,
    _assert_mutable,
    _discussion,
    _main,
    _participant,
    _pending_count,
)
from ._store_values import (
    Json,
    SwarmStoreError,
    _dump,
    _hash,
    _load,
    _now,
)
from .agent_text import DISCUSSION_ANNOUNCEMENT


def _post_message(
    db: SwarmDatabase,
    swarm_id: str,
    sender_id: str,
    text: str,
    request_id: str,
    discussion_id: str | None,
    reply_to: str | None,
    recipients: tuple[str, ...],
    author_kind: str,
    author_name: str | None,
    expected_epoch: int | None,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_mutable(connection, swarm_id)
        if expected_epoch is not None:
            _assert_epoch(connection, swarm_id, expected_epoch)
        sender = (
            _participant(connection, swarm_id, sender_id) if author_kind == "participant" else None
        )
        target = None
        if reply_to is not None:
            target = connection.execute(
                "SELECT discussion_id FROM posts WHERE id=? AND swarm_id=?",
                (reply_to, swarm_id),
            ).fetchone()
            if target is None:
                raise SwarmStoreError("message_not_found")
        discussion_id_value = discussion_id or (
            target["discussion_id"] if target is not None else _main(connection, swarm_id)
        )
        _discussion(connection, swarm_id, discussion_id_value)
        payload = {
            "discussion_id": discussion_id_value,
            "text": text,
            "reply_to": reply_to,
            "recipients": recipients,
            "author_kind": author_kind,
        }
        scope = f"post:{swarm_id}:{sender_id}"
        replay = connection.execute(
            "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
            (scope, request_id),
        ).fetchone()
        if replay is not None:
            if replay["payload_hash"] != _hash(payload):
                raise SwarmStoreError("request_conflict")
            outcome = _load(replay["outcome"])
            outcome["replayed"] = True
            return outcome
        if target is not None and target["discussion_id"] != discussion_id_value:
            raise SwarmStoreError("reply_discussion_mismatch")
        members = connection.execute(
            "SELECT participant_id FROM memberships WHERE discussion_id=?",
            (discussion_id_value,),
        ).fetchall()
        audience = {
            str(row["participant_id"]): "main"
            if discussion_id_value == _main(connection, swarm_id)
            else "discussion"
            for row in members
        }
        for recipient in recipients:
            if (
                connection.execute(
                    "SELECT 1 FROM participants WHERE swarm_id=? AND id=?",
                    (swarm_id, recipient),
                ).fetchone()
                is None
            ):
                raise SwarmStoreError("invalid_recipient")
            audience[recipient] = "ping"
        audience.pop(sender_id, None)
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM posts WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        post_id = new_id("pst")
        name = author_name or (sender["display_name"] if sender is not None else "User")
        connection.execute(
            "INSERT INTO posts(id,swarm_id,discussion_id,sequence,author_kind,author_id,author_name,text,reply_to,recipients_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                post_id,
                swarm_id,
                discussion_id_value,
                sequence,
                author_kind,
                sender_id,
                name,
                text,
                reply_to,
                _dump(list(recipients)),
                _now(),
            ),
        )
        for recipient, route in audience.items():
            connection.execute(
                "INSERT INTO recipients(post_id,participant_id,route_class) VALUES(?,?,?)",
                (post_id, recipient, route),
            )
        result = {
            "post_id": post_id,
            "sequence": sequence,
            "discussion_id": discussion_id_value,
            "routes": {
                route: sum(1 for value in audience.values() if value == route)
                for route in ("ping", "discussion", "main")
            },
        }
        connection.execute(
            "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
            (scope, request_id, _hash(payload), _dump(result)),
        )
        return result

    return db._write(operation)


def _create_discussion(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    title: str,
    text: str,
    request_id: str,
    recipients: tuple[str, ...],
    expected_epoch: int | None,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_mutable(connection, swarm_id)
        if expected_epoch is not None:
            _assert_epoch(connection, swarm_id, expected_epoch)
        _participant(connection, swarm_id, participant_id)
        payload = {"title": title, "text": text, "recipients": recipients}
        scope = f"create:{swarm_id}:{participant_id}"
        replay = connection.execute(
            "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
            (scope, request_id),
        ).fetchone()
        if replay is not None:
            if replay["payload_hash"] != _hash(payload):
                raise SwarmStoreError("request_conflict")
            value = _load(replay["outcome"])
            value["replayed"] = True
            return value
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM discussions WHERE swarm_id=?",
                (swarm_id,),
            ).fetchone()[0]
        )
        for recipient in recipients:
            if (
                connection.execute(
                    "SELECT 1 FROM participants WHERE swarm_id=? AND id=?",
                    (swarm_id, recipient),
                ).fetchone()
                is None
            ):
                raise SwarmStoreError("invalid_recipient")
        discussion_id = new_id("dsc")
        connection.execute(
            "INSERT INTO discussions(id,swarm_id,title,sequence,is_main,created_at) VALUES(?,?,?,?,0,?)",
            (discussion_id, swarm_id, title, sequence, _now()),
        )
        connection.execute(
            "INSERT INTO memberships(discussion_id,participant_id) VALUES(?,?)",
            (discussion_id, participant_id),
        )
        opening_id = _insert_post(
            db,
            connection,
            swarm_id,
            participant_id,
            discussion_id,
            text,
            None,
            recipients,
            "participant",
            None,
        )
        announcement_id = _insert_post(
            db,
            connection,
            swarm_id,
            participant_id,
            _main(connection, swarm_id),
            DISCUSSION_ANNOUNCEMENT.format(
                author_name=_participant(connection, swarm_id, participant_id)["display_name"],
                title=title,
                discussion_id=discussion_id,
                opening_post_id=opening_id,
            ),
            None,
            (),
            "participant",
            None,
        )
        result = {
            "discussion_id": discussion_id,
            "opening_post_id": opening_id,
            "main_announcement_id": announcement_id,
            "joined": True,
        }
        connection.execute(
            "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
            (scope, request_id, _hash(payload), _dump(result)),
        )
        return result

    return db._write(operation)


def _insert_post(
    db: SwarmDatabase,
    connection: sqlite3.Connection,
    swarm_id: str,
    sender_id: str,
    discussion_id: str,
    text: str,
    reply_to: str | None,
    recipients: tuple[str, ...],
    author_kind: str,
    author_name: str | None,
) -> str:
    sender = _participant(connection, swarm_id, sender_id)
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM posts WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    )
    post_id = new_id("pst")
    connection.execute(
        "INSERT INTO posts(id,swarm_id,discussion_id,sequence,author_kind,author_id,author_name,text,reply_to,recipients_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            post_id,
            swarm_id,
            discussion_id,
            sequence,
            author_kind,
            sender_id,
            author_name or sender["display_name"],
            text,
            reply_to,
            _dump(list(recipients)),
            _now(),
        ),
    )
    audience = {
        row["participant_id"]: (
            "main" if discussion_id == _main(connection, swarm_id) else "discussion"
        )
        for row in connection.execute(
            "SELECT participant_id FROM memberships WHERE discussion_id=?", (discussion_id,)
        )
    }
    audience.update(dict.fromkeys(recipients, "ping"))
    audience.pop(sender_id, None)
    for recipient, route in audience.items():
        connection.execute(
            "INSERT INTO recipients(post_id,participant_id,route_class) VALUES(?,?,?)",
            (post_id, recipient, route),
        )
    return post_id


def _membership(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    discussion_id: str,
    joining: bool,
    expected_epoch: int | None,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_mutable(connection, swarm_id)
        if expected_epoch is not None:
            _assert_epoch(connection, swarm_id, expected_epoch)
        _participant(connection, swarm_id, participant_id)
        discussion = _discussion(connection, swarm_id, discussion_id)
        if not joining and discussion["is_main"]:
            raise SwarmStoreError("main_membership_required")
        if joining:
            connection.execute(
                "INSERT OR IGNORE INTO memberships(discussion_id,participant_id) VALUES(?,?)",
                (discussion_id, participant_id),
            )
        else:
            connection.execute(
                "DELETE FROM memberships WHERE discussion_id=? AND participant_id=?",
                (discussion_id, participant_id),
            )
        if not joining:
            return {
                "discussion_id": discussion_id,
                "joined": False,
                "pending_count": _pending_count(connection, swarm_id, participant_id),
            }
        recent = _post_page(db, connection, swarm_id, participant_id, discussion_id, None, 20)
        return {
            "discussion_id": discussion_id,
            "joined": True,
            "recent": {
                "entries": list(recent.entries),
                "has_more": recent.has_more,
                "cursor": recent.cursor,
            },
        }

    return db._write(operation)
