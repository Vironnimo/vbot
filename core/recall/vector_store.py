"""SQLite-vec vector store for per-session semantic recall.

The store is a **disposable derived index** — canonical Session storage
stays in JSONL under ``<data_dir>/agents/<agent-id>/sessions/``. This
module is responsible for opening the connection, observing the embedding
dimension lazily, pinning ``(provider_id, model_id, dimension)`` in a
header, and exposing vector upsert/lookup primitives that the recall
backend (``core/recall/vector.py``) builds on top of.

The on-disk file lives at ``<data_dir>/recall/session_vectors.sqlite``;
the ``sqlite-vec`` extension is loaded via the same enable/disable dance
as the rest of the project's SQLite work, and the index is
schema-versioned through ``PRAGMA user_version`` so a mismatched index
is dropped and rebuilt on the next open.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

_INDEX_DIR_NAME = "recall"
_INDEX_FILE_NAME = "session_vectors.sqlite"
_SQLITE_BUSY_TIMEOUT_MS = 1000
# Bump when the on-disk index schema changes; mismatched indexes are dropped and rebuilt.
_SCHEMA_VERSION = 1
_VECTOR_TABLE_NAME = "session_vectors"
_METADATA_TABLE_NAME = "sessions"
_HEADER_TABLE_NAME = "store_header"
# Reservation for safety so a session with N context_window tokens still leaves
# room for model-specific tokenization overhead (system prompt wrappers, etc.).
_CONTEXT_WINDOW_CHAR_RESERVE = 4
# Conservative default when the bound model is not in the local registry.
_DEFAULT_INPUT_CHARS = 32_000

# Cosine distance range — distances are 0 (identical direction) to 2 (opposite).
# ``overshoot`` is the additional candidate count we ask sqlite-vec for so
# structural filters applied after KNN still leave us with ``limit`` hits.
_KNN_OVERSHOOT = 4


class VectorStoreError(RuntimeError):
    """Raised when the vector store cannot complete a recoverable operation."""


@dataclass(frozen=True)
class VectorHeader:
    """Header row pinning the binding identity that produced the stored vectors."""

    provider_id: str
    model_id: str
    dimension: int


@dataclass(frozen=True)
class SessionVectorRecord:
    """One indexed session row — anchor metadata and the live mtime/size it was indexed at."""

    session_id: str
    agent_id: str
    started_at: str
    mtime_ns: int
    size_bytes: int
    anchor_message_id: str
    snippet: str


class VectorStore:
    """sqlite-vec backed store keyed by session rowid."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.index_path = data_dir / _INDEX_DIR_NAME / _INDEX_FILE_NAME

    @property
    def path(self) -> Path:
        """The on-disk SQLite file path."""

        return self.index_path

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.index_path)
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        connection.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as error:
            # WAL can fail on read-only or shared mounts — non-fatal, store
            # still works (slower). The error path is logged by the caller.
            raise VectorStoreError(f"could not enable WAL for vector store: {error}") from error
        return connection

    def _initialize_schema(
        self,
        connection: sqlite3.Connection,
        *,
        expected_header: VectorHeader,
    ) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != _SCHEMA_VERSION:
            connection.executescript(
                f"""
                DROP TABLE IF EXISTS {_VECTOR_TABLE_NAME};
                DROP TABLE IF EXISTS {_METADATA_TABLE_NAME};
                DROP TABLE IF EXISTS {_HEADER_TABLE_NAME};
                """
            )
        connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {_HEADER_TABLE_NAME} (
              provider_id TEXT NOT NULL,
              model_id TEXT NOT NULL,
              dimension INTEGER NOT NULL,
              schema_version INTEGER NOT NULL,
              PRIMARY KEY (provider_id, model_id)
            );

            CREATE TABLE IF NOT EXISTS {_METADATA_TABLE_NAME} (
              rowid INTEGER PRIMARY KEY,
              session_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              started_at TEXT NOT NULL,
              mtime_ns INTEGER NOT NULL,
              size_bytes INTEGER NOT NULL,
              anchor_message_id TEXT NOT NULL,
              snippet TEXT NOT NULL,
              UNIQUE (agent_id, session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_agent
              ON {_METADATA_TABLE_NAME}(agent_id);
            """
        )
        if version != _SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

        self._ensure_header(connection, expected_header)

    def _ensure_header(
        self,
        connection: sqlite3.Connection,
        expected_header: VectorHeader,
    ) -> None:
        """Write the header row if missing; drop & rebuild if the bound model changed."""

        existing = self._read_header(connection)
        if existing is not None and self._header_matches(existing, expected_header):
            return
        if existing is not None and not self._header_matches(existing, expected_header):
            # Bound model changed — vectors from the previous space are
            # not comparable; drop the whole index and start clean.
            connection.executescript(
                f"""
                DROP TABLE IF EXISTS {_VECTOR_TABLE_NAME};
                DROP TABLE IF EXISTS {_METADATA_TABLE_NAME};
                DELETE FROM {_HEADER_TABLE_NAME};
                """
            )
            connection.executescript(
                f"""
                CREATE TABLE {_METADATA_TABLE_NAME} (
                  rowid INTEGER PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  mtime_ns INTEGER NOT NULL,
                  size_bytes INTEGER NOT NULL,
                  anchor_message_id TEXT NOT NULL,
                  snippet TEXT NOT NULL,
                  UNIQUE (agent_id, session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_agent
                  ON {_METADATA_TABLE_NAME}(agent_id);
                """
            )
        self._create_vector_table(connection, expected_header.dimension)
        connection.execute(
            f"""
            INSERT INTO {_HEADER_TABLE_NAME} (
              provider_id, model_id, dimension, schema_version
            ) VALUES (?, ?, ?, ?)
            """,
            (
                expected_header.provider_id,
                expected_header.model_id,
                expected_header.dimension,
                _SCHEMA_VERSION,
            ),
        )

    @staticmethod
    def _header_matches(stored: VectorHeader, expected: VectorHeader) -> bool:
        return (
            stored.provider_id == expected.provider_id
            and stored.model_id == expected.model_id
            and stored.dimension == expected.dimension
        )

    @staticmethod
    def _read_header(connection: sqlite3.Connection) -> VectorHeader | None:
        # Defensive: on a brand-new database the header table does not exist
        # yet, so we look it up through ``sqlite_master`` before querying it.
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_HEADER_TABLE_NAME,),
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            f"SELECT provider_id, model_id, dimension FROM {_HEADER_TABLE_NAME} LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return VectorHeader(
            provider_id=str(row["provider_id"]),
            model_id=str(row["model_id"]),
            dimension=int(row["dimension"]),
        )

    @staticmethod
    def _create_vector_table(connection: sqlite3.Connection, dimension: int) -> None:
        if dimension <= 0:
            raise VectorStoreError(
                f"refusing to create vec0 table with non-positive dimension: {dimension}"
            )
        # ``vec0`` requires a fixed dimension at create time — we observe it from
        # the first embedding and pin it for the lifetime of this index.
        connection.execute(
            f"""
            CREATE VIRTUAL TABLE {_VECTOR_TABLE_NAME}
            USING vec0(
              embedding float[{dimension}] distance_metric=cosine
            )
            """
        )

    # ------------------------------------------------------------------
    # Session upsert / delete
    # ------------------------------------------------------------------

    def upsert_session(
        self,
        *,
        header: VectorHeader,
        record: SessionVectorRecord,
        vector: Sequence[float],
    ) -> None:
        """Insert or replace one session row + its vector."""

        if len(vector) != header.dimension:
            raise VectorStoreError(
                f"vector length {len(vector)} does not match pinned dimension "
                f"{header.dimension} for model {header.provider_id}/{header.model_id}"
            )
        vector_json = json.dumps([float(value) for value in vector])
        with closing(self._connect()) as connection:
            self._initialize_schema(connection, expected_header=header)
            with connection:
                self._delete_session_rows(connection, record.agent_id, record.session_id)
                cursor = connection.execute(
                    f"""
                    INSERT INTO {_METADATA_TABLE_NAME} (
                      session_id, agent_id, started_at, mtime_ns, size_bytes,
                      anchor_message_id, snippet
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.session_id,
                        record.agent_id,
                        record.started_at,
                        record.mtime_ns,
                        record.size_bytes,
                        record.anchor_message_id,
                        record.snippet,
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is None:
                    raise VectorStoreError(
                        f"failed to insert metadata for session {record.session_id}"
                    )
                connection.execute(
                    f"INSERT INTO {_VECTOR_TABLE_NAME}(rowid, embedding) VALUES (?, vec_f32(?))",
                    (row_id, vector_json),
                )

    def delete_session(self, agent_id: str, session_id: str) -> None:
        """Remove a session row and its vector (used for staleness cleanup)."""

        with closing(self._connect()) as connection, connection:
            self._delete_session_rows(connection, agent_id, session_id)

    def upsert_many_sessions(
        self,
        *,
        header: VectorHeader,
        records: Iterable[tuple[SessionVectorRecord, Sequence[float]]],
    ) -> int:
        """Bulk-upsert sessions; returns the number of rows written."""

        count = 0
        with closing(self._connect()) as connection:
            self._initialize_schema(connection, expected_header=header)
            with connection:
                for record, vector in records:
                    if len(vector) != header.dimension:
                        raise VectorStoreError(
                            f"vector length {len(vector)} does not match pinned dimension "
                            f"{header.dimension} for model "
                            f"{header.provider_id}/{header.model_id}"
                        )
                    self._delete_session_rows(connection, record.agent_id, record.session_id)
                    cursor = connection.execute(
                        f"""
                        INSERT INTO {_METADATA_TABLE_NAME} (
                          session_id, agent_id, started_at, mtime_ns, size_bytes,
                          anchor_message_id, snippet
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.session_id,
                            record.agent_id,
                            record.started_at,
                            record.mtime_ns,
                            record.size_bytes,
                            record.anchor_message_id,
                            record.snippet,
                        ),
                    )
                    row_id = cursor.lastrowid
                    if row_id is None:
                        raise VectorStoreError(
                            f"failed to insert metadata for session {record.session_id}"
                        )
                    connection.execute(
                        "INSERT INTO "
                        f"{_VECTOR_TABLE_NAME}(rowid, embedding) VALUES (?, vec_f32(?))",
                        (row_id, json.dumps([float(value) for value in vector])),
                    )
                    count += 1
        return count

    @staticmethod
    def _delete_session_rows(
        connection: sqlite3.Connection,
        agent_id: str,
        session_id: str,
    ) -> None:
        rows = list(
            connection.execute(
                f"""
                SELECT rowid FROM {_METADATA_TABLE_NAME}
                WHERE agent_id = ? AND session_id = ?
                """,
                (agent_id, session_id),
            )
        )
        for row in rows:
            connection.execute(
                f"DELETE FROM {_VECTOR_TABLE_NAME} WHERE rowid = ?",
                (int(row["rowid"]),),
            )
        connection.execute(
            f"DELETE FROM {_METADATA_TABLE_NAME} WHERE agent_id = ? AND session_id = ?",
            (agent_id, session_id),
        )

    # ------------------------------------------------------------------
    # Freshness + KNN query
    # ------------------------------------------------------------------

    def list_indexed_sessions(self, agent_id: str) -> dict[str, tuple[int, int]]:
        """Return ``{session_id: (mtime_ns, size_bytes)}`` for indexed sessions of one agent."""

        with closing(self._connect()) as connection:
            if not self._has_metadata_table(connection):
                return {}
            rows = connection.execute(
                f"""
                SELECT session_id, mtime_ns, size_bytes FROM {_METADATA_TABLE_NAME}
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchall()
        return {
            str(row["session_id"]): (int(row["mtime_ns"]), int(row["size_bytes"])) for row in rows
        }

    def drop_indexed_sessions(self, agent_id: str, session_ids: Iterable[str]) -> int:
        """Remove indexed session rows that no longer exist in JSONL."""

        removed = 0
        with closing(self._connect()) as connection, connection:
            for session_id in sorted(set(session_ids)):
                rows_before = connection.execute(
                    f"""
                        SELECT rowid FROM {_METADATA_TABLE_NAME}
                        WHERE agent_id = ? AND session_id = ?
                        """,
                    (agent_id, session_id),
                ).fetchall()
                if not rows_before:
                    continue
                for row in rows_before:
                    connection.execute(
                        f"DELETE FROM {_VECTOR_TABLE_NAME} WHERE rowid = ?",
                        (int(row["rowid"]),),
                    )
                connection.execute(
                    f"""
                        DELETE FROM {_METADATA_TABLE_NAME}
                        WHERE agent_id = ? AND session_id = ?
                        """,
                    (agent_id, session_id),
                )
                removed += 1
        return removed

    def knn_search(
        self,
        *,
        header: VectorHeader,
        query_vector: Sequence[float],
        limit: int,
    ) -> list[tuple[int, float]]:
        """Return up to ``limit + overshoot`` nearest rows by cosine distance."""

        if limit <= 0:
            return []
        if len(query_vector) != header.dimension:
            raise VectorStoreError(
                f"query vector length {len(query_vector)} does not match pinned dimension "
                f"{header.dimension} for model {header.provider_id}/{header.model_id}"
            )
        vector_json = json.dumps([float(value) for value in query_vector])
        fetch_limit = limit + _KNN_OVERSHOOT
        with closing(self._connect()) as connection:
            self._initialize_schema(connection, expected_header=header)
            rows = connection.execute(
                f"""
                SELECT rowid, distance FROM {_VECTOR_TABLE_NAME}
                WHERE embedding MATCH vec_f32(?)
                ORDER BY distance
                LIMIT ?
                """,
                (vector_json, fetch_limit),
            ).fetchall()
        return [(int(row["rowid"]), float(row["distance"])) for row in rows]

    def get_sessions_by_rowids(self, row_ids: Iterable[int]) -> dict[int, SessionVectorRecord]:
        """Hydrate metadata rows for the given vec0 rowids, keyed by rowid."""

        ids = [int(value) for value in row_ids]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with closing(self._connect()) as connection:
            if not self._has_metadata_table(connection):
                return {}
            rows = connection.execute(
                f"""
                SELECT rowid, session_id, agent_id, started_at, mtime_ns, size_bytes,
                       anchor_message_id, snippet
                FROM {_METADATA_TABLE_NAME}
                WHERE rowid IN ({placeholders})
                """,
                ids,
            ).fetchall()
        return {
            int(row["rowid"]): SessionVectorRecord(
                session_id=str(row["session_id"]),
                agent_id=str(row["agent_id"]),
                started_at=str(row["started_at"]),
                mtime_ns=int(row["mtime_ns"]),
                size_bytes=int(row["size_bytes"]),
                anchor_message_id=str(row["anchor_message_id"]),
                snippet=str(row["snippet"]),
            )
            for row in rows
        }

    def has_header(self) -> bool:
        """Return whether the on-disk index already has a header row."""

        with closing(self._connect()) as connection:
            return self._read_header(connection) is not None

    def read_header(self) -> VectorHeader | None:
        """Public read of the stored header — used by tests to assert pinning."""

        with closing(self._connect()) as connection:
            return self._read_header(connection)

    def drop_index(self) -> None:
        """Wipe the on-disk index — used when the bound model changes."""

        with closing(self._connect()) as connection:
            connection.executescript(
                f"""
                DROP TABLE IF EXISTS {_VECTOR_TABLE_NAME};
                DROP TABLE IF EXISTS {_METADATA_TABLE_NAME};
                DROP TABLE IF EXISTS {_HEADER_TABLE_NAME};
                """
            )
            connection.execute("PRAGMA user_version = 0")
            connection.commit()

    @staticmethod
    def _has_metadata_table(connection: sqlite3.Connection) -> bool:
        """Whether the metadata table exists (helps the read path on a fresh DB)."""

        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_METADATA_TABLE_NAME,),
            ).fetchone()
            is not None
        )

    # ------------------------------------------------------------------
    # Truncation
    # ------------------------------------------------------------------

    @staticmethod
    def truncate_to_input_limit(text: str, *, context_window: int | None) -> str:
        """Truncate text to ``context_window / _CONTEXT_WINDOW_CHAR_RESERVE`` characters.

        Embedding models typically charge one token per ~4 characters of
        English text, so the input character budget is ``context_window // 4``
        — we divide by 4 to leave a safety margin for non-English text and
        provider-side overhead. When the model is not in the registry, we
        fall back to a conservative default character budget.
        """

        if context_window is None or context_window <= 0:
            limit = _DEFAULT_INPUT_CHARS
        else:
            limit = max(1, context_window // _CONTEXT_WINDOW_CHAR_RESERVE)
        if len(text) <= limit:
            return text
        return text[:limit]


def format_started_at(timestamp: str | datetime | None) -> str:
    """Normalize a started-at timestamp to the ISO format the store persists."""

    if timestamp is None:
        return datetime.now(UTC).isoformat()
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC).isoformat()
        return timestamp.astimezone(UTC).isoformat()
    return str(timestamp)
