"""Store lifecycle."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import sqlite3

from core.sessions import TemporarySessionBinding

from ._store_database import (
    SwarmDatabase,
)
from ._store_records import (
    _assert_epoch,
    _assert_mutable,
    _main,
    _participant,
    _pending_count,
    _record_lifecycle_request,
    _request_replay,
)
from ._store_values import (
    _MUTABLE_SWARM_STATES,
    Json,
    SwarmStoreError,
    _dump,
    _hash,
    _load,
    _now,
)


def _set_swarm_state(db: SwarmDatabase, swarm_id: str, state: str) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        if row["state"] not in _MUTABLE_SWARM_STATES:
            raise SwarmStoreError("invalid_lifecycle_state")
        connection.execute("UPDATE swarms SET state=? WHERE id=?", (state, swarm_id))
        return {"state": state}

    return db._write(operation)


def _fail_startup(db: SwarmDatabase, swarm_id: str, expected_epoch: int) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        connection.execute("UPDATE swarm_epochs SET is_open=0 WHERE swarm_id=?", (swarm_id,))
        connection.execute("DELETE FROM swarm_execution_epochs WHERE swarm_id=?", (swarm_id,))
        connection.execute("UPDATE swarms SET state='needs_attention' WHERE id=?", (swarm_id,))
        return {"swarm_id": swarm_id, "state": "needs_attention"}

    return db._write(operation)


def _refresh_swarm_state(db: SwarmDatabase, connection: sqlite3.Connection, swarm_id: str) -> None:
    row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
    if row is None or row["state"] not in {"running", "idle", "needs_attention"}:
        return
    states = {
        str(item["state"])
        for item in connection.execute(
            "SELECT state FROM participants WHERE swarm_id=?", (swarm_id,)
        )
    }
    if "running" in states:
        state = "running"
    elif states & {"failed", "cancelled", "interrupted"}:
        state = "needs_attention"
    elif states and states <= {"idle"}:
        state = "idle"
    else:
        state = "running"
    connection.execute("UPDATE swarms SET state=? WHERE id=?", (state, swarm_id))


def _set_participant_state(
    db: SwarmDatabase, swarm_id: str, participant_id: str, state: str, idle_boundary: int | None
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_mutable(connection, swarm_id)
        participant = _participant(connection, swarm_id, participant_id)
        connection.execute(
            "UPDATE participants SET state=?,idle_boundary=?,wake_pending=? WHERE id=?",
            (
                state,
                idle_boundary,
                0 if state == "running" else participant["wake_pending"],
                participant_id,
            ),
        )
        _refresh_swarm_state(db, connection, swarm_id)
        return {
            "participant_id": participant_id,
            "state": state,
            "idle_boundary": idle_boundary,
        }

    return db._write(operation)


def _participant_status(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    cursor: str | None,
    limit: int,
) -> Json:
    connection = db._require_connection()
    _participant(connection, swarm_id, participant_id)
    high = int(
        connection.execute(
            "SELECT COALESCE(MAX(ordinal),0) FROM participants WHERE swarm_id=?", (swarm_id,)
        ).fetchone()[0]
    )
    scope = f"{swarm_id}:{participant_id}:{limit}"
    offset = db._cursor(cursor, "status", scope, high)[0] if cursor else 0
    rows = connection.execute(
        "SELECT id,display_name,state FROM participants WHERE swarm_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
        (swarm_id, limit + 1, offset),
    ).fetchall()

    self_row = connection.execute(
        "SELECT id,state FROM participants WHERE id=?",
        (participant_id,),
    ).fetchone()
    totals = {
        str(row["state"]): int(row["count"])
        for row in connection.execute(
            "SELECT state,COUNT(*) AS count FROM participants WHERE swarm_id=? GROUP BY state",
            (swarm_id,),
        )
    }
    settings = connection.execute(
        "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
    ).fetchone()
    delivery = _load(settings["delivery_json"])
    receive = {
        label: [
            route for route in ("main", "discussion", "ping") if delivery[route]["mode"] == mode
        ]
        for label, mode in (("automatic", "all"), ("when_idle", "idle"), ("on_request", "pull"))
    }
    roster = [
        {"id": row["id"], "name": row["display_name"], "state": row["state"]}
        for row in rows[:limit]
    ]
    consumed = len(roster)
    has_more = consumed < len(rows)
    main = _main(connection, swarm_id)
    return {
        "self": {"id": self_row["id"], "state": self_row["state"]},
        "main_discussion_id": main,
        "delivery": {label: routes for label, routes in receive.items() if routes},
        "wake_on_messages": [
            route for route in ("main", "discussion", "ping") if delivery[route]["wake_idle"]
        ],
        "pending_count": _pending_count(connection, swarm_id, participant_id),
        "state_totals": totals,
        "roster": roster,
        "has_more": has_more,
        "cursor": db._make_cursor("status", scope, high, offset + consumed) if has_more else None,
    }


def _record_run_started(
    db: SwarmDatabase, swarm_id: str, participant_id: str, run_id: str, expected_epoch: int
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        if participant["lifecycle_run_id"] == run_id:
            return {
                "participant_id": participant_id,
                "run_id": run_id,
                "state": participant["state"],
            }
        connection.execute(
            "UPDATE participants SET state='running',lifecycle_run_id=? WHERE id=?",
            (run_id, participant_id),
        )
        _refresh_swarm_state(db, connection, swarm_id)
        return {"participant_id": participant_id, "run_id": run_id, "state": "running"}

    return db._write(operation)


def _reconcile_run_finished(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    run_id: str,
    expected_epoch: int,
    outcome: str,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        if participant["lifecycle_run_id"] != run_id:
            raise SwarmStoreError("stale_run")
        state = "idle" if outcome == "completed" else outcome
        connection.execute("UPDATE participants SET state=? WHERE id=?", (state, participant_id))
        _refresh_swarm_state(db, connection, swarm_id)
        return {"participant_id": participant_id, "run_id": run_id, "state": state}

    return db._write(operation)


def _recover_interrupted(db: SwarmDatabase) -> list[Json]:
    def operation(connection: sqlite3.Connection) -> list[Json]:
        rows = connection.execute(
            "SELECT id,state FROM swarms WHERE state IN (?,?,?,?,?)",
            ("preparing", "running", "idle", "needs_attention", "stopping"),
        ).fetchall()
        result: list[Json] = []
        for row in rows:
            swarm_id = str(row["id"])
            connection.execute("UPDATE swarm_epochs SET is_open=0 WHERE swarm_id=?", (swarm_id,))
            connection.execute("UPDATE swarms SET state='interrupted' WHERE id=?", (swarm_id,))
            connection.execute(
                "UPDATE participants SET state=CASE WHEN state='running' THEN 'interrupted' ELSE state END,wake_pending=0 WHERE swarm_id=?",
                (swarm_id,),
            )
            connection.execute(
                "INSERT INTO swarm_events(swarm_id,kind,actor,old_json,new_json,settings_revision,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    swarm_id,
                    "recovered",
                    "system",
                    _dump({"state": row["state"]}),
                    _dump({"state": "interrupted"}),
                    None,
                    _now(),
                ),
            )
            result.append({"swarm_id": swarm_id, "state": "interrupted"})
        return result

    return db._write(operation)


def _begin_stop(db: SwarmDatabase, swarm_id: str, request_id: str, actor: str) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        scope = f"stop:{swarm_id}"
        payload_hash = _hash({"action": "begin"})
        replay = _request_replay(connection, scope, request_id, payload_hash)
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        if row["state"] == "deleting":
            raise SwarmStoreError("swarm_closed")
        if replay is not None:
            finished = connection.execute(
                "SELECT outcome FROM requests WHERE scope=? AND request_id=?",
                (f"stop-finish:{swarm_id}", request_id),
            ).fetchone()
            if finished is not None:
                return {**_load(finished["outcome"]), "replayed": True}
            return replay
        epoch = connection.execute(
            "SELECT epoch FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if row["state"] in {"stopped", "cancelled"}:
            result = {
                "swarm_id": swarm_id,
                "state": str(row["state"]),
                "epoch": int(epoch["epoch"]),
            }
            _record_lifecycle_request(
                connection,
                scope,
                request_id,
                payload_hash,
                swarm_id,
                "stop_requested",
                actor,
                row["state"],
                result,
            )
            return result
        connection.execute("UPDATE swarm_epochs SET is_open=0 WHERE swarm_id=?", (swarm_id,))
        connection.execute("UPDATE swarms SET state='stopping' WHERE id=?", (swarm_id,))
        result = {"swarm_id": swarm_id, "state": "stopping", "epoch": int(epoch["epoch"])}
        _record_lifecycle_request(
            connection,
            scope,
            request_id,
            payload_hash,
            swarm_id,
            "stop_requested",
            actor,
            row["state"],
            result,
        )
        return result

    return db._write(operation)


def _finish_stop(
    db: SwarmDatabase, swarm_id: str, request_id: str, actor: str, drain_report: Json
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        scope = f"stop-finish:{swarm_id}"
        payload_hash = _hash({"drain_report": drain_report})
        replay = _request_replay(connection, scope, request_id, payload_hash)
        if replay is not None:
            return replay
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        if row["state"] != "stopping":
            raise SwarmStoreError("invalid_lifecycle_state")
        connection.execute(
            "UPDATE participants SET state=CASE WHEN state='running' THEN 'cancelled' ELSE state END,wake_pending=0 WHERE swarm_id=?",
            (swarm_id,),
        )
        connection.execute("UPDATE swarms SET state='cancelled' WHERE id=?", (swarm_id,))
        result = {"swarm_id": swarm_id, "state": "cancelled", "drain_report": drain_report}
        _record_lifecycle_request(
            connection,
            scope,
            request_id,
            payload_hash,
            swarm_id,
            "stopped",
            actor,
            row["state"],
            result,
        )
        return result

    return db._write(operation)


def _begin_resume(
    db: SwarmDatabase, swarm_id: str, request_id: str, actor: str, participant_id: str | None
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        scope = f"resume:{swarm_id}"
        payload_hash = _hash({"action": "begin", "participant_id": participant_id})
        replay = _request_replay(connection, scope, request_id, payload_hash)
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        if row["state"] == "deleting":
            raise SwarmStoreError("swarm_closed")
        if replay is not None:
            return replay
        if row["state"] in {"stopping", "preparing"}:
            raise SwarmStoreError("swarm_closed")
        epoch = connection.execute(
            "SELECT epoch,is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if participant_id is not None:
            target = connection.execute(
                "SELECT state FROM participants WHERE swarm_id=? AND id=?",
                (swarm_id, participant_id),
            ).fetchone()
            if target is None:
                raise SwarmStoreError("participant_not_found")
            eligible = {"idle", "failed", "cancelled", "interrupted"}
            if target["state"] not in eligible:
                raise SwarmStoreError("participant_not_resumable")
        if epoch["is_open"]:
            participants = [
                str(item["id"])
                for item in connection.execute(
                    "SELECT id FROM participants WHERE swarm_id=? "
                    "AND state IN ('idle','failed','cancelled','interrupted') "
                    "ORDER BY ordinal",
                    (swarm_id,),
                )
            ]
            if participant_id is not None:
                participants = [participant_id]
            result = {
                "swarm_id": swarm_id,
                "state": str(row["state"]),
                "epoch": int(epoch["epoch"]),
                "participant_ids": participants,
                "reused_epoch": True,
            }
            _record_lifecycle_request(
                connection,
                scope,
                request_id,
                payload_hash,
                swarm_id,
                "resume_requested",
                actor,
                row["state"],
                result,
            )
            return result
        next_epoch = int(epoch["epoch"]) + 1
        participants = [
            str(item["id"])
            for item in connection.execute(
                "SELECT id FROM participants WHERE swarm_id=? ORDER BY ordinal",
                (swarm_id,),
            )
        ]
        if participant_id is not None:
            participants = [participant_id]
        connection.executemany(
            "UPDATE participants SET state='idle',wake_pending=0 WHERE swarm_id=? AND id=?",
            [(swarm_id, peer_id) for peer_id in participants],
        )
        connection.execute(
            "UPDATE swarm_epochs SET epoch=?,is_open=1 WHERE swarm_id=?", (next_epoch, swarm_id)
        )
        connection.execute("DELETE FROM swarm_execution_epochs WHERE swarm_id=?", (swarm_id,))
        connection.execute("UPDATE swarms SET state='preparing' WHERE id=?", (swarm_id,))
        result = {
            "swarm_id": swarm_id,
            "state": "preparing",
            "epoch": next_epoch,
            "participant_ids": participants,
        }
        _record_lifecycle_request(
            connection,
            scope,
            request_id,
            payload_hash,
            swarm_id,
            "resume_requested",
            actor,
            row["state"],
            result,
        )
        return result

    return db._write(operation)


def _finish_admission(
    db: SwarmDatabase, swarm_id: str, request_id: str, kind: str, runs: list[Json]
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        scope = "start" if kind == "start" else f"resume:{swarm_id}"
        row = connection.execute(
            "SELECT outcome FROM requests WHERE scope=? AND request_id=?",
            (scope, request_id),
        ).fetchone()
        if row is None:
            raise SwarmStoreError("request_conflict")
        outcome = _load(row["outcome"])
        if outcome["swarm_id"] != swarm_id:
            raise SwarmStoreError("request_conflict")
        if "runs" not in outcome:
            outcome["runs"] = runs
            outcome["state"] = connection.execute(
                "SELECT state FROM swarms WHERE id=?", (swarm_id,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE requests SET outcome=? WHERE scope=? AND request_id=?",
                (_dump(outcome), scope, request_id),
            )
        return outcome

    return db._write(operation)


def _bind_execution_epoch(
    db: SwarmDatabase, swarm_id: str, expected_epoch: int, execution_epoch: str
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        row = connection.execute(
            "SELECT execution_epoch FROM swarm_execution_epochs WHERE swarm_id=? AND epoch=?",
            (swarm_id, expected_epoch),
        ).fetchone()
        if row is not None:
            if row["execution_epoch"] != execution_epoch:
                raise SwarmStoreError("execution_epoch_conflict")
            return {
                "swarm_id": swarm_id,
                "epoch": expected_epoch,
                "execution_epoch": execution_epoch,
                "replayed": True,
            }
        connection.execute(
            "INSERT INTO swarm_execution_epochs(swarm_id,epoch,execution_epoch) VALUES(?,?,?)",
            (swarm_id, expected_epoch, execution_epoch),
        )
        return {
            "swarm_id": swarm_id,
            "epoch": expected_epoch,
            "execution_epoch": execution_epoch,
        }

    return db._write(operation)


def _bind_participant_session(db: SwarmDatabase, binding: TemporarySessionBinding) -> None:
    def operation(connection: sqlite3.Connection) -> None:
        participant = connection.execute(
            "SELECT swarm_id FROM participants WHERE id=?", (binding.participant_id,)
        ).fetchone()
        if participant is None or str(participant["swarm_id"]) != binding.group_id:
            raise SwarmStoreError("participant_not_found")
        connection.execute(
            "INSERT INTO participant_sessions(participant_id,project_id,agent_id,session_id,generation_id,owner_name) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(participant_id) DO NOTHING",
            (
                binding.participant_id,
                binding.address.project_id,
                binding.address.agent_id,
                binding.address.session_id,
                binding.generation_id,
                binding.owner_name,
            ),
        )
        existing = connection.execute(
            "SELECT project_id,agent_id,session_id,generation_id,owner_name FROM participant_sessions WHERE participant_id=?",
            (binding.participant_id,),
        ).fetchone()
        expected = (
            binding.address.project_id,
            binding.address.agent_id,
            binding.address.session_id,
            binding.generation_id,
            binding.owner_name,
        )
        if existing is None or tuple(existing) != expected:
            raise SwarmStoreError("participant_binding_conflict")

    db._write(operation)
