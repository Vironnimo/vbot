"""Store delivery."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import sqlite3

from core.sessions import DeliveryReceipt, SessionAddress
from core.utils.ids import new_id

from ._store_database import (
    SwarmDatabase,
)
from ._store_lifecycle import (
    _refresh_swarm_state,
)
from ._store_records import (
    _assert_epoch,
    _assert_mutable,
    _participant,
    _pending_count,
    _pending_rows,
    _pending_rows_from_rows,
)
from ._store_values import (
    Json,
    Page,
    SwarmStoreError,
    _hash,
    _load,
    _now,
    _page,
    _post,
)


def _prepare_delivery(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    post_ids: tuple[str, ...] | None,
    limit: int,
    effect_kind: str,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _participant(connection, swarm_id, participant_id)
        binding = connection.execute(
            "SELECT project_id,agent_id,session_id,generation_id,owner_name FROM participant_sessions WHERE participant_id=?",
            (participant_id,),
        ).fetchone()
        if binding is None:
            raise SwarmStoreError("participant_unbound")
        settings = _load(
            connection.execute(
                "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        if post_ids is None:
            rows = _pending_rows(
                connection, swarm_id, participant_id, limit, settings["batch_chars"]
            )
            receipt_id = new_id("rcp")
        else:
            if not post_ids:
                return {"entries": [], "receipt_id": None, "pending_remaining": 0}
            placeholders = ",".join("?" for _ in post_ids)
            rows = connection.execute(
                "SELECT p.*,r.route_class,d.title AS discussion_title "
                "FROM recipients r JOIN posts p ON p.id=r.post_id "
                "JOIN discussions d ON d.id=p.discussion_id "
                f"WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL AND p.id IN ({placeholders}) ORDER BY p.sequence",
                (swarm_id, participant_id, *post_ids),
            ).fetchall()
            receipt_id = new_id("rcp")
        entries = [_post(row) for row in rows]
        if not entries:
            return {"entries": [], "receipt_id": None, "pending_remaining": 0}
        content_hash = _hash({"effect_kind": effect_kind, "posts": entries})
        connection.execute(
            "INSERT INTO delivery_batches(receipt_id,participant_id,content_hash,effect_kind,created_at) VALUES(?,?,?,?,?)",
            (receipt_id, participant_id, content_hash, effect_kind, _now()),
        )
        for entry in entries:
            connection.execute(
                "INSERT INTO delivery_batch_entries(receipt_id,post_id,participant_id) VALUES(?,?,?)",
                (receipt_id, entry["id"], participant_id),
            )
        pending_remaining = int(
            connection.execute(
                "SELECT COUNT(*) FROM recipients r JOIN posts p ON p.id=r.post_id WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                (swarm_id, participant_id),
            ).fetchone()[0]
        ) - len(entries)
        return {
            "entries": entries,
            "receipt_id": receipt_id,
            "content_hash": content_hash,
            "effect_kind": effect_kind,
            "pending_remaining": max(0, pending_remaining),
        }

    return db._write(operation)


def _prepare_automatic_delivery(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    expected_epoch: int,
    boundary: int | None,
    wake_only: bool,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        # Busy participants select their batch at the next request boundary;
        # freezing it in the wake scan could replay content read by a Tool.
        if wake_only and participant["state"] != "idle":
            return {
                "entries": [],
                "wake": False,
                "pending_remaining": _pending_count(connection, swarm_id, participant_id),
            }
        resolved_boundary = int(participant["idle_boundary"] or 0) if boundary is None else boundary
        if participant["state"] in {"failed", "cancelled", "interrupted"}:
            return {"entries": [], "wake": False, "pending_remaining": 0}
        settings_row = connection.execute(
            "SELECT revision,delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        settings = _load(settings_row["delivery_json"])
        prepared = connection.execute(
            "SELECT receipt_id,content_hash,effect_kind,settings_revision FROM delivery_batches "
            "WHERE participant_id=? AND effect_kind='swarm_automatic' AND acknowledged_at IS NULL "
            "ORDER BY created_at LIMIT 1",
            (participant_id,),
        ).fetchone()
        if prepared is not None:
            newest = int(
                connection.execute(
                    "SELECT COALESCE(MAX(p.sequence),0) FROM recipients r JOIN posts p ON p.id=r.post_id "
                    "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                    (swarm_id, participant_id),
                ).fetchone()[0]
            )
            wake = (
                newest > int(participant["wake_announced_seq"] or 0)
                and not participant["wake_pending"]
                and participant["state"] in {"idle"}
                and any(
                    settings[row["route_class"]]["wake_idle"]
                    for row in connection.execute(
                        "SELECT r.route_class FROM recipients r JOIN posts p ON p.id=r.post_id "
                        "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                        (swarm_id, participant_id),
                    )
                )
            )
            if wake:
                connection.execute(
                    "UPDATE participants SET wake_epoch=?,wake_pending=1,wake_pending_seq=? WHERE id=?",
                    (expected_epoch, newest, participant_id),
                )
            rows = connection.execute(
                "SELECT p.*,r.route_class,d.title AS discussion_title FROM delivery_batch_entries e JOIN posts p ON p.id=e.post_id "
                "JOIN recipients r ON r.post_id=p.id AND r.participant_id=e.participant_id "
                "JOIN discussions d ON d.id=p.discussion_id "
                "WHERE e.receipt_id=? ORDER BY p.sequence",
                (prepared["receipt_id"],),
            ).fetchall()
            return {
                "entries": [_post(row) for row in rows],
                "receipt_id": prepared["receipt_id"],
                "content_hash": prepared["content_hash"],
                "effect_kind": prepared["effect_kind"],
                "wake": wake,
                "pending_remaining": _pending_count(connection, swarm_id, participant_id)
                - len(rows),
                "settings_revision": int(prepared["settings_revision"]),
                "replayed": True,
            }
        pending = connection.execute(
            "SELECT p.*,r.route_class,d.title AS discussion_title FROM recipients r JOIN posts p ON p.id=r.post_id JOIN discussions d ON d.id=p.discussion_id "
            "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL ORDER BY p.sequence",
            (swarm_id, participant_id),
        ).fetchall()
        eligible = []
        for row in pending:
            policy = settings[row["route_class"]]
            if policy["mode"] == "pull" and not (
                participant["state"] == "idle" and policy["wake_idle"]
            ):
                continue
            if policy["mode"] == "idle" and participant["state"] != "idle":
                continue
            eligible.append(row)
        newest = max((int(row["sequence"]) for row in pending), default=0)
        epoch = connection.execute(
            "SELECT epoch,is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if not epoch["is_open"] or int(epoch["epoch"]) != expected_epoch:
            raise SwarmStoreError("stale_epoch")
        epoch_value = int(epoch["epoch"])
        announced = int(participant["wake_announced_seq"] or 0)
        wake = bool(
            newest > announced
            and not participant["wake_pending"]
            and any(settings[row["route_class"]]["wake_idle"] for row in pending)
            and participant["state"] in {"idle"}
        )
        if wake:
            connection.execute(
                "UPDATE participants SET wake_epoch=?,wake_pending=1,wake_pending_seq=?,idle_boundary=? WHERE id=?",
                (epoch_value, newest, resolved_boundary, participant_id),
            )
        if not eligible:
            return {
                "entries": [],
                "wake": wake,
                "pending_remaining": len(pending),
                "settings_revision": int(settings_row["revision"]),
                "admission_boundary": resolved_boundary,
            }
        rows = _pending_rows_from_rows(
            eligible, settings["batch_messages"], settings["batch_chars"]
        )
        receipt_id = new_id("rcp")
        entries = [_post(row) for row in rows]
        content_hash = _hash(
            {
                "effect_kind": "swarm_automatic",
                "posts": entries,
                "revision": int(settings_row["revision"]),
            }
        )
        connection.execute(
            "INSERT INTO delivery_batches(receipt_id,participant_id,content_hash,effect_kind,created_at,settings_revision) VALUES(?,?,?,?,?,?)",
            (
                receipt_id,
                participant_id,
                content_hash,
                "swarm_automatic",
                _now(),
                int(settings_row["revision"]),
            ),
        )
        for entry in entries:
            connection.execute(
                "INSERT INTO delivery_batch_entries(receipt_id,post_id,participant_id) VALUES(?,?,?)",
                (receipt_id, entry["id"], participant_id),
            )
        return {
            "entries": entries,
            "receipt_id": receipt_id,
            "content_hash": content_hash,
            "effect_kind": "swarm_automatic",
            "wake": wake,
            "pending_remaining": len(pending) - len(entries),
            "settings_revision": int(settings_row["revision"]),
            "admission_boundary": resolved_boundary,
        }

    return db._write(operation)


def _list_prepared_deliveries(db: SwarmDatabase, cursor: str | None, limit: int) -> Page:
    connection = db._require_connection()
    high_water = int(
        connection.execute("SELECT COALESCE(MAX(rowid),0) FROM delivery_batches").fetchone()[0]
    )
    offset = 0
    if cursor:
        offset, frozen = db._cursor(cursor, "prepared", "all", high_water)
        if frozen is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen
    rows = connection.execute(
        "SELECT receipt_id,participant_id,content_hash,effect_kind,settings_revision FROM delivery_batches WHERE acknowledged_at IS NULL AND rowid<=? ORDER BY rowid LIMIT ? OFFSET ?",
        (high_water, limit + 1, offset),
    ).fetchall()
    return _page(
        [dict(row) for row in rows],
        limit,
        db._make_cursor("prepared", "all", high_water, offset + limit),
    )


def _claim_wake(db: SwarmDatabase, swarm_id: str, participant_id: str, expected_epoch: int) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        if not participant["wake_pending"] or int(participant["wake_epoch"]) != expected_epoch:
            return {"participant_id": participant_id, "pending": False}
        return {
            "participant_id": participant_id,
            "pending": True,
            "boundary": participant["idle_boundary"],
        }

    return db._write(operation)


def _list_wake_intents(db: SwarmDatabase, swarm_id: str, cursor: str | None, limit: int) -> Page:
    connection = db._require_connection()
    _assert_mutable(connection, swarm_id)
    high_water = int(
        connection.execute(
            "SELECT COALESCE(MAX(ordinal),0) FROM participants WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    )
    offset = 0
    if cursor:
        offset, frozen = db._cursor(cursor, "wakes", swarm_id, high_water)
        if frozen is None:
            raise SwarmStoreError("invalid_cursor")
        high_water = frozen
    rows = connection.execute(
        "SELECT id,wake_epoch,wake_announced_seq,idle_boundary FROM participants WHERE swarm_id=? AND wake_pending=1 AND ordinal<=? ORDER BY ordinal LIMIT ? OFFSET ?",
        (swarm_id, high_water, limit + 1, offset),
    ).fetchall()
    entries = [
        {
            "participant_id": row["id"],
            "epoch": row["wake_epoch"],
            "boundary": row["idle_boundary"],
            "announced_sequence": row["wake_announced_seq"],
        }
        for row in rows
    ]
    return _page(entries, limit, db._make_cursor("wakes", swarm_id, high_water, offset + limit))


def _mark_wake_admitted(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    expected_epoch: int,
    run_id: str,
    boundary: int | None,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        if (
            not participant["wake_pending"]
            and participant["lifecycle_run_id"] == run_id
            and participant["idle_boundary"] == boundary
        ):
            return {
                "participant_id": participant_id,
                "run_id": run_id,
                "boundary": boundary,
                "admitted": True,
                "replayed": True,
            }
        if not participant["wake_pending"] or participant["idle_boundary"] != boundary:
            raise SwarmStoreError("wake_unavailable")
        state = participant["state"] if participant["lifecycle_run_id"] == run_id else "running"
        connection.execute(
            "UPDATE participants SET wake_pending=0,wake_announced_seq=wake_pending_seq,state=?,lifecycle_run_id=?,idle_boundary=? WHERE id=?",
            (state, run_id, boundary, participant_id),
        )
        _refresh_swarm_state(db, connection, swarm_id)
        return {
            "participant_id": participant_id,
            "run_id": run_id,
            "boundary": boundary,
            "admitted": True,
        }

    return db._write(operation)


def _prepared_delivery(db: SwarmDatabase, receipt_id: str) -> Json | None:
    row = (
        db._require_connection()
        .execute(
            "SELECT b.participant_id,b.receipt_id,b.content_hash,b.effect_kind,b.acknowledged_at,s.project_id,s.agent_id,s.session_id,s.generation_id,s.owner_name "
            "FROM delivery_batches b JOIN participant_sessions s ON s.participant_id=b.participant_id "
            "WHERE b.receipt_id=?",
            (receipt_id,),
        )
        .fetchone()
    )
    if row is None:
        return None
    return {
        "participant_id": str(row["participant_id"]),
        "receipt_id": str(row["receipt_id"]),
        "content_hash": str(row["content_hash"]),
        "effect_kind": str(row["effect_kind"]),
        "generation_id": str(row["generation_id"]),
        "owner_name": str(row["owner_name"]),
        "acknowledged": row["acknowledged_at"] is not None,
        "address": SessionAddress(row["project_id"], str(row["agent_id"]), str(row["session_id"])),
    }


def _acknowledge_delivery(
    db: SwarmDatabase, prepared: Json, receipt: DeliveryReceipt | None
) -> bool:
    if receipt is None:
        return False
    location = receipt.carrier_location
    if (
        receipt.receipt_id != prepared["receipt_id"]
        or receipt.content_hash != prepared["content_hash"]
        or receipt.effect_kind != prepared["effect_kind"]
        or not isinstance(location, dict)
        or not isinstance(location.get("kind"), str)
        or type(location.get("sequence")) is not int
        or location["sequence"] < 0
    ):
        raise SwarmStoreError("receipt_conflict")

    def operation(connection: sqlite3.Connection) -> bool:
        if prepared["acknowledged"]:
            return True
        connection.execute(
            "UPDATE recipients SET delivered_at=?,carrier_kind=?,carrier_sequence=? "
            "WHERE participant_id=? AND delivered_at IS NULL AND post_id IN "
            "(SELECT post_id FROM delivery_batch_entries WHERE receipt_id=? AND participant_id=?)",
            (
                _now(),
                location["kind"],
                location["sequence"],
                prepared["participant_id"],
                receipt.receipt_id,
                prepared["participant_id"],
            ),
        )
        connection.execute(
            "UPDATE delivery_batches SET acknowledged_at=?,carrier_kind=?,carrier_sequence=? WHERE receipt_id=?",
            (_now(), location["kind"], location["sequence"], receipt.receipt_id),
        )
        return True

    return db._write(operation)
