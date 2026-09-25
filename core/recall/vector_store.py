"""sqlite-vec Passage vectors for semantic Recall.

``recall/session_passage_vectors.sqlite`` is a disposable projection opened
through the kernel (``core/database``). It holds the shared Passage catalog
(:mod:`core.recall._passage_catalog`), a singleton header pinning the embedding
space, one vector per Passage in a ``vec0`` table, and ``pending_vectors``, the
Passages still waiting for a vector.

Indexing has two independent steps:

- The catalog refresh is structural and complete for the request's candidates.
  A trigger queues every new Passage; one whose text already has a vector in
  the pinned space copies it at once and leaves the queue.
- Embedding drains the queue in batches outside any transaction. Storing a
  vector serves every queued Passage with the same text.

A search ranks only Passages that have a vector; the queued Passages among its
candidates are its coverage gap. A change of embedding space drops the vector
table and queues every Passage again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

from core.database import APPLICATION_IDS, DISPOSABLE, DatabaseCorruptError, DatabaseSpec
from core.recall._passage_catalog import (
    CATALOG_SCHEMA,
    VIEWED_BY_SESSIONS,
    Candidates,
    PassageCatalog,
    StoredPassage,
    candidate_refs,
    passage_columns,
    projection_version,
    refs_parameter,
    reported_sessions,
    stored_passage,
    time_bounds,
)
from core.utils.timestamps import parse_canonical_timestamp

_INDEX_DIR_NAME = "recall"
_INDEX_FILE_NAME = "session_passage_vectors.sqlite"
# Bump when the index tables or the meaning of their rows change; the kernel
# discards and rebuilds an index built for another version.
_LAYOUT_VERSION = 1
_VECTOR_TABLE = "passage_vectors"
_VECTOR_TRIGGER = "passages_drop_vector"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)

_VECTOR_SCHEMA = """
CREATE TABLE store_header (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  response_model_id TEXT NOT NULL,
  space_fingerprint TEXT NOT NULL,
  index_policy TEXT NOT NULL,
  dimension INTEGER NOT NULL CHECK (dimension > 0)
) STRICT;

CREATE TABLE pending_vectors (
  passage_ref INTEGER PRIMARY KEY
) STRICT;

CREATE TRIGGER passages_queue_vector AFTER INSERT ON passages BEGIN
  INSERT INTO pending_vectors (passage_ref) VALUES (new.passage_ref);
END;

CREATE TRIGGER passages_unqueue_vector AFTER DELETE ON passages BEGIN
  DELETE FROM pending_vectors WHERE passage_ref = old.passage_ref;
