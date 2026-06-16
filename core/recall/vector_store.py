"""SQLite-vec vector store for per-chunk semantic recall.

The store is a **disposable derived index** — canonical Session storage
stays in JSONL under ``<data_dir>/agents/<agent-id>/sessions/``. This
module is responsible for opening the connection, observing the embedding
dimension lazily, pinning ``(provider_id, model_id, dimension)`` in a
header, and exposing chunk-level vector upsert/lookup primitives that the
recall backend (``core/recall/vector.py``) builds on top of.

A session's searchable text is split into one or more **chunks**
(message-aware windows with overlap — see ``core/recall/vector.py`` for
the chunking policy). Each chunk is its own row in the metadata table
and its own row in the ``vec0`` virtual table, keyed by the chunk's
``(agent_id, session_id, chunk_index)`` tuple. The on-disk file lives at
``<data_dir>/recall/session_vectors.sqlite``; the ``sqlite-vec``
extension is loaded via the same enable/disable dance as the rest of
the project's SQLite work, and the index is schema-versioned through
``PRAGMA user_version`` so a mismatched index is dropped and rebuilt
on the next open.
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
# Bump when the on-disk index becomes invalid under a new build/index policy;
# mismatched indexes are dropped and rebuilt (the index is disposable, no migration).
# v2 → chunk-keyed metadata (one row per chunk, not per session).
# v3 → empty-text chunks (e.g. run_summary-only windows) are no longer indexed;
#      older indexes hold constant-vector noise rows that must be purged.
_SCHEMA_VERSION = 3
_VECTOR_TABLE_NAME = "session_vectors"
_CHUNK_TABLE_NAME = "chunks"
_HEADER_TABLE_NAME = "store_header"
# Character-budget heuristic for capping a session's text before embedding.
# Embedding models bill ~1 token per N characters; English averages ~4, but
# German (compound words, umlauts) and code tokenize denser — observed ~3.9 and
# as low as ~3 — so we assume 3 to stay safely under the model's token cap for
# mixed-language sessions. A pure character heuristic cannot match the model's
# tokenizer exactly; see FLAGGED.md for the residual overflow risk.
_CHARS_PER_TOKEN = 3
# Fraction of the token window we actually fill, leaving headroom for the
# heuristic's error and any provider-side request wrapping.
_INPUT_TOKEN_SAFETY = 0.9
# Conservative default token window when the bound model is not in the local
# registry. 8192 is the cap for bge-m3, the OpenAI embedding models, and most
# OpenRouter embedding models, so we assume that floor.
_DEFAULT_CONTEXT_WINDOW = 8192

# Cosine distance range — distances are 0 (identical direction) to 2 (opposite).
# ``overshoot`` is the additional candidate count we ask sqlite-vec for so
# structural filters applied after KNN still leave us with ``limit`` hits.
# The recall backend over-fetches further for chunk→session dedup.
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
class ChunkVectorRecord:
    """One indexed chunk row — anchor metadata and the live mtime/size it was indexed at.

    The metadata describes a *chunk* of a session (a window of consecutive
    messages), not the session as a whole. ``mtime_ns`` and ``size_bytes``
    are still the session's, copied onto every chunk row so the freshness
    diff (``list_indexed_sessions``) stays a session-level check.
    """

    session_id: str
    agent_id: str
    started_at: str
    mtime_ns: int
    size_bytes: int
    anchor_message_id: str
    snippet: str
    chunk_index: int
    start_message_id: str
    end_message_id: str


class VectorStore:
    """sqlite-vec backed store keyed by chunk rowid."""

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

    def _chunk_table_ddl(self) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {_CHUNK_TABLE_NAME} (
              rowid INTEGER PRIMARY KEY,
              session_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              started_at TEXT NOT NULL,
              mtime_ns INTEGER NOT NULL,
              size_bytes INTEGER NOT NULL,
              anchor_message_id TEXT NOT NULL,
              snippet TEXT NOT NULL,
              chunk_index INTEGER NOT NULL,
              start_message_id TEXT NOT NULL,
              end_message_id TEXT NOT NULL,
              UNIQUE (agent_id, session_id, chunk_index)
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_agent
              ON {_CHUNK_TABLE_NAME}(agent_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_agent_session
              ON {_CHUNK_TABLE_NAME}(agent_id, session_id);
            """

    @staticmethod
    def _header_table_ddl() -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {_HEADER_TABLE_NAME} (
              provider_id TEXT NOT NULL,
              model_id TEXT NOT NULL,
              dimension INTEGER NOT NULL,
              schema_version INTEGER NOT NULL,
              PRIMARY KEY (provider_id, model_id)
            );
            """

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
                DROP TABLE IF EXISTS {_CHUNK_TABLE_NAME};
                DROP TABLE IF EXISTS {_HEADER_TABLE_NAME};
                """
            )
        connection.executescript(self._chunk_table_ddl())
        connection.executescript(self._header_table_ddl())
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
                DROP TABLE IF EXISTS {_CHUNK_TABLE_NAME};
                DELETE FROM {_HEADER_TABLE_NAME};
                """
            )
            connection.executescript(self._chunk_table_ddl())
            connection.executescript(self._header_table_ddl())
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
    # Chunk upsert / delete
    # ------------------------------------------------------------------

    def delete_session(self, agent_id: str, session_id: str) -> None:
        """Remove all chunk rows for an agent+session (used for staleness cleanup)."""

        with closing(self._connect()) as connection, connection:
            # On a fresh index the chunk table does not exist yet — there is
            # nothing to delete, and querying it would raise ``no such table``.
            if not self._has_chunk_table(connection):
                return
            self._delete_session_rows(connection, agent_id, session_id)

    def upsert_many_chunks(
        self,
        *,
        header: VectorHeader,
        records: Iterable[tuple[ChunkVectorRecord, Sequence[float]]],
    ) -> int:
        """Bulk-upsert chunks; replaces all chunks of any touched session.

        Each distinct ``(agent_id, session_id)`` seen in *records* has its
        existing chunk rows deleted **once** before the new chunks are
        inserted. Per-row delete is unsafe here: deleting chunk 1 between
        inserting chunk 0 and chunk 2 would clobber chunk 0 via the
        session-wide delete. We collect the distinct session set up front
        so every session is wiped exactly once, then insert the new
        chunks in a single pass. Returns the number of chunk rows
        written.
        """

        materialized = [(record, vector) for record, vector in records]
        if not materialized:
            return 0
        count = 0
        with closing(self._connect()) as connection:
            self._initialize_schema(connection, expected_header=header)
            with connection:
                # Delete each session's existing chunks exactly once.
                distinct_sessions = {
                    (record.agent_id, record.session_id) for record, _ in materialized
                }
                for agent_id, session_id in distinct_sessions:
                    self._delete_session_rows(connection, agent_id, session_id)
                for record, vector in materialized:
                    if len(vector) != header.dimension:
                        raise VectorStoreError(
                            f"vector length {len(vector)} does not match pinned dimension "
                            f"{header.dimension} for model "
                            f"{header.provider_id}/{header.model_id}"
                        )
                    cursor = connection.execute(
                        f"""
                        INSERT INTO {_CHUNK_TABLE_NAME} (
                          session_id, agent_id, started_at, mtime_ns, size_bytes,
                          anchor_message_id, snippet, chunk_index,
                          start_message_id, end_message_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.session_id,
                            record.agent_id,
                            record.started_at,
                            record.mtime_ns,
                            record.size_bytes,
                            record.anchor_message_id,
                            record.snippet,
                            record.chunk_index,
                            record.start_message_id,
                            record.end_message_id,
                        ),
                    )
                    row_id = cursor.lastrowid
                    if row_id is None:
                        raise VectorStoreError(
                            f"failed to insert chunk for session {record.session_id} "
                            f"chunk_index {record.chunk_index}"
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
        # Deletes *all* chunk rows for an (agent, session) — re-used for
        # re-indexing, staleness drops, and the pre-insert wipe in
        # ``upsert_many_chunks``. Any matching vec0 rowids are removed
        # first so the vec0 table never dangles.
        rows = list(
            connection.execute(
                f"""
                SELECT rowid FROM {_CHUNK_TABLE_NAME}
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
            f"DELETE FROM {_CHUNK_TABLE_NAME} WHERE agent_id = ? AND session_id = ?",
            (agent_id, session_id),
        )

    # ------------------------------------------------------------------
    # Freshness + KNN query
    # ------------------------------------------------------------------

    def list_indexed_sessions(self, agent_id: str) -> dict[str, tuple[int, int]]:
        """Return ``{session_id: (mtime_ns, size_bytes)}`` for indexed sessions of one agent.

        With chunk-keyed storage, a session has one row per chunk; we
        dedup to one entry per ``session_id`` (every chunk row of a
        session shares the session's ``mtime_ns``/``size_bytes``).
        """

        with closing(self._connect()) as connection:
            if not self._has_chunk_table(connection):
                return {}
            rows = connection.execute(
                f"""
                SELECT session_id, mtime_ns, size_bytes FROM {_CHUNK_TABLE_NAME}
                WHERE agent_id = ?
                GROUP BY session_id
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
            if not self._has_chunk_table(connection):
                return 0
            for session_id in sorted(set(session_ids)):
                rows_before = connection.execute(
                    f"""
                        SELECT rowid FROM {_CHUNK_TABLE_NAME}
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
                        DELETE FROM {_CHUNK_TABLE_NAME}
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

    def get_chunks_by_rowids(self, row_ids: Iterable[int]) -> dict[int, ChunkVectorRecord]:
        """Hydrate chunk metadata rows for the given vec0 rowids, keyed by rowid."""

        ids = [int(value) for value in row_ids]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with closing(self._connect()) as connection:
            if not self._has_chunk_table(connection):
                return {}
            rows = connection.execute(
                f"""
                SELECT rowid, session_id, agent_id, started_at, mtime_ns, size_bytes,
                       anchor_message_id, snippet, chunk_index, start_message_id, end_message_id
                FROM {_CHUNK_TABLE_NAME}
                WHERE rowid IN ({placeholders})
                """,
                ids,
            ).fetchall()
        return {
            int(row["rowid"]): ChunkVectorRecord(
                session_id=str(row["session_id"]),
                agent_id=str(row["agent_id"]),
                started_at=str(row["started_at"]),
                mtime_ns=int(row["mtime_ns"]),
                size_bytes=int(row["size_bytes"]),
                anchor_message_id=str(row["anchor_message_id"]),
                snippet=str(row["snippet"]),
                chunk_index=int(row["chunk_index"]),
                start_message_id=str(row["start_message_id"]),
                end_message_id=str(row["end_message_id"]),
            )
            for row in rows
        }

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
                DROP TABLE IF EXISTS {_CHUNK_TABLE_NAME};
                DROP TABLE IF EXISTS {_HEADER_TABLE_NAME};
                """
            )
            connection.execute("PRAGMA user_version = 0")
            connection.commit()

    @staticmethod
    def _has_chunk_table(connection: sqlite3.Connection) -> bool:
        """Whether the chunk metadata table exists (helps the read path on a fresh DB)."""

        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_CHUNK_TABLE_NAME,),
            ).fetchone()
            is not None
        )

    # ------------------------------------------------------------------
    # Truncation
    # ------------------------------------------------------------------

    @staticmethod
    def truncate_to_input_limit(text: str, *, context_window: int | None) -> str:
        """Cap *text* to a character budget safely under the model's token window.

        The budget is ``context_window * _INPUT_TOKEN_SAFETY * _CHARS_PER_TOKEN``
        characters. ``_CHARS_PER_TOKEN`` is a conservative chars-per-token
        estimate (German and code tokenize denser than English), and
        ``_INPUT_TOKEN_SAFETY`` reserves headroom so the heuristic's error does
        not push the request over the model's hard token cap. When the bound
        model's window is unknown, ``_DEFAULT_CONTEXT_WINDOW`` is assumed.
        """

        window = (
            context_window if context_window and context_window > 0 else _DEFAULT_CONTEXT_WINDOW
        )
        limit = max(1, int(window * _INPUT_TOKEN_SAFETY) * _CHARS_PER_TOKEN)
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
