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
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from core.extensions.databases import Database

from ._store_values import (
    SwarmStoreError,
    _dump,
    _load,
)


class SwarmDatabase:
    """Swarm operations on the Extension's kernel database handle.

    The Extension host owns the handle's lifetime: it opens the database for
    the current registration and closes it after shutdown. Each write runs in
    one kernel write transaction and each read in one kernel read transaction,
    so a read sees one consistent state without holding up writers.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._cursor_key: str | None = None

    async def run(self, function: Callable[..., Any], *arguments: Any) -> Any:
        """Run ``function(self, *arguments)`` on the database's own worker pool.

        After the host closes the database it raises the kernel's
        ``DatabaseUnavailableError``.
        """
        return await self._database.run_async(function, self, *arguments)

    def _open(self) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT value FROM swarm_meta WHERE key='cursor_key'"
            ).fetchone()
        if row is None:

            def create_key(connection: sqlite3.Connection) -> str:
                connection.execute(
                    "INSERT OR IGNORE INTO swarm_meta(key, value) VALUES ('cursor_key', ?)",
                    (secrets.token_hex(32),),
                )
                return str(
                    connection.execute(
                        "SELECT value FROM swarm_meta WHERE key='cursor_key'"
                    ).fetchone()[0]
                )

            self._cursor_key = self._database.write(create_key)
        else:
            self._cursor_key = str(row[0])

    def _close(self) -> None:
        self._cursor_key = None

    def _write(self, function: Callable[[sqlite3.Connection], Any]) -> Any:
        self._require_open()
        return self._database.write(function)

    def _read(self) -> AbstractContextManager[sqlite3.Connection]:
        self._require_open()
        return self._database.read()

    def _require_open(self) -> str:
        if self._cursor_key is None:
            raise RuntimeError("SwarmStore is not open")
        return self._cursor_key

    def _make_cursor(self, kind: str, scope: str, high_water: int | None, offset: int) -> str:
        data = _dump({"k": kind, "s": scope, "h": high_water, "o": offset})
        key = self._require_open()
        mac = hmac.new(key.encode(), data.encode(), hashlib.sha256).digest()
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
            key = self._require_open()
            if not hmac.compare_digest(mac, hmac.new(key.encode(), data, hashlib.sha256).digest()):
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


DATABASE_NAME = "swarm"

# The Swarm database schema, opened through ``host.open_database``. Changes stay
# additive (new tables, indexes, and nullable or defaulted columns); state and
# kind values are validated in code, never by CHECK constraints.
#
# Every index names its reader:
# - ``wiki_revision_page``: one page's revision history, the latest revision of
#   each page for the page list, and the revision high-water mark
#   (``_store_wiki.wiki``).
# - ``recipients_pending_participant``: a participant's undelivered Posts
#   (``_store_records._pending_rows`` and ``_pending_count``, and
#   ``_store_delivery._prepare_automatic_delivery``, which names it with
#   ``INDEXED BY``), so delivered history does not dominate the scan.
# - ``posts_discussion_page``: the Post pages of one Discussion
#   (``_store_reads._post_page`` and ``_human_post_page``, which name it with
#   ``INDEXED BY`` so SQLite does not walk the whole Swarm's Posts by sequence)
#   and its Post high-water mark (``_store_records._post_high_water``).
# - ``discussions_one_main``: enforces one main Discussion per Swarm and serves
#   the main-Discussion lookup (``_store_records._main``).
# Discussion pages read through the ``UNIQUE(swarm_id,sequence)`` constraint.
SCHEMA_SQL = """
CREATE TABLE swarm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT;
CREATE TABLE profiles(id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,name TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL) STRICT;
CREATE TABLE swarms(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,profile_snapshot TEXT NOT NULL,effective_configuration TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL) STRICT;
CREATE TABLE participants(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),model TEXT NOT NULL,display_name TEXT NOT NULL,ordinal INTEGER NOT NULL,state TEXT NOT NULL,idle_boundary INTEGER,wake_announced_seq INTEGER NOT NULL DEFAULT 0,wake_epoch INTEGER NOT NULL DEFAULT 0,wake_pending INTEGER NOT NULL DEFAULT 0,wake_pending_seq INTEGER NOT NULL DEFAULT 0,lifecycle_run_id TEXT,UNIQUE(swarm_id,ordinal),UNIQUE(swarm_id,display_name COLLATE NOCASE)) STRICT;
CREATE TABLE participant_sessions(participant_id TEXT PRIMARY KEY REFERENCES participants(id),project_id TEXT,agent_id TEXT NOT NULL,session_id TEXT NOT NULL,generation_id TEXT NOT NULL,owner_name TEXT NOT NULL) STRICT;
CREATE TABLE swarm_settings(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),revision INTEGER NOT NULL,delivery_json TEXT NOT NULL) STRICT;
CREATE TABLE swarm_epochs(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),epoch INTEGER NOT NULL,is_open INTEGER NOT NULL CHECK(is_open IN(0,1))) STRICT;
CREATE TABLE swarm_execution_epochs(swarm_id TEXT NOT NULL REFERENCES swarms(id),epoch INTEGER NOT NULL,execution_epoch TEXT NOT NULL,PRIMARY KEY(swarm_id,epoch)) STRICT;
CREATE TABLE swarm_events(id INTEGER PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),kind TEXT NOT NULL,actor TEXT NOT NULL,old_json TEXT,new_json TEXT,settings_revision INTEGER,created_at TEXT NOT NULL) STRICT;
CREATE TABLE discussions(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),title TEXT NOT NULL,sequence INTEGER NOT NULL,is_main INTEGER NOT NULL CHECK(is_main IN(0,1)),created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE memberships(discussion_id TEXT NOT NULL REFERENCES discussions(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(discussion_id,participant_id)) STRICT;
CREATE TABLE posts(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),discussion_id TEXT NOT NULL REFERENCES discussions(id),sequence INTEGER NOT NULL,author_kind TEXT NOT NULL,author_id TEXT NOT NULL,author_name TEXT NOT NULL,text TEXT NOT NULL,reply_to TEXT,recipients_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE recipients(post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),route_class TEXT NOT NULL,delivered_at TEXT,receipt_id TEXT,content_hash TEXT,effect_kind TEXT,carrier_kind TEXT,carrier_sequence INTEGER,prepared_at TEXT,PRIMARY KEY(post_id,participant_id)) STRICT;
CREATE TABLE delivery_batches(receipt_id TEXT PRIMARY KEY,participant_id TEXT NOT NULL REFERENCES participants(id),content_hash TEXT NOT NULL,effect_kind TEXT NOT NULL,created_at TEXT NOT NULL,acknowledged_at TEXT,carrier_kind TEXT,carrier_sequence INTEGER,settings_revision INTEGER) STRICT;
CREATE TABLE delivery_batch_entries(receipt_id TEXT NOT NULL REFERENCES delivery_batches(receipt_id),post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(receipt_id,post_id,participant_id)) STRICT;
CREATE TABLE requests(scope TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,outcome TEXT NOT NULL,PRIMARY KEY(scope,request_id)) STRICT;
CREATE TABLE swarm_goals(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),post_id TEXT NOT NULL REFERENCES posts(id)) STRICT;
CREATE TABLE wiki_pages(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),revision INTEGER NOT NULL) STRICT;
CREATE TABLE wiki_revisions(id INTEGER PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),page_id TEXT NOT NULL REFERENCES wiki_pages(id),revision INTEGER NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,deleted INTEGER NOT NULL CHECK(deleted IN(0,1)),author_id TEXT NOT NULL,author_name TEXT NOT NULL,author_kind TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(page_id,revision)) STRICT;
CREATE INDEX wiki_revision_page ON wiki_revisions(swarm_id,page_id,id DESC);
CREATE INDEX recipients_pending_participant ON recipients(participant_id,post_id) WHERE delivered_at IS NULL;
CREATE INDEX posts_discussion_page ON posts(swarm_id,discussion_id,sequence DESC);
CREATE UNIQUE INDEX discussions_one_main ON discussions(swarm_id) WHERE is_main=1;
"""

# Reads name their columns and never use ``SELECT *``: an older vBot must not
# pick up columns a newer one adds. Keep these lists equal to the tables above.
SWARM_COLUMNS = "id,prompt,profile_snapshot,effective_configuration,state,created_at"
PARTICIPANT_COLUMNS = "id,swarm_id,model,display_name,ordinal,state,idle_boundary,wake_announced_seq,wake_epoch,wake_pending,wake_pending_seq,lifecycle_run_id"
DISCUSSION_COLUMNS = "id,swarm_id,title,sequence,is_main,created_at"
POST_COLUMNS = "id,swarm_id,discussion_id,sequence,author_kind,author_id,author_name,text,reply_to,recipients_json,created_at"
WIKI_PAGE_COLUMNS = "id,swarm_id,revision"
WIKI_REVISION_COLUMNS = "id,swarm_id,page_id,revision,title,content,deleted,author_id,author_name,author_kind,created_at"


def qualified(columns: str, alias: str) -> str:
    """``columns`` prefixed with a table alias, for joined reads."""
    return ",".join(f"{alias}.{name}" for name in columns.split(","))


ALIASED_DISCUSSION_COLUMNS = qualified(DISCUSSION_COLUMNS, "d")
ALIASED_POST_COLUMNS = qualified(POST_COLUMNS, "p")
ALIASED_WIKI_REVISION_COLUMNS = qualified(WIKI_REVISION_COLUMNS, "r")