END;
"""


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


def _load_sqlite_vec(connection: sqlite3.Connection) -> None:
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)


def recall_vectors_database_spec(path: Path) -> DatabaseSpec:
    """Declare the disposable Passage vector index at ``path``."""
    return DatabaseSpec(
        name="recall_vectors",
        path=path,
        profile=DISPOSABLE,
        application_id=APPLICATION_IDS["recall_vectors"],
        format_generation=1,
        schema_sql=CATALOG_SCHEMA + _VECTOR_SCHEMA,
        projection_version=projection_version(_LAYOUT_VERSION),
        connection_setup=_load_sqlite_vec,
    )


class VectorStore(PassageCatalog):
    """The Passage catalog with sqlite-vec vectors in one pinned embedding space."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        super().__init__(
            recall_vectors_database_spec(data_dir / _INDEX_DIR_NAME / _INDEX_FILE_NAME)
        )

    async def read_header(self) -> VectorHeader | None:
        """Return the pinned embedding space; ``None`` before the first vector space.

        A pinned space without its vector table is damage the owner rebuilds.
        """
        return await self.read(_read_pinned_header)

    async def use_space(self, header: VectorHeader) -> bool:
        """Pin *header*'s embedding space; ``True`` when that dropped every vector.

        Another space's vectors are unusable, so its vector table is replaced
        and every stored Passage is queued for embedding again.
        """
        if header.dimension <= 0:
            raise VectorStoreError(f"refusing to pin non-positive dimension: {header.dimension}")
        return await self.write(lambda connection: _use_space(connection, header))

    def _after_add(self, connection: sqlite3.Connection, passage_refs: list[int]) -> None:
        # A new Passage whose text already has a vector reuses it instead of
        # waiting for the provider.
        if _read_header(connection) is None:
            return
        queued: dict[str, list[sqlite3.Row]] = {}
        for row in connection.execute(
            "SELECT p.passage_ref, p.project_id, p.agent_id, p.text_hash, p.start_timestamp, "
            "p.end_timestamp FROM pending_vectors AS q "
            "JOIN passages AS p ON p.passage_ref = q.passage_ref "
            "WHERE q.passage_ref IN (SELECT value FROM json_each(?))",
            (json.dumps(passage_refs),),
        ).fetchall():
            queued.setdefault(str(row["text_hash"]), []).append(row)
        for text_hash, rows in queued.items():
            embedding = _stored_embedding(connection, text_hash)
            if embedding is not None:
                _insert_vectors(connection, rows, embedding)

    async def pending_texts(
        self,
        candidates: Candidates | None,
        *,
        limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
        exclude: Collection[str] = (),
    ) -> list[tuple[str, str]]:
        """Return up to *limit* distinct ``(text_hash, text)`` still waiting for a vector.

        With *candidates* only their Passages in the requested period count;
        without, every queued Passage does. Newest Passages come first, and
        text hashes in *exclude* are skipped.
        """

        def select(connection: sqlite3.Connection) -> list[tuple[str, str]]:
            conditions = ["p.text_hash NOT IN (SELECT value FROM json_each(?))"]
            parameters: list[object] = [json.dumps(sorted(exclude))]
            if not _add_candidate_filter(
                connection, conditions, parameters, candidates, since, until
            ):
                return []
            # With MAX, SQLite takes the bare text column from the newest row.
            rows = connection.execute(
                f"""
                SELECT p.text_hash, p.text, MAX(p.end_timestamp) AS newest
                FROM pending_vectors AS q JOIN passages AS p ON p.passage_ref = q.passage_ref
                WHERE {" AND ".join(conditions)}
                GROUP BY p.text_hash
                ORDER BY newest DESC, p.text_hash
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
            return [(str(row["text_hash"]), str(row["text"])) for row in rows]

        return await self.read(select)

    async def count_pending(
        self,
        candidates: Candidates,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """Count the candidates' Passages in the period that have no vector yet."""

        def count(connection: sqlite3.Connection) -> int:
            conditions: list[str] = []
            parameters: list[object] = []
            if not _add_candidate_filter(
                connection, conditions, parameters, candidates, since, until
            ):
                return 0
            row = connection.execute(
                "SELECT COUNT(*) FROM pending_vectors AS q "
                "JOIN passages AS p ON p.passage_ref = q.passage_ref "
                f"WHERE {' AND '.join(conditions)}",
                parameters,
            ).fetchone()
            return int(row[0])

        return await self.read(count)

    async def store_vectors(
        self, header: VectorHeader, vectors: Mapping[str, Sequence[float]]
    ) -> bool:
        """Store one vector per text hash for every Passage still waiting for it.

        Returns ``False``, storing nothing, when the pinned space is no longer
        *header*'s: the vectors belong to a space the index has left.
        """
        serialized: dict[str, bytes] = {}
        for text_hash, vector in vectors.items():
            if len(vector) != header.dimension:
                raise VectorStoreError(
                    f"vector length {len(vector)} does not match pinned dimension "
                    f"{header.dimension} for model {header.provider_id}/{header.model_id}"
                )
            serialized[text_hash] = sqlite_vec.serialize_float32([float(v) for v in vector])

        def store(connection: sqlite3.Connection) -> bool:
            if _read_header(connection) != header:
                return False
            for text_hash, embedding in serialized.items():
                rows = connection.execute(
                    "SELECT p.passage_ref, p.project_id, p.agent_id, p.start_timestamp, "
                    "p.end_timestamp FROM passages AS p "
                    "JOIN pending_vectors AS q ON q.passage_ref = p.passage_ref "
                    "WHERE p.text_hash = ?",
                    (text_hash,),
                ).fetchall()
                _insert_vectors(connection, rows, embedding)
            return True

        return await self.write(store)

    async def knn_search(
        self,
        *,
        header: VectorHeader,
        query_vector: Sequence[float],
        limit: int,
        candidates: Candidates,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[tuple[StoredPassage, str, float]]:
        """Return the nearest Passages of the candidates with their reported Session.

        The scope, candidate and time filters all run inside the vec0 KNN, so a
        filtered row never takes one of the ``limit`` nearest slots. Each
        Passage appears once, for the Session its hit is reported for.
        """
        if limit <= 0:
            return []
        if len(query_vector) != header.dimension:
            raise VectorStoreError(
                f"query vector length {len(query_vector)} does not match pinned dimension "
                f"{header.dimension} for model {header.provider_id}/{header.model_id}"
            )
        embedding = sqlite_vec.serialize_float32([float(value) for value in query_vector])

        def search(connection: sqlite3.Connection) -> list[tuple[StoredPassage, str, float]]:
            if _read_header(connection) != header:
                raise VectorStoreError(
                    "vector store header is missing or does not match the requested embedding space"
                )
            refs = candidate_refs(connection, candidates)
            if not refs:
                return []
            clauses = [
                "embedding MATCH ?",
                "k = ?",
                "scope_key = ?",
                f"rowid IN ({VIEWED_BY_SESSIONS})",
            ]
            parameters: list[object] = [
                embedding,
                limit,
                _scope_key(candidates.project, candidates.agent_id),
                refs_parameter(refs),
            ]
            if since is not None:
                clauses.append("end_timestamp >= ?")
                parameters.append(_datetime_micros(since))
            if until is not None:
                clauses.append("start_timestamp <= ?")
                parameters.append(_datetime_micros(until))
            rows = connection.execute(
                f"""
                WITH knn AS (
                  SELECT rowid, distance FROM {_VECTOR_TABLE}
                  WHERE {" AND ".join(clauses)}
                )
                SELECT {passage_columns("p")}, knn.distance
                FROM knn JOIN passages AS p ON p.passage_ref = knn.rowid
                ORDER BY knn.distance, p.passage_id, p.passage_ref
                """,
                parameters,
            ).fetchall()
            reported = reported_sessions(
                connection, [int(row["passage_ref"]) for row in rows], refs
            )
            return [
                (stored_passage(row), reported[int(row["passage_ref"])], float(row["distance"]))
                for row in rows
            ]

        return await self.read(search)


def _read_header(connection: sqlite3.Connection) -> VectorHeader | None:
    row = connection.execute(
        "SELECT provider_id, model_id, response_model_id, space_fingerprint, index_policy, "
        "dimension FROM store_header WHERE singleton = 1"
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


def _read_pinned_header(connection: sqlite3.Connection) -> VectorHeader | None:
    header = _read_header(connection)
    if header is not None and (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_VECTOR_TABLE,)
        ).fetchone()
        is None
    ):
        raise DatabaseCorruptError(
            "recall_vectors: the vector table of the pinned space is missing"
        )
    return header


def _use_space(connection: sqlite3.Connection, header: VectorHeader) -> bool:
    if _read_header(connection) == header:
        return False
    connection.execute(f"DROP TRIGGER IF EXISTS {_VECTOR_TRIGGER}")
    connection.execute(f"DROP TABLE IF EXISTS {_VECTOR_TABLE}")
    connection.execute("DELETE FROM store_header")
    connection.execute(
        "INSERT INTO store_header (singleton, provider_id, model_id, response_model_id, "
        "space_fingerprint, index_policy, dimension) VALUES (1, ?, ?, ?, ?, ?, ?)",
        (
            header.provider_id,
            header.model_id,
            header.response_model_id,
            header.space_fingerprint,
            header.index_policy,
            header.dimension,
        ),
    )
    # vec0 needs a fixed dimension at creation; the space pins it.
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE {_VECTOR_TABLE} USING vec0(
          scope_key TEXT partition key,
          start_timestamp INTEGER,
          end_timestamp INTEGER,
          embedding float[{header.dimension}] distance_metric=cosine
        )
        """
    )
    # vec0 resolves only ``rowid = ?`` as a point lookup.
    connection.execute(
        f"""
        CREATE TRIGGER {_VECTOR_TRIGGER} AFTER DELETE ON passages BEGIN
          DELETE FROM {_VECTOR_TABLE} WHERE rowid = old.passage_ref;
        END
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO pending_vectors (passage_ref) SELECT passage_ref FROM passages"
    )
    return True


