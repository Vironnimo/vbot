"""Durable command checkpoints bound to the canonical Session identity.

The command owner interprets the opaque checkpoint. Sessions keep its binding
through moves, remove it with the Session, and check delivery against Notes in
that same Session, so a restart cannot duplicate an already persisted result.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.sessions._types import SessionAddress
from core.utils.timestamps import utc_now_timestamp


def put(
    connection: sqlite3.Connection,
    address: SessionAddress,
    command_id: str,
    origin_run_id: str,
    checkpoint: dict[str, Any],
) -> None:
    previous = connection.execute(
        "SELECT checkpoint_json FROM session_background_commands WHERE command_id = ?",
        (command_id,),
    ).fetchone()
    if previous is not None:
        connection.execute(
            "UPDATE session_background_commands SET checkpoint_json = ?, updated_at = ? "
            "WHERE command_id = ?",
            (
                json.dumps(json.loads(previous[0]) | checkpoint, ensure_ascii=False),
                utc_now_timestamp(),
                command_id,
            ),
        )
        return
    # Do not resurrect a removed Session or rebind an existing command to a
    # replacement that reuses its address. A Session move keeps session_key.
    connection.execute(
        "INSERT INTO session_background_commands "
        "(command_id, session_key, origin_run_id, checkpoint_json, updated_at) "
        "SELECT ?, s.session_key, ?, ?, ? FROM sessions AS s "
        "WHERE s.project_id = ? AND s.agent_id = ? AND s.session_id = ? AND s.state = 'live' "
        "ON CONFLICT(command_id) DO NOTHING",
        (
            command_id,
            origin_run_id,
            json.dumps(checkpoint, ensure_ascii=False),
            utc_now_timestamp(),
            address.project_id or "",
            address.agent_id,
            address.session_id,
        ),
    )


def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "command_id": str(row["command_id"]),
            "owner": SessionAddress(
                row["project_id"] or None, str(row["agent_id"]), str(row["session_id"])
            ),
            "origin_run_id": str(row["origin_run_id"]),
            "checkpoint": json.loads(row["checkpoint_json"]),
        }
        for row in connection.execute(
            "SELECT c.command_id, c.origin_run_id, c.checkpoint_json, "
            "s.project_id, s.agent_id, s.session_id "
            "FROM session_background_commands AS c "
            "JOIN sessions AS s ON s.session_key = c.session_key WHERE s.state = 'live'"
        )
    ]


def delete(connection: sqlite3.Connection, command_id: str) -> None:
    connection.execute(
        "DELETE FROM session_background_commands WHERE command_id = ?", (command_id,)
    )


def delivered(connection: sqlite3.Connection, command_id: str, body: str) -> bool:
    if not body:
        return False
    return (
        connection.execute(
            "SELECT 1 FROM session_background_commands AS c "
            "JOIN entries AS e ON e.session_key = c.session_key "
            "JOIN entry_text AS t ON t.entry_key = e.entry_key "
            "WHERE c.command_id = ? AND e.role = 'note' AND instr(t.content, ?) > 0 LIMIT 1",
            (command_id, body),
        ).fetchone()
        is not None
    )
