"""Durable command checkpoints bound to the canonical Session identity.

The command owner interprets the opaque checkpoint. Sessions keep its binding
through moves, remove it with the Session, and check delivery against Notes in
that same Session, so a restart cannot duplicate an already persisted result.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from core.secrets.store import SecretStore
from core.sessions._types import SessionAddress
from core.utils.timestamps import utc_now_timestamp


def put(
    connection: sqlite3.Connection,
    address: SessionAddress,
    command_id: str,
    origin_run_id: str,
    checkpoint: dict[str, Any],
    *,
    generation_id: str,
    secrets: SecretStore,
) -> None:
    previous = connection.execute(
        "SELECT c.checkpoint_json, c.sealed_checkpoint FROM session_background_commands AS c "
        "JOIN sessions AS s ON s.session_key = c.session_key "
        "WHERE c.command_id = ? AND s.generation_id = ? AND s.state = 'live'",
        (command_id, generation_id),
    ).fetchone()
    if previous is not None:
        checkpoint = _checkpoint(secrets, command_id, previous) | checkpoint
    sealed = secrets.seal_data(
        json.dumps(checkpoint, ensure_ascii=False).encode("utf-8"),
        purpose="session-command-checkpoint/1",
        identity=command_id,
        create_store=False,
    )
    if previous is not None:
        connection.execute(
            "UPDATE session_background_commands SET checkpoint_json = '{}', "
            "sealed_checkpoint = ?, updated_at = ? "
            "WHERE command_id = ?",
            (
                sealed,
                utc_now_timestamp(),
                command_id,
            ),
        )
        return
    # Capture generation before process launch: even the first queued write
    # follows a move and can never bind to a replacement at the old address.
    connection.execute(
        "INSERT INTO session_background_commands "
        "(command_id, session_key, origin_run_id, checkpoint_json, sealed_checkpoint, updated_at) "
        "SELECT ?, s.session_key, ?, '{}', ?, ? FROM sessions AS s "
        "WHERE s.generation_id = ? AND s.state = 'live' "
        "ON CONFLICT(command_id) DO NOTHING",
        (
            command_id,
            origin_run_id,
            sealed,
            utc_now_timestamp(),
            generation_id,
        ),
    )


def _checkpoint(secrets: SecretStore, command_id: str, row: sqlite3.Row) -> dict[str, Any]:
    content = row["checkpoint_json"]
    if row["sealed_checkpoint"] is not None:
        content = secrets.open_data(
            row["sealed_checkpoint"], purpose="session-command-checkpoint/1", identity=command_id
        ).decode("utf-8")
    return cast(dict[str, Any], json.loads(content))


def read(connection: sqlite3.Connection, *, secrets: SecretStore) -> list[dict[str, Any]]:
    return [
        {
            "command_id": str(row["command_id"]),
            "owner": SessionAddress(
                row["project_id"] or None, str(row["agent_id"]), str(row["session_id"])
            ),
            "origin_run_id": str(row["origin_run_id"]),
            "generation_id": str(row["generation_id"]),
            "checkpoint": _checkpoint(secrets, str(row["command_id"]), row),
        }
        for row in connection.execute(
            "SELECT c.command_id, c.origin_run_id, c.checkpoint_json, c.sealed_checkpoint, "
            "s.project_id, s.agent_id, s.session_id, s.generation_id "
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
