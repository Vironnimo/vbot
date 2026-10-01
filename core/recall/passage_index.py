"""The one disposable Passage index of Recall.

``<data>/recall/passage_index.sqlite`` is a disposable projection opened through
the kernel (``core/database``). It holds:

- the Passage catalog (:mod:`core.recall._passage_catalog`): which Passages every
  indexed Session's current view shows, with a freshness stamp per Session;
- two FTS5 tables over Passage text for literal search, a trigram table for
  substring matches and a ``unicode61`` token table for one- and two-character
  terms;
- a singleton header pinning the embedding space, one vector per Passage in a
  sqlite-vec ``vec0`` table, and ``pending_vectors``, the Passages still waiting
  for a vector;
- ``skipped_texts``, texts the embedding provider permanently rejected, and the
  indexer's bookkeeping (usage spent in the pinned space, last completed pass).

The catalog refresh is structural and complete for the Sessions it covers. A
trigger queues every new Passage; one whose text already has a vector in the
pinned space copies it at once and leaves the queue. Embedding drains the queue
outside any transaction (:mod:`core.recall.semantic_indexer`); storing a vector
serves every queued Passage with the same text. A change of embedding space
drops the vector table and queues every Passage again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

from core.database import APPLICATION_IDS, DISPOSABLE, DatabaseCorruptError, DatabaseSpec
from core.model_tasks import EmbeddingResult, EmbeddingSpaceIdentity, EmbeddingUsage
from core.recall._passage_catalog import (
    CATALOG_SCHEMA,
    VIEWED_BY_SESSIONS,
    Candidates,
    PassageCatalog,
    StoredPassage,
    candidate_refs,
    delete_sessions,
    passage_columns,
    projection_version,
    refs_parameter,
    reported_sessions,
    stored_passage,
    stored_scope,
    time_bounds,
)
from core.recall.canonical import compact_text, text_matches_search_request
from core.recall.passages import (
    PASSAGE_OVERLAP_CHARS,
    PASSAGE_POLICY_VERSION,
    PASSAGE_TARGET_CHARS,
)
from core.recall.recall import RecallSearchRequest
from core.utils.timestamps import format_canonical_timestamp, parse_canonical_timestamp

INDEX_DIR_NAME = "recall"
INDEX_FILE_NAME = "passage_index.sqlite"
# Bump when the index tables or the meaning of their rows change; the kernel
# discards and rebuilds an index built for another version.
_LAYOUT_VERSION = 1
# sqlite-vec rejects a vec0 KNN query whose ``k`` exceeds this limit.
KNN_MAX_K = 4096
# The Passage construction policy every stored vector was embedded under.
INDEX_POLICY = (
    f"passage-v{PASSAGE_POLICY_VERSION}:target={PASSAGE_TARGET_CHARS}:"
    f"overlap={PASSAGE_OVERLAP_CHARS}"
)

_VECTOR_TABLE = "passage_vectors"
_VECTOR_TRIGGER = "passages_drop_vector"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
# FTS5 trigram needs at least three characters; shorter values use the token index.
_TRIGRAM_MIN_CHARS = 3
_TRIGRAM_TABLE = "passages_fts"
_TOKEN_TABLE = "passages_fts_tokens"
TRIGRAM_RANKING = "bm25_trigram"
_TOKEN_RANKING = "bm25_token"

_INDEX_SCHEMA = """
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

CREATE TABLE skipped_texts (
  text_hash TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  skipped_at TEXT NOT NULL
) STRICT;

CREATE TABLE indexer_state (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  requests INTEGER NOT NULL,
  token_reports INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,
  cost_reports INTEGER NOT NULL,
  cost REAL NOT NULL,
  last_completed_at TEXT
) STRICT;

CREATE TRIGGER passages_queue_vector AFTER INSERT ON passages BEGIN
  INSERT INTO pending_vectors (passage_ref) VALUES (new.passage_ref);
END;

CREATE TRIGGER passages_unqueue_vector AFTER DELETE ON passages BEGIN
  DELETE FROM pending_vectors WHERE passage_ref = old.passage_ref;
