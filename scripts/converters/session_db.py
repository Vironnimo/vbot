"""Narrow offline inspection and recovery commands for canonical Sessions."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.sessions import SessionAddress, SessionStore
from core.sessions.schema import SCHEMA_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "inspect"):
        command = subcommands.add_parser(name)
        command.add_argument("data_dir", type=Path)
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
    print(f"schema_version: {version}")
    print(f"integrity: {integrity}")
    print(f"foreign_key_errors: {len(foreign_keys)}")
    print(f"live_sessions: {live}")
    print(f"archived_sessions: {archived}")
    print(f"messages: {messages}")
    return 0 if version == SCHEMA_VERSION and integrity == "ok" and not foreign_keys else 1


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


if __name__ == "__main__":
    sys.exit(main())