def _stored_embedding(connection: sqlite3.Connection, text_hash: str) -> bytes | None:
    """One stored vector for *text_hash*, from any Passage that has one."""
    row = connection.execute(
        "SELECT passage_ref FROM passages AS p WHERE text_hash = ? AND NOT EXISTS "
        "(SELECT 1 FROM pending_vectors WHERE passage_ref = p.passage_ref) LIMIT 1",
        (text_hash,),
    ).fetchone()
    if row is None:
        return None
    vector = connection.execute(
        f"SELECT embedding FROM {_VECTOR_TABLE} WHERE rowid = ?", (int(row[0]),)
    ).fetchone()
    return None if vector is None else bytes(vector[0])


def _insert_vectors(
    connection: sqlite3.Connection, rows: Sequence[sqlite3.Row], embedding: bytes
) -> None:
    for row in rows:
        passage_ref = int(row["passage_ref"])
        connection.execute(
            f"INSERT INTO {_VECTOR_TABLE} (rowid, scope_key, start_timestamp, end_timestamp, "
            "embedding) VALUES (?, ?, ?, ?, ?)",
            (
                passage_ref,
                _scope_key(str(row["project_id"]), str(row["agent_id"])),
                _timestamp_micros(row["start_timestamp"], passage_ref),
                _timestamp_micros(row["end_timestamp"], passage_ref),
                embedding,
            ),
        )
        connection.execute("DELETE FROM pending_vectors WHERE passage_ref = ?", (passage_ref,))


