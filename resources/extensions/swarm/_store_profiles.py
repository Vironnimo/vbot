"""Store profiles."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import re
import secrets
import sqlite3

from core.utils.ids import new_id

from ._participant_names import (
    _PARTICIPANT_NAMES,
)
from ._store_database import (
    SwarmDatabase,
)
from ._store_lifecycle import (
    _refresh_swarm_state,
)
from ._store_records import (
    _assert_mutable,
)
from ._store_values import (
    _DELIVERY_ROUTES,
    Json,
    Page,
    SwarmStoreError,
    _copy,
    _dump,
    _hash,
    _load,
    _now,
    _page,
)


def _save_profile(
    db: SwarmDatabase, profile: Json, expected_revision: int | None, automatic_slug: bool = False
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        existing = connection.execute(
            "SELECT revision, slug FROM profiles WHERE id = ?", (profile["id"],)
        ).fetchone()
        if existing is None:
            if expected_revision is not None:
                raise SwarmStoreError("revision_conflict")
            revision = 1
        else:
            if expected_revision != int(existing["revision"]):
                raise SwarmStoreError("revision_conflict")
            revision = int(existing["revision"]) + 1
        profile["revision"] = revision
        if automatic_slug:
            if existing is not None:
                profile["slug"] = existing["slug"]
            else:
                base = re.sub(r"[^a-z0-9]+", "-", profile["name"].lower()).strip("-") or "swarm"
                candidate, suffix = base, 2
                while connection.execute(
                    "SELECT 1 FROM profiles WHERE slug=?", (candidate,)
                ).fetchone():
                    candidate, suffix = f"{base}-{suffix}", suffix + 1
                profile["slug"] = candidate
        now = _now()
        try:
            connection.execute(
                "INSERT INTO profiles(id, slug, name, revision, payload, created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,name=excluded.name,revision=excluded.revision,payload=excluded.payload,updated_at=excluded.updated_at",
                (
                    profile["id"],
                    profile["slug"],
                    profile["name"],
                    revision,
                    _dump(profile),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise SwarmStoreError("slug_unavailable", field="slug") from error
        return _copy(profile)

    return db._write(operation)


def _get_profile(db: SwarmDatabase, profile_id: str) -> Json:
    row = (
        db._require_connection()
        .execute("SELECT payload FROM profiles WHERE id=?", (profile_id,))
        .fetchone()
    )
    if row is None:
        raise SwarmStoreError("profile_not_found")
    return _load(row["payload"])


def _list_profiles(db: SwarmDatabase, cursor: str | None, limit: int) -> Page:
    offset = db._cursor(cursor, "profiles", "all", None)[0] if cursor else 0
    rows = (
        db._require_connection()
        .execute("SELECT payload FROM profiles ORDER BY slug LIMIT ? OFFSET ?", (limit + 1, offset))
        .fetchall()
    )
    return _page(
        [_load(row["payload"]) for row in rows],
        limit,
        db._make_cursor("profiles", "all", None, offset + limit),
    )


def _delete_profile(db: SwarmDatabase, profile_id: str, expected_revision: int) -> None:
    def operation(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT revision FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
        if row is None:
            raise SwarmStoreError("profile_not_found")
        if int(row["revision"]) != expected_revision:
            raise SwarmStoreError("revision_conflict")
        connection.execute("DELETE FROM profiles WHERE id=?", (profile_id,))

    db._write(operation)


def _create_swarm(
    db: SwarmDatabase,
    profile_id: str,
    prompt: str,
    effective: Json,
    request_id: str,
    expected_profile_revision: int,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        payload_hash = _hash(
            {
                "profile_id": profile_id,
                "prompt": prompt,
                "effective": effective,
                "expected_profile_revision": expected_profile_revision,
            }
        )
        replay = connection.execute(
            "SELECT payload_hash, outcome FROM requests WHERE scope='start' AND request_id=?",
            (request_id,),
        ).fetchone()
        if replay is not None:
            if replay["payload_hash"] != payload_hash:
                raise SwarmStoreError("request_conflict")
            result = _load(replay["outcome"])
            result["replayed"] = True
            return result
        profile = connection.execute(
            "SELECT payload,revision FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
        if profile is None:
            raise SwarmStoreError("profile_not_found")
        if int(profile["revision"]) != expected_profile_revision:
            raise SwarmStoreError("revision_conflict")
        profile_snapshot = _load(profile["payload"])
        swarm_id = new_id("swr")
        now = _now()
        connection.execute(
            "INSERT INTO swarms(id,prompt,profile_snapshot,effective_configuration,state,created_at) VALUES(?,?,?,?,?,?)",
            (swarm_id, prompt, _dump(profile_snapshot), _dump(effective), "preparing", now),
        )
        _refresh_swarm_state(db, connection, swarm_id)
        connection.execute(
            "INSERT INTO swarm_settings(swarm_id,revision,delivery_json) VALUES(?,?,?)",
            (swarm_id, 1, _dump(profile_snapshot["delivery"])),
        )
        connection.execute(
            "INSERT INTO swarm_epochs(swarm_id,epoch,is_open) VALUES(?,?,?)",
            (swarm_id, 0, 1),
        )
        main_id = new_id("dsc")
        connection.execute(
            "INSERT INTO discussions(id,swarm_id,title,sequence,is_main,created_at) VALUES(?,?,?,?,1,?)",
            (main_id, swarm_id, "Main", 1, now),
        )
        names = list(_PARTICIPANT_NAMES)
        secrets.SystemRandom().shuffle(names)
        ordinal = 0
        for formation in profile_snapshot["participants"]:
            for _ in range(formation["count"]):
                ordinal += 1
                participant_id = new_id("prt")
                name = names[(ordinal - 1) % len(names)]
                if ordinal > len(names):
                    suffix = str(ordinal - len(names))
                    name = f"{name[: 8 - len(suffix)]}{suffix}"
                connection.execute(
                    "INSERT INTO participants(id,swarm_id,model,display_name,ordinal,state) VALUES(?,?,?,?,?,?)",
                    (participant_id, swarm_id, formation["model"], name, ordinal, "idle"),
                )
                connection.execute(
                    "INSERT INTO memberships(discussion_id,participant_id) VALUES(?,?)",
                    (main_id, participant_id),
                )
        result = {"swarm_id": swarm_id, "main_discussion_id": main_id, "state": "preparing"}
        connection.execute(
            "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES('start',?,?,?)",
            (request_id, payload_hash, _dump(result)),
        )
        return result

    return db._write(operation)


def _begin_delete(db: SwarmDatabase, swarm_id: str) -> bool:
    def operation(connection: sqlite3.Connection) -> bool:
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            return False
        epoch = connection.execute(
            "SELECT is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if (
            row["state"] not in {"stopped", "cancelled", "interrupted", "deleting"}
            or epoch["is_open"]
        ):
            raise SwarmStoreError("swarm_not_stopped")
        if connection.execute(
            "SELECT 1 FROM participants WHERE swarm_id=? AND state='running'", (swarm_id,)
        ).fetchone():
            raise SwarmStoreError("swarm_not_stopped")
        connection.execute("UPDATE swarms SET state='deleting' WHERE id=?", (swarm_id,))
        return True

    return db._write(operation)


def _delete_swarm(db: SwarmDatabase, swarm_id: str) -> None:
    def operation(connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            return
        if row["state"] != "deleting":
            raise SwarmStoreError("invalid_lifecycle_state")
        for table in (
            "delivery_batch_entries",
            "delivery_batches",
            "recipients",
            "memberships",
            "participant_sessions",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE participant_id IN (SELECT id FROM participants WHERE swarm_id=?)",
                (swarm_id,),
            )
        for table in (
            "posts",
            "discussions",
            "participants",
            "swarm_settings",
            "swarm_execution_epochs",
            "swarm_epochs",
            "swarm_events",
        ):
            connection.execute(f"DELETE FROM {table} WHERE swarm_id=?", (swarm_id,))
        connection.execute(
            "DELETE FROM requests WHERE scope IN (?,?,?,?) "
            "OR substr(scope,1,?)=? OR substr(scope,1,?)=? "
            "OR (scope='start' AND json_extract(outcome,'$.swarm_id')=?)",
            (
                f"settings:{swarm_id}",
                f"stop:{swarm_id}",
                f"stop-finish:{swarm_id}",
                f"resume:{swarm_id}",
                len(f"post:{swarm_id}:"),
                f"post:{swarm_id}:",
                len(f"create:{swarm_id}:"),
                f"create:{swarm_id}:",
                swarm_id,
            ),
        )
        connection.execute("DELETE FROM swarms WHERE id=?", (swarm_id,))

    db._write(operation)


def _get_swarm(db: SwarmDatabase, swarm_id: str) -> Json:
    connection = db._require_connection()
    row = connection.execute("SELECT * FROM swarms WHERE id=?", (swarm_id,)).fetchone()
    if row is None:
        raise SwarmStoreError("swarm_not_found")
    participants = connection.execute(
        "SELECT p.id,p.model,p.display_name,p.ordinal,p.state,p.lifecycle_run_id,"
        "(SELECT COUNT(*) FROM recipients r JOIN posts ps ON ps.id=r.post_id "
        "WHERE r.participant_id=p.id AND r.delivered_at IS NULL AND ps.swarm_id=p.swarm_id) "
        "AS pending_count FROM participants p WHERE p.swarm_id=? ORDER BY p.ordinal",
        (swarm_id,),
    ).fetchall()
    main = connection.execute(
        "SELECT id FROM discussions WHERE swarm_id=? AND is_main=1", (swarm_id,)
    ).fetchone()
    return {
        "id": swarm_id,
        "prompt": row["prompt"],
        "profile_snapshot": _load(row["profile_snapshot"]),
        "effective_configuration": _load(row["effective_configuration"]),
        "state": row["state"],
        "delivery": _load(
            connection.execute(
                "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        ),
        "settings_revision": int(
            connection.execute(
                "SELECT revision FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        ),
        "main_discussion_id": main["id"],
        "epoch": int(
            connection.execute(
                "SELECT epoch FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        ),
        "execution_epoch": (lambda value: value[0] if value else None)(
            connection.execute(
                "SELECT execution_epoch FROM swarm_execution_epochs WHERE swarm_id=? ORDER BY epoch DESC LIMIT 1",
                (swarm_id,),
            ).fetchone()
        ),
        "participants": [dict(value) for value in participants],
    }


def _list_swarms(db: SwarmDatabase, cursor: str | None, limit: int) -> Page:
    connection = db._require_connection()
    high_water = int(connection.execute("SELECT COALESCE(MAX(rowid),0) FROM swarms").fetchone()[0])
    if cursor:
        offset, frozen_high_water = db._cursor(cursor, "swarms", "all", high_water)
        if frozen_high_water is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen_high_water
    else:
        offset = 0
    rows = connection.execute(
        "SELECT s.id,s.state,s.created_at,substr(s.prompt,1,120) AS title,COUNT(p.id) AS participant_count,"
        "ss.revision AS settings_revision "
        "FROM swarms s JOIN swarm_settings ss ON ss.swarm_id=s.id "
        "LEFT JOIN participants p ON p.swarm_id=s.id WHERE s.rowid<=? "
        "GROUP BY s.id ORDER BY s.rowid DESC LIMIT ? OFFSET ?",
        (high_water, limit + 1, offset),
    ).fetchall()
    entries = [
        {
            "id": str(row["id"]),
            "title": str(row["title"]).split("\n", 1)[0],
            "state": str(row["state"]),
            "created_at": str(row["created_at"]),
            "participant_count": int(row["participant_count"]),
            "settings_revision": int(row["settings_revision"]),
        }
        for row in rows
    ]
    return _page(
        entries,
        limit,
        db._make_cursor("swarms", "all", high_water, offset + limit),
    )


def _list_events(db: SwarmDatabase, swarm_id: str, cursor: str | None, limit: int) -> Page:
    connection = db._require_connection()
    if connection.execute("SELECT 1 FROM swarms WHERE id=?", (swarm_id,)).fetchone() is None:
        raise SwarmStoreError("swarm_not_found")
    high_water = int(
        connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM swarm_events WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    )
    if cursor:
        offset, frozen_high_water = db._cursor(cursor, "events", swarm_id, high_water)
        if frozen_high_water is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen_high_water
    else:
        offset = 0
    rows = connection.execute(
        "SELECT id,kind,actor,old_json,new_json,settings_revision,created_at FROM swarm_events "
        "WHERE swarm_id=? AND id<=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (swarm_id, high_water, limit + 1, offset),
    ).fetchall()
    entries = [
        {
            "id": int(row["id"]),
            "kind": str(row["kind"]),
            "actor": str(row["actor"]),
            "old": _load(row["old_json"]) if row["old_json"] is not None else None,
            "new": _load(row["new_json"]) if row["new_json"] is not None else None,
            "settings_revision": row["settings_revision"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]
    return _page(
        entries,
        limit,
        db._make_cursor("events", swarm_id, high_water, offset + limit),
    )


def _apply_delivery_settings(
    db: SwarmDatabase,
    swarm_id: str,
    delivery: Json,
    expected_revision: int,
    request_id: str,
    actor: str,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        scope = f"settings:{swarm_id}"
        payload_hash = _hash({"delivery": delivery, "expected_revision": expected_revision})
        replay = connection.execute(
            "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
            (scope, request_id),
        ).fetchone()
        if replay is not None:
            if replay["payload_hash"] != payload_hash:
                raise SwarmStoreError("request_conflict")
            result = _load(replay["outcome"])
            result["replayed"] = True
            return result
        _assert_mutable(connection, swarm_id)
        row = connection.execute(
            "SELECT revision,delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        if int(row["revision"]) != expected_revision:
            raise SwarmStoreError("revision_conflict")
        old = _load(row["delivery_json"])
        revision = expected_revision + 1
        connection.execute(
            "UPDATE swarm_settings SET revision=?,delivery_json=? WHERE swarm_id=?",
            (revision, _dump(delivery), swarm_id),
        )
        for participant in connection.execute(
            "SELECT id FROM participants WHERE swarm_id=? AND wake_pending=1", (swarm_id,)
        ):
            still_eligible = connection.execute(
                "SELECT 1 FROM recipients r JOIN posts p ON p.id=r.post_id "
                "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL "
                "AND r.route_class IN (?,?,?) LIMIT 1",
                (swarm_id, participant["id"], *_DELIVERY_ROUTES),
            ).fetchone()
            if still_eligible is None or not any(
                delivery[route]["wake_idle"]
                for route in _DELIVERY_ROUTES
                if connection.execute(
                    "SELECT 1 FROM recipients r JOIN posts p ON p.id=r.post_id "
                    "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL "
                    "AND r.route_class=? LIMIT 1",
                    (swarm_id, participant["id"], route),
                ).fetchone()
                is not None
            ):
                connection.execute(
                    "UPDATE participants SET wake_pending=0 WHERE id=?", (participant["id"],)
                )
        newly_enabled = {
            route
            for route in _DELIVERY_ROUTES
            if not old[route]["wake_idle"] and delivery[route]["wake_idle"]
        }
        wake_intents: list[Json] = []
        if newly_enabled:
            for participant in connection.execute(
                "SELECT id,state,wake_pending,wake_announced_seq FROM participants WHERE swarm_id=?",
                (swarm_id,),
            ):
                if participant["state"] not in {"idle"} or participant["wake_pending"]:
                    continue
                placeholders = ",".join("?" for _ in newly_enabled)
                pending = connection.execute(
                    "SELECT COUNT(*) AS count,MAX(p.sequence) AS newest FROM recipients r "
                    "JOIN posts p ON p.id=r.post_id WHERE p.swarm_id=? AND r.participant_id=? "
                    "AND r.delivered_at IS NULL AND r.route_class IN (" + placeholders + ")",
                    (swarm_id, participant["id"], *sorted(newly_enabled)),
                ).fetchone()
                if pending["newest"] is None or int(pending["newest"]) <= int(
                    participant["wake_announced_seq"]
                ):
                    continue
                connection.execute(
                    "UPDATE participants SET wake_pending=1 WHERE id=?", (participant["id"],)
                )
                wake_intents.append(
                    {
                        "participant_id": str(participant["id"]),
                        "pending_count": int(pending["count"]),
                        "settings_revision": revision,
                    }
                )
        connection.execute(
            "INSERT INTO swarm_events(swarm_id,kind,actor,old_json,new_json,settings_revision,created_at) VALUES(?,?,?,?,?,?,?)",
            (swarm_id, "settings", actor, _dump(old), _dump(delivery), revision, _now()),
        )
        result = {
            "revision": revision,
            "old": old,
            "new": delivery,
            "effective": delivery,
            "wake_intents": wake_intents,
        }
        connection.execute(
            "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
            (scope, request_id, payload_hash, _dump(result)),
        )
        return result

    return db._write(operation)