END;
"""

# FTS5 tables are virtual, which the kernel's declared schema cannot hold, so
# they are created on open together with the triggers that keep them in step
# with ``passages``. Passage rows never change after insertion.
_FTS_TABLES = {
    _TRIGRAM_TABLE: (
        f"CREATE VIRTUAL TABLE {_TRIGRAM_TABLE} USING fts5("
        "text, content='passages', content_rowid='passage_ref', tokenize='trigram')"
    ),
    _TOKEN_TABLE: (
        f"CREATE VIRTUAL TABLE {_TOKEN_TABLE} USING fts5("
        "text, content='passages', content_rowid='passage_ref')"
    ),
}
_FTS_TRIGGERS = {
    "passages_fts_insert": f"""
        CREATE TRIGGER passages_fts_insert AFTER INSERT ON passages BEGIN
          INSERT INTO {_TRIGRAM_TABLE}(rowid, text) VALUES (new.passage_ref, new.text);
          INSERT INTO {_TOKEN_TABLE}(rowid, text) VALUES (new.passage_ref, new.text);
        END
    """,
    "passages_fts_delete": f"""
        CREATE TRIGGER passages_fts_delete AFTER DELETE ON passages BEGIN
          INSERT INTO {_TRIGRAM_TABLE}({_TRIGRAM_TABLE}, rowid, text)
            VALUES ('delete', old.passage_ref, old.text);
          INSERT INTO {_TOKEN_TABLE}({_TOKEN_TABLE}, rowid, text)
            VALUES ('delete', old.passage_ref, old.text);
        END
    """,
}


class PassageIndexError(RuntimeError):
    """An index operation was used wrongly; the database itself is intact.

    It is never a SQLite error, so the kernel never takes it for damage.
    """


@dataclass(frozen=True)
class VectorHeader:
    """Header row pinning the embedding space that produced the stored vectors."""

    provider_id: str
    model_id: str
    dimension: int
    space_fingerprint: str = ""
    index_policy: str = ""
    # The model the provider reported serving when the space was pinned. It is
    # informational, not identity: a router such as OpenRouter may answer one
    # configured model from several hosts that report different names.
    response_model_id: str = field(default="", compare=False)

    @classmethod
    def for_space(cls, identity: EmbeddingSpaceIdentity) -> VectorHeader:
        """The configured space before an embedding revealed its dimension."""
        return cls(
            provider_id=identity.provider_id,
            model_id=identity.model_id,
            dimension=0,
            space_fingerprint=identity.fingerprint,
            index_policy=INDEX_POLICY,
        )

    @classmethod
    def from_result(cls, result: EmbeddingResult) -> VectorHeader:
        """The space one embedding response landed in."""
        return cls(
            provider_id=result.provider_id,
            model_id=result.model_id,
            dimension=result.dimension,
            space_fingerprint=result.space_fingerprint,
            index_policy=INDEX_POLICY,
            response_model_id=result.actual_model_id,
        )

    def same_space(self, other: VectorHeader) -> bool:
        """Same configured space and policy; dimension aside."""
        return (
            self.provider_id == other.provider_id
            and self.model_id == other.model_id
            and self.space_fingerprint == other.space_fingerprint
            and self.index_policy == other.index_policy
        )


def space_change_reason(stored: VectorHeader | None, header: VectorHeader) -> str:
    """Why pinning *header* over *stored* drops the stored vectors, as a log value."""
    if stored is None:
        return "first"
    if not stored.same_space(header):
        return "binding"
    return "dimension"


@dataclass(frozen=True)
class KnnMatches:
    """The nearest Passages of one KNN query, nearest first.

    ``truncated`` is true when the query asked for more than :data:`KNN_MAX_K`
    rows and the capped query returned a full set: more Passages exist than
    the index can rank.
    """

    matches: tuple[tuple[StoredPassage, str, float], ...]
    truncated: bool = False


@dataclass(frozen=True)
class LiteralQuery:
    """One FTS table and ``MATCH`` expression for a literal Passage query."""

    table: str
    expression: str
    ranking: str


@dataclass(frozen=True)
class IndexCounts:
    """Distinct stored Passages by embedding state, and the size of what waits.

    ``waiting_texts`` and ``waiting_characters`` count each waiting text once,
    as the provider receives it.
    """

    indexed: int = 0
    waiting: int = 0
    skipped: int = 0
    waiting_texts: int = 0
    waiting_characters: int = 0


@dataclass(frozen=True)
class IndexSpent:
    """Provider usage of document embedding since the current space was pinned."""

    usage: EmbeddingUsage = EmbeddingUsage()
    last_completed_at: str | None = None


def index_path(data_dir: Path) -> Path:
    """The Passage index file inside *data_dir*."""
    return data_dir / INDEX_DIR_NAME / INDEX_FILE_NAME


def _load_sqlite_vec(connection: sqlite3.Connection) -> None:
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)


def _prepare_fts(connection: sqlite3.Connection) -> None:
    """Create missing FTS tables and triggers; a new table indexes existing rows."""
    names = [*_FTS_TABLES, *_FTS_TRIGGERS]
    present = {
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE name IN ({', '.join('?' for _ in names)})",
            names,
        )
    }
    if present.issuperset(names):
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, sql in _FTS_TABLES.items():
            if name not in present:
                connection.execute(sql)
                connection.execute(f"INSERT INTO {name}({name}) VALUES ('rebuild')")
        for name, sql in _FTS_TRIGGERS.items():
            if name not in present:
                connection.execute(sql)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def passage_index_database_spec(path: Path) -> DatabaseSpec:
    """Declare the disposable Passage index at ``path``."""
    return DatabaseSpec(
        name="recall_index",
        path=path,
        profile=DISPOSABLE,
        application_id=APPLICATION_IDS["recall_index"],
        format_generation=1,
        schema_sql=CATALOG_SCHEMA + _INDEX_SCHEMA,
        projection_version=projection_version(_LAYOUT_VERSION),
        after_open=_prepare_fts,
        connection_setup=_load_sqlite_vec,
    )


class PassageIndex(PassageCatalog):
    """The Passage catalog with literal FTS and vectors in one pinned embedding space."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        super().__init__(passage_index_database_spec(index_path(data_dir)))

    # -- Embedding space -----------------------------------------------------------

    async def read_header(self) -> VectorHeader | None:
        """Return the pinned embedding space; ``None`` before the first vector space.

        A pinned space without its vector table is damage the owner rebuilds.
        """
        return await self.read(_read_pinned_header)

    async def use_space(self, header: VectorHeader) -> bool:
        """Pin *header*'s embedding space; ``True`` when that dropped every vector.

        Another space's vectors are unusable, so its vector table is replaced
        and every stored Passage is queued for embedding again. Skipped texts
        and spent usage belong to the old space and are cleared.
        """
        if header.dimension <= 0:
            raise PassageIndexError(f"refusing to pin non-positive dimension: {header.dimension}")
        return await self.write(lambda connection: _use_space(connection, header))

    async def reset_vectors(self) -> None:
        """Drop every vector, skipped text and spent usage; queue every Passage again.

        The next embedding pins its space anew.
        """
        await self.write(_drop_vectors)

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

    # -- The embedding queue -------------------------------------------------------

    async def pending_texts(self, *, limit: int) -> list[tuple[str, str]]:
        """Return up to *limit* distinct ``(text_hash, text)`` still waiting for a vector.

        Newest Passages come first; skipped texts are left out.
        """

        def select(connection: sqlite3.Connection) -> list[tuple[str, str]]:
            # With MAX, SQLite takes the bare text column from the newest row.
            rows = connection.execute(
                """
                SELECT p.text_hash, p.text, MAX(p.end_timestamp) AS newest
                FROM pending_vectors AS q JOIN passages AS p ON p.passage_ref = q.passage_ref
                WHERE p.text_hash NOT IN (SELECT text_hash FROM skipped_texts)
                GROUP BY p.text_hash
                ORDER BY newest DESC, p.text_hash
                LIMIT ?
                """,
                (limit,),
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
        """Count the candidates' Passages in the period still waiting for a vector.

        Skipped texts never get a vector in this space and do not count.
        """

        def count(connection: sqlite3.Connection) -> int:
            refs = candidate_refs(connection, candidates)
            if not refs:
                return 0
            conditions = [
                f"q.passage_ref IN ({VIEWED_BY_SESSIONS})",
                "p.text_hash NOT IN (SELECT text_hash FROM skipped_texts)",
            ]
            parameters: list[object] = [refs_parameter(refs)]
            bounds, bound_parameters = time_bounds("p", since, until)
            conditions.extend(bounds)
            parameters.extend(bound_parameters)
            row = connection.execute(
                "SELECT COUNT(*) FROM pending_vectors AS q "
                "JOIN passages AS p ON p.passage_ref = q.passage_ref "
                f"WHERE {' AND '.join(conditions)}",
                parameters,
            ).fetchone()
            return int(row[0])

        return await self.read(count)

    async def store_vectors(
        self,
        header: VectorHeader,
        vectors: Mapping[str, Sequence[float]],
        *,
        usage: EmbeddingUsage | None = None,
    ) -> bool:
        """Store one vector per text hash for every Passage still waiting for it.

        *usage* adds to the spent usage of the space. Returns ``False``, storing
        nothing, when the pinned space is no longer *header*'s: the vectors
        belong to a space the index has left.
        """
        serialized: dict[str, bytes] = {}
        for text_hash, vector in vectors.items():
            if len(vector) != header.dimension:
                raise PassageIndexError(
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
            if usage is not None:
                _add_spent(connection, usage)
            return True

        return await self.write(store)

    async def record_skipped(
        self, header: VectorHeader | None, reasons: Mapping[str, str], *, at: datetime
    ) -> bool:
        """Exclude texts the provider permanently rejected in *header*'s space.

        *reasons* maps each text hash to a stable reason code. They stay
        excluded until the space changes or the vectors are reset. ``None``
        records them while no space is pinned; pinning the first space clears
        them, so they are tried once more there. Returns ``False``, recording
        nothing, when the index left *header*'s space.
        """
        skipped_at = format_canonical_timestamp(at)

        def record(connection: sqlite3.Connection) -> bool:
            if _read_header(connection) != header:
                return False
            connection.executemany(
                "INSERT INTO skipped_texts (text_hash, reason, skipped_at) VALUES (?, ?, ?) "
                "ON CONFLICT (text_hash) DO UPDATE SET reason = excluded.reason, "
                "skipped_at = excluded.skipped_at",
                [(text_hash, reason, skipped_at) for text_hash, reason in reasons.items()],
            )
            return True

        return await self.write(record)

    async def mark_completed(self, at: datetime) -> None:
        """Record that a pass left nothing waiting at *at*."""
        completed_at = format_canonical_timestamp(at)

        def mark(connection: sqlite3.Connection) -> None:
            _ensure_indexer_state(connection)
            connection.execute(
                "UPDATE indexer_state SET last_completed_at = ? WHERE singleton = 1",
                (completed_at,),
            )

        await self.write(mark)

    async def counts(self) -> IndexCounts:
        """Count the distinct stored Passages by embedding state."""
        return await self.read(_counts)

    async def spent(self) -> IndexSpent:
        """Usage spent on document embedding in the pinned space."""
        return await self.read(_read_spent)

    # -- Search ----------------------------------------------------------------------

    async def knn_search(
        self,
        *,
        header: VectorHeader,
        query_vector: Sequence[float],
        limit: int,
        candidates: Candidates,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> KnnMatches:
        """Return the nearest Passages of the candidates with their reported Session.

        The scope, candidate and time filters all run inside the vec0 KNN, so a
        filtered row never takes one of the nearest slots. Each Passage appears
        once, for the Session its hit is reported for. At most
        :data:`KNN_MAX_K` Passages are ranked.
        """
        if limit <= 0:
            return KnnMatches(())
        if len(query_vector) != header.dimension:
            raise PassageIndexError(
                f"query vector length {len(query_vector)} does not match pinned dimension "
                f"{header.dimension} for model {header.provider_id}/{header.model_id}"
            )
        embedding = sqlite_vec.serialize_float32([float(value) for value in query_vector])
        k = min(limit, KNN_MAX_K)

        def search(connection: sqlite3.Connection) -> KnnMatches:
            if _read_header(connection) != header:
                raise PassageIndexError(
                    "the pinned embedding space is missing or differs from the requested one"
                )
            refs = candidate_refs(connection, candidates)
            if not refs:
                return KnnMatches(())
            clauses = [
                "embedding MATCH ?",
                "k = ?",
                "scope_key = ?",
                f"rowid IN ({VIEWED_BY_SESSIONS})",
            ]
            parameters: list[object] = [
                embedding,
                k,
                _scope_key(candidates.project, candidates.agent_id),
                refs_parameter(refs),
            ]
            if since is not None:
                clauses.append("end_timestamp >= ?")
                parameters.append(_datetime_micros(since))
            if until is not None:
                clauses.append("start_timestamp <= ?")
                parameters.append(_datetime_micros(until))
            try:
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
            except sqlite3.OperationalError as error:
                if _is_knn_parameter_error(error):
                    raise PassageIndexError(f"invalid KNN query: {error}") from None
                raise
            reported = reported_sessions(
                connection, [int(row["passage_ref"]) for row in rows], refs
            )
            return KnnMatches(
                tuple(
                    (stored_passage(row), reported[int(row["passage_ref"])], float(row["distance"]))
                    for row in rows
                ),
                truncated=limit > KNN_MAX_K and len(rows) >= KNN_MAX_K,
            )

        return await self.read(search)

    async def literal_search(
        self,
        request: RecallSearchRequest,
        candidates: Candidates,
        query: LiteralQuery,
        wanted: int,
    ) -> list[tuple[StoredPassage, str, float]]:
        """Return up to *wanted* ranked Passages whose text matches the query literally."""
        return await self.read(
            lambda connection: _matching_passages(connection, request, candidates, query, wanted)
        )

    # -- Scopes ------------------------------------------------------------------------

    async def indexed_scopes(self) -> set[tuple[str | None, str]]:
        """Every ``(project_id, agent_id)`` scope with an indexed Session."""

        def select(connection: sqlite3.Connection) -> set[tuple[str | None, str]]:
            return {
                (str(row[0]) or None, str(row[1]))
                for row in connection.execute(
                    "SELECT DISTINCT project_id, agent_id FROM indexed_sessions"
                )
            }

        return await self.read(select)

    async def remove_scope(self, agent_id: str, project_id: str | None) -> None:
        """Evict every Session of one scope and the Passages only they showed."""
        project = stored_scope(project_id)

        def remove(connection: sqlite3.Connection) -> None:
            session_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT session_id FROM indexed_sessions WHERE project_id = ? AND agent_id = ?",
                    (project, agent_id),
                )
            ]
            if session_ids:
                delete_sessions(
                    connection, agent_id=agent_id, project=project, session_ids=session_ids
                )

        await self.write(remove)


# -- Literal search ------------------------------------------------------------------


def literal_query(request: RecallSearchRequest) -> LiteralQuery | None:
    """Choose the trigram index when every value has three characters, else tokens.

    Values keep their spelling; both indexes fold case themselves. ``None``
    means the query has no searchable value.
    """

    compact = compact_text(request.query)
    if request.match_mode == "phrase":
        values = [compact] if compact else []
        operator = ""
    else:
        values = [term for term in compact.split(" ") if term]
        operator = " OR " if request.match_mode == "any_term" else " AND "
    if not values:
        return None
    expression = operator.join(_quote_fts_value(value) for value in values)
    if all(len(value) >= _TRIGRAM_MIN_CHARS for value in values):
        return LiteralQuery(_TRIGRAM_TABLE, expression, TRIGRAM_RANKING)
    return LiteralQuery(_TOKEN_TABLE, expression, _TOKEN_RANKING)


def _quote_fts_value(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _matching_passages(
    connection: sqlite3.Connection,
    request: RecallSearchRequest,
    candidates: Candidates,
    query: LiteralQuery,
    wanted: int,
) -> list[tuple[StoredPassage, str, float]]:
    """Return up to *wanted* ranked Passages whose text matches the query literally.

    FTS supplies candidates in rank order; a token-index candidate matches
    whole tokens only (``C#`` is the token ``c``), so each candidate is checked
    before it counts and pages stay full. Each Passage carries the candidate
    Session it is reported for.
    """

    refs = candidate_refs(connection, candidates)
    if not refs:
        return []
    table = query.table
    conditions = [f"{table} MATCH ?", f"p.passage_ref IN ({VIEWED_BY_SESSIONS})"]
    parameters: list[Any] = [query.expression, refs_parameter(refs)]
    bounds, bound_parameters = time_bounds("p", request.since, request.until)
    conditions.extend(bounds)
    parameters.extend(bound_parameters)
    sql = f"""
        SELECT {passage_columns("p")}, bm25({table}) AS rank
        FROM {table}
        JOIN passages AS p ON p.passage_ref = {table}.rowid
        WHERE {" AND ".join(conditions)}
        ORDER BY rank ASC, p.start_timestamp DESC, p.passage_id ASC, p.passage_ref ASC
    """
    matched: list[tuple[StoredPassage, float]] = []
    with closing(connection.execute(sql, parameters)) as cursor:
        for row in cursor:
            if text_matches_search_request(str(row["text"]), request):
                matched.append((stored_passage(row), float(row["rank"])))
                if len(matched) >= wanted:
                    break
    reported = reported_sessions(connection, [passage.passage_ref for passage, _ in matched], refs)
    return [(passage, reported[passage.passage_ref], rank) for passage, rank in matched]


# -- Vector rows ---------------------------------------------------------------------


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
        raise DatabaseCorruptError("recall_index: the vector table of the pinned space is missing")
    return header


def _drop_vectors(connection: sqlite3.Connection) -> None:
    """Remove the vector table, the pinned space and its bookkeeping; queue everything."""
    connection.execute(f"DROP TRIGGER IF EXISTS {_VECTOR_TRIGGER}")
    connection.execute(f"DROP TABLE IF EXISTS {_VECTOR_TABLE}")
    connection.execute("DELETE FROM store_header")
    connection.execute("DELETE FROM skipped_texts")
    connection.execute("DELETE FROM indexer_state")
    connection.execute(
        "INSERT OR IGNORE INTO pending_vectors (passage_ref) SELECT passage_ref FROM passages"
    )


def _use_space(connection: sqlite3.Connection, header: VectorHeader) -> bool:
    if _read_header(connection) == header:
        return False
    _drop_vectors(connection)
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


def _is_knn_parameter_error(error: sqlite3.OperationalError) -> bool:
    """sqlite-vec rejected the KNN query's ``k``, not the database."""
    return "k value in knn quer" in str(error).lower()


# -- Bookkeeping -----------------------------------------------------------------------


def _ensure_indexer_state(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO indexer_state (singleton, requests, token_reports, input_tokens, "
        "total_tokens, cost_reports, cost) VALUES (1, 0, 0, 0, 0, 0, 0.0) "
        "ON CONFLICT (singleton) DO NOTHING"
    )


def _add_spent(connection: sqlite3.Connection, usage: EmbeddingUsage) -> None:
    _ensure_indexer_state(connection)
    connection.execute(
        "UPDATE indexer_state SET requests = requests + ?, token_reports = token_reports + ?, "
        "input_tokens = input_tokens + ?, total_tokens = total_tokens + ?, "
        "cost_reports = cost_reports + ?, cost = cost + ? WHERE singleton = 1",
        (
            usage.requests,
            usage.token_reports,
            usage.input_tokens,
            usage.total_tokens,
            usage.cost_reports,
            float(usage.cost),
        ),
    )


def _read_spent(connection: sqlite3.Connection) -> IndexSpent:
    row = connection.execute(
        "SELECT requests, token_reports, input_tokens, total_tokens, cost_reports, cost, "
        "last_completed_at FROM indexer_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return IndexSpent()
    return IndexSpent(
        usage=EmbeddingUsage(
            requests=int(row["requests"]),
            token_reports=int(row["token_reports"]),
            input_tokens=int(row["input_tokens"]),
            total_tokens=int(row["total_tokens"]),
            cost_reports=int(row["cost_reports"]),
            cost=float(row["cost"]),
        ),
        last_completed_at=(
            str(row["last_completed_at"]) if row["last_completed_at"] is not None else None
        ),
    )


def _counts(connection: sqlite3.Connection) -> IndexCounts:
    total = int(connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0])
    pending = int(connection.execute("SELECT COUNT(*) FROM pending_vectors").fetchone()[0])
    skipped = int(
        connection.execute(
            "SELECT COUNT(*) FROM pending_vectors AS q "
            "JOIN passages AS p ON p.passage_ref = q.passage_ref "
            "WHERE p.text_hash IN (SELECT text_hash FROM skipped_texts)"
        ).fetchone()[0]
    )
    texts, characters = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(characters), 0) FROM ("
        "  SELECT MAX(length(p.text)) AS characters FROM pending_vectors AS q "
        "  JOIN passages AS p ON p.passage_ref = q.passage_ref "
        "  WHERE p.text_hash NOT IN (SELECT text_hash FROM skipped_texts) "
        "  GROUP BY p.text_hash"
        ")"
    ).fetchone()
    return IndexCounts(
        indexed=total - pending,
        waiting=pending - skipped,
        skipped=skipped,
        waiting_texts=int(texts),
        waiting_characters=int(characters),
    )


# -- Values ----------------------------------------------------------------------------


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
            f"recall_index: Passage {passage_ref} has a non-canonical timestamp {value!r}"
        ) from error
    return _datetime_micros(parsed)


def _datetime_micros(value: datetime) -> int:
    """An aware *value* as whole microseconds since the epoch."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone information")
    return (value - _EPOCH) // _MICROSECOND


__all__ = [
    "INDEX_DIR_NAME",
    "INDEX_FILE_NAME",
    "INDEX_POLICY",
    "KNN_MAX_K",
    "TRIGRAM_RANKING",
    "IndexCounts",
    "IndexSpent",
    "KnnMatches",
    "LiteralQuery",
    "PassageIndex",
    "PassageIndexError",
    "VectorHeader",
    "index_path",
    "literal_query",
    "passage_index_database_spec",
    "space_change_reason",
]