def _add_candidate_filter(
    connection: sqlite3.Connection,
    conditions: list[str],
    parameters: list[object],
    candidates: Candidates | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    """Restrict a pending-queue query to *candidates*; ``False`` when none is indexed."""
    if candidates is None:
        return True
    refs = candidate_refs(connection, candidates)
    if not refs:
        return False
    conditions.append(f"q.passage_ref IN ({VIEWED_BY_SESSIONS})")
    parameters.append(refs_parameter(refs))
    bounds, bound_parameters = time_bounds("p", since, until)
    conditions.extend(bounds)
    parameters.extend(bound_parameters)
    return True


def _scope_key(project: str, agent_id: str) -> str:
    return f"{project}\0{agent_id}"


def _timestamp_micros(value: object, passage_ref: int) -> int:
    """A stored Passage timestamp as microseconds since the epoch.

    Passages carry the Session's canonical timestamps, so any other value means
    the projection cannot be trusted and is rebuilt.
    """
    try:
        parsed = parse_canonical_timestamp(value if isinstance(value, str) else "")
    except ValueError as error:
        raise DatabaseCorruptError(
            f"recall_vectors: Passage {passage_ref} has a non-canonical timestamp {value!r}"
        ) from error
    return _datetime_micros(parsed)


def _datetime_micros(value: datetime) -> int:
    """An aware *value* as whole microseconds since the epoch."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone information")
    return (value - _EPOCH) // _MICROSECOND
