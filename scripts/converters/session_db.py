"""Narrow offline inspection and recovery commands for canonical Sessions."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.chat.messages import ChatMessage
from core.sessions import SessionAddress
from core.sessions.schema import APPLICATION_ID, SCHEMA_CONVERSION_FLOOR, SCHEMA_VERSION
from core.sessions.store import SessionStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "inspect", "compact"):
        command = subcommands.add_parser(name)
        command.add_argument("data_dir", type=Path)
    backup = subcommands.add_parser("backup")
    backup.add_argument("data_dir", type=Path)
    backup.add_argument("destination", type=Path)
    restore = subcommands.add_parser("restore")
    restore.add_argument("data_dir", type=Path)
    restore.add_argument("--agent-id", required=True)
    restore.add_argument("--session-id", required=True)
    restore.add_argument("--project-id")
    args = parser.parse_args(argv)
    path = args.data_dir.expanduser().resolve() / "sessions.db"
    if args.command == "verify":
        return _verify(path)
    if args.command == "inspect":
        return _inspect(path)
    if args.command == "backup":
        store = SessionStore(path)
        try:
            store.backup(args.destination)
        finally:
            store.close()
        print(f"backed up Session database to {args.destination}")
        return 0
    if args.command == "compact":
        with sqlite3.connect(path, isolation_level=None) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
        print("compacted Session database")
        return 0
    store = SessionStore(path)
    try:
        store.restore(SessionAddress(args.project_id, args.agent_id, args.session_id))
    finally:
        store.close()
    print("restored archived Session")
    return 0


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"Session database not found: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _verify(path: Path) -> int:
    with _connect_read_only(path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        live = int(
            connection.execute("SELECT COUNT(*) FROM sessions WHERE status = 'live'").fetchone()[0]
        )
        archived = int(
            connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE status = 'archived'"
            ).fetchone()[0]
        )
        messages = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        semantic_errors = _semantic_errors(connection)
    print(f"schema_version: {version}")
    print(f"application_id: {application_id}")
    print(f"integrity: {integrity}")
    print(f"foreign_key_errors: {len(foreign_keys)}")
    print(f"live_sessions: {live}")
    print(f"archived_sessions: {archived}")
    print(f"messages: {messages}")
    print(f"semantic_errors: {len(semantic_errors)}")
    for error in semantic_errors:
        print(f"error: {error}")
    return (
        0
        if SCHEMA_CONVERSION_FLOOR <= version <= SCHEMA_VERSION
        and application_id == APPLICATION_ID
        and integrity == "ok"
        and not foreign_keys
        and not semantic_errors
        else 1
    )


def _inspect(path: Path) -> int:
    with _connect_read_only(path) as connection:
        rows = connection.execute(
            "SELECT project_id, agent_id, session_id, status, history_revision, state_revision, "
            "created_at, last_message_at FROM sessions ORDER BY project_id, agent_id, session_id"
        ).fetchall()
    for row in rows:
        print(
            "\t".join(
                str(row[name])
                for name in (
                    "project_id",
                    "agent_id",
                    "session_id",
                    "status",
                    "history_revision",
                    "state_revision",
                    "created_at",
                    "last_message_at",
                )
            )
        )
    return 0


def _semantic_errors(connection: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    rows = connection.execute("SELECT * FROM sessions ORDER BY session_key").fetchall()
    for session in rows:
        key = int(session["session_key"])
        generation_id = session["generation_id"]
        if not isinstance(generation_id, str) or len(generation_id) != 32:
            errors.append(f"session_key {key}: invalid generation_id")
        messages = connection.execute(
            "SELECT * FROM messages WHERE session_key = ? ORDER BY seq", (key,)
        ).fetchall()
        if [int(message["seq"]) for message in messages] != list(range(len(messages))):
            errors.append(f"session_key {key}: message sequence is not dense")
        if int(session["message_count"]) != len(messages):
            errors.append(f"session_key {key}: message_count does not match rows")
        expected_last_id = messages[-1]["message_id"] if messages else None
        expected_last_at = messages[-1]["timestamp"] if messages else None
        if session["last_message_id"] != expected_last_id:
            errors.append(f"session_key {key}: last_message_id does not match")
        if session["last_message_at"] != expected_last_at:
            errors.append(f"session_key {key}: last_message_at does not match")
        for message in messages:
            try:
                decoded = ChatMessage.from_dict(json.loads(message["message_json"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                errors.append(f"session_key {key} seq {message['seq']}: invalid ChatMessage")
                continue
            if (
                decoded.id != message["message_id"]
                or decoded.role != message["role"]
                or decoded.timestamp != message["timestamp"]
            ):
                errors.append(f"session_key {key} seq {message['seq']}: projections differ")
        continuation = connection.execute(
            "SELECT seq FROM continuation_records WHERE session_key = ? ORDER BY seq", (key,)
        ).fetchall()
        if [int(record["seq"]) for record in continuation] != list(range(len(continuation))):
            errors.append(f"session_key {key}: continuation sequence is not dense")
    return errors


if __name__ == "__main__":
    sys.exit(main())
