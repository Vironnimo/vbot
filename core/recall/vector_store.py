"""SQLite-vec vector store for Passage-level semantic Recall.

The store is a **disposable derived index**; canonical Session history stays in
``<data_dir>/sessions.db``. This module opens the connection, pins the
embedding-space identity and observed dimension in a singleton header, and
reconciles each Session's stored Passages incrementally.

Every Passage is one metadata row in ``passages`` and one row in the ``vec0``
virtual table sharing its rowid. ``indexed_sessions`` holds one freshness stamp
per indexed Session, including Sessions that yield no Passages. A refresh is a
two-phase operation around the asynchronous embedding call: ``plan_refresh``
diffs the target Passages against the stored rows and names the texts that have
no stored vector in the pinned space; ``apply_refresh`` then deletes vanished
rows, inserts new ones, and advances the stamps in one write transaction.
Unchanged rows keep their rowids and vectors. The schema is versioned through
``PRAGMA user_version``; a mismatched file is discarded and rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

from core.database import required_journal_mode
from core.recall.passages import Passage

_INDEX_DIR_NAME = "recall"
_INDEX_FILE_NAME = "session_passage_vectors.sqlite"
_SQLITE_BUSY_TIMEOUT_MS = 1000
# Bump when the on-disk index becomes invalid under a new build/index policy;
# mismatched indexes are dropped and rebuilt (the index is disposable, no migration).
# v2 → chunk-keyed metadata (one row per chunk, not per session).
# v3 → empty-text chunks (e.g. run_summary-only windows) are no longer indexed;
#      older indexes hold constant-vector noise rows that must be purged.
# v4 → chunk keys are project-scoped (``project_id`` column) so the same session
#      UUID under a project vs. the global scope never collides in the index.
# v5 → vec0 carries scope, Session, and time metadata so structural filters run
#      inside KNN instead of starving an eligible scope after global retrieval.
# v6 → a singleton header pins the complete embedding-space fingerprint and
#      index policy, preventing cross-connection/options/policy vector reuse.
# v7 → the header also pins the provider-reported model id, preventing a router
#      alias or fallback from mixing vectors produced by different real models.
# v8 → canonical Session generations/revisions replace filesystem freshness
#      and prevent recreated addresses from reusing stale chunks.
# v9 → invalid chunk timestamps use unbounded interval endpoints instead of
#      1970, preserving fail-open time-filter behavior for malformed metadata.
# v10 → Passage rows carry a text hash for incremental reuse, per-Session
#       freshness stamps move to ``indexed_sessions``, and the duplicate
#       chunk-era columns are gone.
_SCHEMA_VERSION = 10
_VECTOR_TABLE_NAME = "session_vectors"
_PASSAGE_TABLE_NAME = "passages"
_SESSION_TABLE_NAME = "indexed_sessions"
_HEADER_TABLE_NAME = "store_header"
_UNBOUNDED_START_TIMESTAMP_MICROS = -(2**63)
_UNBOUNDED_END_TIMESTAMP_MICROS = 2**63 - 1
# Stay well below SQLite's bound-parameter limit in ``IN (...)`` lists.
_SQL_PARAMETER_CHUNK = 500
# vec0 rejects a KNN query with more than about 16 constraints; MATCH, k, scope
# and time filters use up to five, so at most this many ``!=`` exclusions are
# pushed down individually before exclusions become an allowlist.
_MAX_PUSHED_EXCLUSIONS = 8

SessionVersion = tuple[str, int]


class VectorStoreError(RuntimeError):
    """Raised when the vector store cannot complete a recoverable operation."""


@dataclass(frozen=True)
class VectorHeader:
    """Header row pinning the binding identity that produced the stored vectors."""

    provider_id: str
    model_id: str
    dimension: int
    space_fingerprint: str = ""
    index_policy: str = ""
    response_model_id: str = ""


@dataclass(frozen=True)
class StoredPassage:
    """One indexed Passage row with its exact canonical boundaries and text."""

    session_id: str
    passage_id: str
    text: str
    start_message_id: str
    end_message_id: str
    start_timestamp: str
    end_timestamp: str
    start_role: str
    end_role: str


@dataclass(frozen=True)
class SessionPassages:
    """The complete target Passage set of one Session at one canonical version."""

    session_id: str
    version: SessionVersion
    previous_version: SessionVersion | None
    passages: tuple[Passage, ...]


@dataclass(frozen=True)
class _SessionChange:
    session_id: str
    version: SessionVersion
    previous_version: SessionVersion | None
    delete_rowids: tuple[int, ...]
    inserts: tuple[Passage, ...]


@dataclass(frozen=True)
class RefreshPlan:
    """Row-level changes that bring the planned Sessions to their target Passages.

    ``texts_to_embed`` lists each text without a stored vector exactly once;
    ``apply_refresh`` expects one vector per entry, in this order.
    """

    header: VectorHeader
    header_present: bool
    agent_id: str
    project_id: str
    pruned_session_ids: tuple[str, ...]
    changes: tuple[_SessionChange, ...]
    reused_vectors: Mapping[str, bytes]
    texts_to_embed: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.pruned_session_ids and not self.changes


class VectorStore:
    """sqlite-vec backed Passage store keyed by rowid."""

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
        try:
            connection.row_factory = sqlite3.Row
            connection.enable_load_extension(True)
            try:
                sqlite_vec.load(connection)
            finally:
                connection.enable_load_extension(False)
            connection.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            journal_mode = required_journal_mode(sqlite3.sqlite_version_info)
            connection.execute(f"PRAGMA journal_mode={journal_mode.upper()}")
            connection.execute("PRAGMA synchronous=NORMAL")
            return connection
        except Exception as error:
            connection.close()
            if isinstance(error, VectorStoreError):
                raise
            raise VectorStoreError(f"could not open vector store: {error}") from error

    @staticmethod
    def _create_tables(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_PASSAGE_TABLE_NAME} (
              rowid INTEGER PRIMARY KEY,
              project_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              passage_id TEXT NOT NULL,
              text_hash TEXT NOT NULL,
              text TEXT NOT NULL,
              start_message_id TEXT NOT NULL,
              end_message_id TEXT NOT NULL,
              start_timestamp TEXT NOT NULL,
              end_timestamp TEXT NOT NULL,
              start_role TEXT NOT NULL,
              end_role TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_passages_session
              ON {_PASSAGE_TABLE_NAME}(project_id, agent_id, session_id)
            """
        )
        connection.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_passages_text_hash
              ON {_PASSAGE_TABLE_NAME}(text_hash)
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_SESSION_TABLE_NAME} (
              project_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              generation_id TEXT NOT NULL,
              history_revision INTEGER NOT NULL,
              PRIMARY KEY (project_id, agent_id, session_id)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_HEADER_TABLE_NAME} (
              singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
              provider_id TEXT NOT NULL,
              model_id TEXT NOT NULL,
              response_model_id TEXT NOT NULL,
              space_fingerprint TEXT NOT NULL,
              index_policy TEXT NOT NULL,
              dimension INTEGER NOT NULL,
              schema_version INTEGER NOT NULL
            )
            """
        )

    @staticmethod
    def _drop_schema(connection: sqlite3.Connection) -> None:
        for table in (
            _VECTOR_TABLE_NAME,
            _PASSAGE_TABLE_NAME,
            _SESSION_TABLE_NAME,
            _HEADER_TABLE_NAME,
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")

    def _initialize_schema(
        self,
        connection: sqlite3.Connection,
        *,
        expected_header: VectorHeader,
    ) -> None:
        """Create the schema for *expected_header*, discarding any other index."""

        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != _SCHEMA_VERSION:
            self._drop_schema(connection)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._create_tables(connection)
        existing = self._read_header(connection)
        if existing == expected_header and self._has_table(connection, _VECTOR_TABLE_NAME):
            return
        if existing is not None or self._has_table(connection, _VECTOR_TABLE_NAME):
            # A different header or a vector table without its committed
            # header is not comparable/complete; rebuild the disposable schema.
            self._drop_schema(connection)
            self._create_tables(connection)
        self._create_vector_table(connection, expected_header.dimension)
        connection.execute(
            f"""
            INSERT INTO {_HEADER_TABLE_NAME} (
              singleton, provider_id, model_id, response_model_id,
              space_fingerprint, index_policy, dimension, schema_version
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expected_header.provider_id,
                expected_header.model_id,
                expected_header.response_model_id,
                expected_header.space_fingerprint,
                expected_header.index_policy,
                expected_header.dimension,
                _SCHEMA_VERSION,
            ),
        )

    @classmethod
    def _read_header(cls, connection: sqlite3.Connection) -> VectorHeader | None:
        # On a brand-new database the header table does not exist yet.
        if not cls._has_table(connection, _HEADER_TABLE_NAME):
            return None
        row = connection.execute(
            f"""
            SELECT provider_id, model_id, response_model_id,
                   space_fingerprint, index_policy, dimension
            FROM {_HEADER_TABLE_NAME} WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            return None
        return VectorHeader(
            provider_id=str(row["provider_id"]),
            model_id=str(row["model_id"]),
            response_model_id=str(row["response_model_id"]),
            dimension=int(row["dimension"]),
            space_fingerprint=str(row["space_fingerprint"]),
            index_policy=str(row["index_policy"]),
        )

    @classmethod
    def _read_current_header(cls, connection: sqlite3.Connection) -> VectorHeader | None:
        """Read the header, rejecting a populated file from another schema version."""

        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != _SCHEMA_VERSION:
            populated = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone()
            if populated is not None:
                raise VectorStoreError(
                    f"vector store schema {version} does not match {_SCHEMA_VERSION}"
                )
            return None
        return cls._read_header(connection)

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
              scope_key TEXT partition key,
              session_id TEXT,
              start_timestamp INTEGER,
              end_timestamp INTEGER,
              embedding float[{dimension}] distance_metric=cosine
            )
            """
        )

    # ------------------------------------------------------------------
    # Incremental refresh
    # ------------------------------------------------------------------

    def list_indexed_sessions(
        self, agent_id: str, project_id: str = ""
    ) -> dict[str, SessionVersion]:
        """Return ``{session_id: (generation_id, history_revision)}`` for one scope.

        ``project_id`` is the scope key (``""`` for identity/global) so two
        scopes' same-UUID Sessions stay distinct freshness entries.
        """

        with closing(self._connect()) as connection:
            if self._read_current_header(connection) is None:
                return {}
            return self._read_stamps(connection, agent_id, project_id)

    @staticmethod
    def _read_stamps(
        connection: sqlite3.Connection,
        agent_id: str | None,
        project_id: str,
    ) -> dict[str, SessionVersion]:
        """Read the freshness stamps of one scope, or of every scope without an Agent."""

        query = f"SELECT session_id, generation_id, history_revision FROM {_SESSION_TABLE_NAME}"
        parameters: tuple[str, ...] = ()
        if agent_id is not None:
            query += " WHERE project_id = ? AND agent_id = ?"
            parameters = (project_id, agent_id)
        return {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in connection.execute(query, parameters).fetchall()
        }

    def plan_refresh(
        self,
        agent_id: str,
        project_id: str,
        *,
        header: VectorHeader,
        sessions: Sequence[SessionPassages],
        pruned_session_ids: Iterable[str] = (),
    ) -> RefreshPlan:
        """Diff target Passages against stored rows without writing.

        A stored row is kept only when its Passage id, text hash, boundary
        Message ids, timestamps and roles all match a target Passage; matching
        is multiset-aware. New rows reuse any stored vector for the same text in
        the pinned embedding space, and every other distinct text is listed in
        ``texts_to_embed``.
        """

        if header.dimension <= 0:
            raise VectorStoreError("refresh requires a header with a resolved dimension")
        with closing(self._connect()) as connection:
            stored_header = self._read_current_header(connection)
            if stored_header is not None and stored_header != header:
                raise VectorStoreError(
                    "vector store header does not match the embedding space of this refresh"
                )
            header_present = stored_header is not None
            changes: list[_SessionChange] = []
            insert_texts: dict[str, str] = {}
            for target in sessions:
                existing = (
                    self._stored_row_keys(connection, project_id, agent_id, target.session_id)
                    if header_present
                    else {}
                )
                inserts: list[Passage] = []
                for passage in target.passages:
                    rowids = existing.get(_passage_key(passage))
                    if rowids:
                        rowids.pop()
                        continue
                    inserts.append(passage)
                    insert_texts.setdefault(_text_hash(passage.text), passage.text)
                changes.append(
                    _SessionChange(
                        session_id=target.session_id,
                        version=target.version,
                        previous_version=target.previous_version,
                        delete_rowids=tuple(
                            sorted(rowid for rowids in existing.values() for rowid in rowids)
                        ),
                        inserts=tuple(inserts),
                    )
                )
            reused = (
                self._vectors_for_text_hashes(connection, insert_texts) if header_present else {}
            )
        return RefreshPlan(
            header=header,
            header_present=header_present,
            agent_id=agent_id,
            project_id=project_id,
            pruned_session_ids=tuple(sorted(set(pruned_session_ids))),
            changes=tuple(changes),
            reused_vectors=reused,
            texts_to_embed=tuple(
                text for text_hash, text in insert_texts.items() if text_hash not in reused
            ),
        )

    def apply_refresh(
        self,
        plan: RefreshPlan,
        vectors: Sequence[Sequence[float]] = (),
    ) -> None:
        """Apply *plan* in one write transaction, with one vector per text to embed.

        A Session whose stored stamp no longer equals the planned previous
        version was refreshed by another writer since planning and is skipped.
        """

        if len(vectors) != len(plan.texts_to_embed):
            raise VectorStoreError(
                f"refresh received {len(vectors)} vectors for {len(plan.texts_to_embed)} texts"
            )
        header = plan.header
        embedded: dict[str, bytes] = {}
        for text, vector in zip(plan.texts_to_embed, vectors, strict=True):
            if len(vector) != header.dimension:
                raise VectorStoreError(
                    f"vector length {len(vector)} does not match pinned dimension "
                    f"{header.dimension} for model {header.provider_id}/{header.model_id}"
                )
            embedded[_text_hash(text)] = sqlite_vec.serialize_float32([float(v) for v in vector])
        available = {**plan.reused_vectors, **embedded}
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                stored_header = self._read_current_header(connection)
                if stored_header is not None and stored_header != header:
                    raise VectorStoreError("vector store header changed during refresh")
                if plan.header_present and stored_header is None:
                    raise VectorStoreError("vector store was discarded during refresh")
                if stored_header is not None and plan.is_empty:
                    connection.rollback()
                    return
                self._initialize_schema(connection, expected_header=header)
                for session_id in plan.pruned_session_ids:
                    self._delete_session_rows(
                        connection, plan.agent_id, plan.project_id, session_id
                    )
                for change in plan.changes:
                    self._apply_session_change(connection, plan, change, vectors=available)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _apply_session_change(
        self,
        connection: sqlite3.Connection,
        plan: RefreshPlan,
        change: _SessionChange,
        *,
        vectors: Mapping[str, bytes],
    ) -> None:
        scope = (plan.project_id, plan.agent_id, change.session_id)
        stamp = connection.execute(
            f"""
            SELECT generation_id, history_revision FROM {_SESSION_TABLE_NAME}
            WHERE project_id = ? AND agent_id = ? AND session_id = ?
            """,
            scope,
        ).fetchone()
        current = None if stamp is None else (str(stamp[0]), int(stamp[1]))
        if current != change.previous_version:
            return
        self._delete_rowids(connection, change.delete_rowids)
        scope_key = _scope_key(plan.project_id, plan.agent_id)
        for passage in change.inserts:
            text_hash = _text_hash(passage.text)
            cursor = connection.execute(
                f"""
                INSERT INTO {_PASSAGE_TABLE_NAME} (
                  project_id, agent_id, session_id, passage_id, text_hash, text,
                  start_message_id, end_message_id, start_timestamp, end_timestamp,
                  start_role, end_role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *scope,
                    passage.passage_id,
                    text_hash,
                    passage.text,
                    passage.start_message_id,
                    passage.end_message_id,
                    passage.start_timestamp,
                    passage.end_timestamp,
                    passage.start_role,
                    passage.end_role,
                ),
            )
            if cursor.lastrowid is None:
                raise VectorStoreError(f"failed to insert Passage for {change.session_id}")
            connection.execute(
                f"""
                INSERT INTO {_VECTOR_TABLE_NAME} (
                  rowid, scope_key, session_id, start_timestamp, end_timestamp, embedding
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cursor.lastrowid,
                    scope_key,
                    change.session_id,
                    _timestamp_micros(
                        passage.start_timestamp, fallback=_UNBOUNDED_START_TIMESTAMP_MICROS
                    ),
                    _timestamp_micros(
                        passage.end_timestamp, fallback=_UNBOUNDED_END_TIMESTAMP_MICROS
                    ),
                    vectors[text_hash],
                ),
            )
        connection.execute(
            f"""
            INSERT INTO {_SESSION_TABLE_NAME} (
              project_id, agent_id, session_id, generation_id, history_revision
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (project_id, agent_id, session_id) DO UPDATE SET
              generation_id = excluded.generation_id,
              history_revision = excluded.history_revision
            """,
            (*scope, *change.version),
        )

    @staticmethod
    def _stored_row_keys(
        connection: sqlite3.Connection,
        project_id: str,
        agent_id: str,
        session_id: str,
    ) -> dict[tuple[str, ...], list[int]]:
        rows = connection.execute(
            f"""
            SELECT rowid, passage_id, text_hash, start_message_id, end_message_id,
                   start_timestamp, end_timestamp, start_role, end_role
            FROM {_PASSAGE_TABLE_NAME}
            WHERE project_id = ? AND agent_id = ? AND session_id = ?
            """,
            (project_id, agent_id, session_id),
        ).fetchall()
        keys: dict[tuple[str, ...], list[int]] = {}
        for row in rows:
            key = tuple(str(value) for value in tuple(row)[1:])
            keys.setdefault(key, []).append(int(row["rowid"]))
        return keys

    @staticmethod
    def _vectors_for_text_hashes(
        connection: sqlite3.Connection,
        text_hashes: Iterable[str],
    ) -> dict[str, bytes]:
        """Copy one stored vector per requested text hash, from any Session."""

        wanted = list(text_hashes)
        vectors: dict[str, bytes] = {}
        for start in range(0, len(wanted), _SQL_PARAMETER_CHUNK):
            chunk = wanted[start : start + _SQL_PARAMETER_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT p.text_hash, v.embedding
                FROM {_PASSAGE_TABLE_NAME} AS p
                JOIN {_VECTOR_TABLE_NAME} AS v ON v.rowid = p.rowid
                WHERE p.text_hash IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for row in rows:
                vectors.setdefault(str(row[0]), bytes(row[1]))
        return vectors

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete_session(self, agent_id: str, project_id: str, session_id: str) -> None:
        """Remove one Session's rows and freshness stamp (delete-time cleanup)."""

        with closing(self._connect()) as connection, connection:
            # A file from another schema holds nothing current; the next
            # search discards and rebuilds it.
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION or self._read_header(connection) is None:
                return
            self._delete_session_rows(connection, agent_id, project_id, session_id)

    @classmethod
    def _delete_session_rows(
        cls,
        connection: sqlite3.Connection,
        agent_id: str,
        project_id: str,
        session_id: str,
    ) -> None:
        scope = (project_id, agent_id, session_id)
        rowids = [
            int(row[0])
            for row in connection.execute(
                f"SELECT rowid FROM {_PASSAGE_TABLE_NAME} "
                "WHERE project_id = ? AND agent_id = ? AND session_id = ?",
                scope,
            )
        ]
        cls._delete_rowids(connection, rowids)
        connection.execute(
            f"DELETE FROM {_SESSION_TABLE_NAME} "
            "WHERE project_id = ? AND agent_id = ? AND session_id = ?",
            scope,
        )

    @staticmethod
    def _delete_rowids(connection: sqlite3.Connection, rowids: Sequence[int]) -> None:
        if not rowids:
            return
        # vec0 resolves only ``rowid = ?`` as a point lookup; ``IN`` scans the table.
        connection.executemany(
            f"DELETE FROM {_VECTOR_TABLE_NAME} WHERE rowid = ?",
            ((rowid,) for rowid in rowids),
        )
        for start in range(0, len(rowids), _SQL_PARAMETER_CHUNK):
            chunk = rowids[start : start + _SQL_PARAMETER_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM {_PASSAGE_TABLE_NAME} WHERE rowid IN ({placeholders})",
                chunk,
            )

    # ------------------------------------------------------------------
    # KNN query
    # ------------------------------------------------------------------

    def knn_search(
        self,
        *,
        header: VectorHeader,
        query_vector: Sequence[float],
        limit: int,
        agent_id: str | None = None,
        project_id: str = "",
        session_ids: Collection[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[tuple[StoredPassage, float]]:
        """Return the nearest Passages after structural prefilters inside KNN.

        ``session_ids`` names the Sessions whose Passages may match; ``None``
        admits every Session of the scope. Every scope, Session and time filter
        runs inside the vec0 KNN, so filtered rows never take a slot of the
        ``limit`` nearest. KNN and row hydration run as one statement, so every
        returned vector has its Passage row.
        """

        if limit <= 0:
            return []
        if len(query_vector) != header.dimension:
            raise VectorStoreError(
                f"query vector length {len(query_vector)} does not match pinned dimension "
                f"{header.dimension} for model {header.provider_id}/{header.model_id}"
            )
        allowed = None if session_ids is None else set(session_ids)
        if allowed is not None and not allowed:
            return []
        clauses = ["embedding MATCH ?", "k = ?"]
        parameters: list[object] = [
            sqlite_vec.serialize_float32([float(value) for value in query_vector]),
            limit,
        ]
        if agent_id is not None:
            clauses.append("scope_key = ?")
            parameters.append(_scope_key(project_id, agent_id))
        if since is not None:
            clauses.append("end_timestamp >= ?")
            parameters.append(_datetime_micros(since))
        if until is not None:
            clauses.append("start_timestamp <= ?")
            parameters.append(_datetime_micros(until))
        with closing(self._connect()) as connection:
            stored = self._read_current_header(connection)
            if stored is None or stored != header:
                raise VectorStoreError(
                    "vector store header is missing or does not match the requested embedding space"
                )
            if not self._has_table(connection, _VECTOR_TABLE_NAME):
                raise VectorStoreError("vector store table is missing")
            if allowed is not None and not self._filter_sessions(
                connection, clauses, parameters, agent_id, project_id, allowed
            ):
                return []
            rows = connection.execute(
                f"""
                WITH knn AS (
                  SELECT rowid, distance FROM {_VECTOR_TABLE_NAME}
                  WHERE {" AND ".join(clauses)}
                )
                SELECT p.session_id, p.passage_id, p.text, p.start_message_id,
                       p.end_message_id, p.start_timestamp, p.end_timestamp,
                       p.start_role, p.end_role, knn.distance
                FROM knn JOIN {_PASSAGE_TABLE_NAME} AS p ON p.rowid = knn.rowid
                ORDER BY knn.distance, p.session_id, p.passage_id
                """,
                parameters,
            ).fetchall()
        return [
            (
                StoredPassage(
                    session_id=str(row["session_id"]),
                    passage_id=str(row["passage_id"]),
                    text=str(row["text"]),
                    start_message_id=str(row["start_message_id"]),
                    end_message_id=str(row["end_message_id"]),
                    start_timestamp=str(row["start_timestamp"]),
                    end_timestamp=str(row["end_timestamp"]),
                    start_role=str(row["start_role"]),
                    end_role=str(row["end_role"]),
                ),
                float(row["distance"]),
            )
            for row in rows
        ]

    @classmethod
    def _filter_sessions(
        cls,
        connection: sqlite3.Connection,
        clauses: list[str],
        parameters: list[object],
        agent_id: str | None,
        project_id: str,
        allowed: set[str],
    ) -> bool:
        """Add the KNN constraint admitting only *allowed* Sessions.

        vec0 applies ``NOT IN`` only to the k nearest rows, which would starve
        the page, and rejects a query with more than about 16 constraints. A
        few indexed Sessions outside *allowed* become ``!=`` constraints; more
        become an ``IN`` allowlist read from one JSON parameter. Returns
        ``False`` when no allowed Session has indexed Passages.
        """

        if len(allowed) == 1:
            clauses.append("session_id = ?")
            parameters.append(next(iter(allowed)))
            return True
        indexed = set(cls._read_stamps(connection, agent_id, project_id))
        excluded = indexed - allowed
        if len(excluded) <= _MAX_PUSHED_EXCLUSIONS:
            for excluded_id in sorted(excluded):
                clauses.append("session_id != ?")
                parameters.append(excluded_id)
            return bool(indexed & allowed)
        admitted = sorted(indexed & allowed)
        if not admitted:
            return False
        clauses.append("session_id IN (SELECT value FROM json_each(?))")
        parameters.append(json.dumps(admitted))
        return True

    # ------------------------------------------------------------------
    # Header / lifecycle
    # ------------------------------------------------------------------

    def read_header(self) -> VectorHeader | None:
        """Read the pinned header; a populated file from another schema is an error."""

        with closing(self._connect()) as connection:
            return self._read_current_header(connection)

    def reset_index(self) -> None:
        """Discard the exact derived-index files without opening a corrupt database."""

        for path in (
            self.index_path,
            Path(f"{self.index_path}-wal"),
            Path(f"{self.index_path}-shm"),
            Path(f"{self.index_path}-journal"),
        ):
            path.unlink(missing_ok=True)

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )


def _passage_key(passage: Passage) -> tuple[str, ...]:
    """Identity of a stored row; the order matches ``_stored_row_keys``."""

    return (
        passage.passage_id,
        _text_hash(passage.text),
        passage.start_message_id,
        passage.end_message_id,
        passage.start_timestamp,
        passage.end_timestamp,
        passage.start_role,
        passage.end_role,
    )


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope_key(project_id: str, agent_id: str) -> str:
    return f"{project_id}\0{agent_id}"


def _timestamp_micros(value: str, *, fallback: int) -> int:
    if not value:
        return fallback
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return fallback
    return _datetime_micros(parsed)


def _datetime_micros(value: datetime) -> int:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(normalized.timestamp() * 1_000_000)
