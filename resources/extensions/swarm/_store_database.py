"""Store database."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.sessions.schema import required_journal_mode

from ._store_values import (
    SwarmStoreError,
    _dump,
    _load,
)


class SwarmDatabase:
    """Serialize operations on the Store's single SQLite connection."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def call(self, function: Callable[..., Any], *arguments: Any) -> Any:
        with self._lock:
            return function(self, *arguments)

    def _open(self) -> None:
        with self._lock:
            self._open_connection()

    def _open_connection(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False, timeout=1
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute(
            f"PRAGMA journal_mode={required_journal_mode(sqlite3.sqlite_version_info)}"
        )
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(_SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT value FROM swarm_meta WHERE key = 'cursor_key'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO swarm_meta(key, value) VALUES ('cursor_key', ?)",
                    (secrets.token_hex(32),),
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        self._connection = connection

    def _close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _write(self, function: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            return self._write_locked(function)

    def _write_locked(self, function: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            value = function(connection)
            connection.execute("COMMIT")
            return value
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SwarmStore is not open")
        return self._connection

    def _make_cursor(self, kind: str, scope: str, high_water: int | None, offset: int) -> str:
        data = _dump({"k": kind, "s": scope, "h": high_water, "o": offset})
        key = (
            self._require_connection()
            .execute("SELECT value FROM swarm_meta WHERE key='cursor_key'")
            .fetchone()[0]
        )
        mac = hmac.new(str(key).encode(), data.encode(), hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(data.encode()).decode()
            + "."
            + base64.urlsafe_b64encode(mac).decode()
        )

    def _cursor(
        self, cursor: str, kind: str, scope: str, high_water: int | None
    ) -> tuple[int, int | None]:
        try:
            encoded_data, encoded_mac = cursor.split(".", 1)
            data = base64.urlsafe_b64decode(encoded_data.encode())
            mac = base64.urlsafe_b64decode(encoded_mac.encode())
            if (
                base64.urlsafe_b64encode(data).decode() != encoded_data
                or base64.urlsafe_b64encode(mac).decode() != encoded_mac
            ):
                raise ValueError
            key = (
                self._require_connection()
                .execute("SELECT value FROM swarm_meta WHERE key='cursor_key'")
                .fetchone()[0]
            )
            if not hmac.compare_digest(
                mac, hmac.new(str(key).encode(), data, hashlib.sha256).digest()
            ):
                raise ValueError
            value = _load(data.decode())
            if (
                value.get("k") != kind
                or value.get("s") != scope
                or not isinstance(value.get("o"), int)
                or value["o"] < 0
                or (value.get("h") is not None and not isinstance(value["h"], int))
            ):
                raise ValueError
            return value["o"], value["h"]
        except (
            ValueError,
            KeyError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            binascii.Error,
        ) as error:
            raise SwarmStoreError("invalid_cursor") from error


_SCHEMA = """
CREATE TABLE IF NOT EXISTS swarm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,name TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarms(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,profile_snapshot TEXT NOT NULL,effective_configuration TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS participants(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),model TEXT NOT NULL,display_name TEXT NOT NULL,ordinal INTEGER NOT NULL,state TEXT NOT NULL,idle_boundary INTEGER,wake_announced_seq INTEGER NOT NULL DEFAULT 0,wake_epoch INTEGER NOT NULL DEFAULT 0,wake_pending INTEGER NOT NULL DEFAULT 0,wake_pending_seq INTEGER NOT NULL DEFAULT 0,lifecycle_run_id TEXT,UNIQUE(swarm_id,ordinal),UNIQUE(swarm_id,display_name COLLATE NOCASE)) STRICT;
CREATE TABLE IF NOT EXISTS participant_sessions(participant_id TEXT PRIMARY KEY REFERENCES participants(id),project_id TEXT,agent_id TEXT NOT NULL,session_id TEXT NOT NULL,generation_id TEXT NOT NULL,owner_name TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarm_settings(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),revision INTEGER NOT NULL,delivery_json TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarm_epochs(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),epoch INTEGER NOT NULL,is_open INTEGER NOT NULL CHECK(is_open IN(0,1))) STRICT;
CREATE TABLE IF NOT EXISTS swarm_execution_epochs(swarm_id TEXT NOT NULL REFERENCES swarms(id),epoch INTEGER NOT NULL,execution_epoch TEXT NOT NULL,PRIMARY KEY(swarm_id,epoch)) STRICT;
CREATE TABLE IF NOT EXISTS swarm_events(id INTEGER PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),kind TEXT NOT NULL,actor TEXT NOT NULL,old_json TEXT,new_json TEXT,settings_revision INTEGER,created_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS discussions(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),title TEXT NOT NULL,sequence INTEGER NOT NULL,is_main INTEGER NOT NULL CHECK(is_main IN(0,1)),created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE IF NOT EXISTS memberships(discussion_id TEXT NOT NULL REFERENCES discussions(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(discussion_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),discussion_id TEXT NOT NULL REFERENCES discussions(id),sequence INTEGER NOT NULL,author_kind TEXT NOT NULL,author_id TEXT NOT NULL,author_name TEXT NOT NULL,text TEXT NOT NULL,reply_to TEXT,recipients_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE IF NOT EXISTS recipients(post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),route_class TEXT NOT NULL,delivered_at TEXT,receipt_id TEXT,content_hash TEXT,effect_kind TEXT,carrier_kind TEXT,carrier_sequence INTEGER,prepared_at TEXT,PRIMARY KEY(post_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS delivery_batches(receipt_id TEXT PRIMARY KEY,participant_id TEXT NOT NULL REFERENCES participants(id),content_hash TEXT NOT NULL,effect_kind TEXT NOT NULL,created_at TEXT NOT NULL,acknowledged_at TEXT,carrier_kind TEXT,carrier_sequence INTEGER,settings_revision INTEGER) STRICT;
CREATE TABLE IF NOT EXISTS delivery_batch_entries(receipt_id TEXT NOT NULL REFERENCES delivery_batches(receipt_id),post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(receipt_id,post_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,outcome TEXT NOT NULL,PRIMARY KEY(scope,request_id)) STRICT;
CREATE INDEX IF NOT EXISTS posts_discussion_page ON posts(swarm_id,discussion_id,sequence DESC);
CREATE INDEX IF NOT EXISTS discussions_page ON discussions(swarm_id,is_main DESC,sequence);
CREATE UNIQUE INDEX IF NOT EXISTS discussions_one_main ON discussions(swarm_id) WHERE is_main=1;
"""
