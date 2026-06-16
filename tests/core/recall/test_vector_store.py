"""Tests for the sqlite-vec vector store."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlite_vec  # type: ignore[import-untyped]

from core.recall.vector_store import (
    ChunkVectorRecord,
    VectorHeader,
    VectorStore,
    VectorStoreError,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _record(
    session_id: str,
    *,
    agent_id: str = "coder",
    mtime_ns: int = 1,
    size_bytes: int = 1,
    anchor: str = "m1",
    snippet: str | None = None,
    chunk_index: int = 0,
    start_message_id: str = "m1",
    end_message_id: str = "m1",
) -> ChunkVectorRecord:
    return ChunkVectorRecord(
        session_id=session_id,
        agent_id=agent_id,
        started_at=datetime(2026, 5, 1, 12, tzinfo=UTC).isoformat(),
        mtime_ns=mtime_ns,
        size_bytes=size_bytes,
        anchor_message_id=anchor,
        snippet=snippet if snippet is not None else f"snippet for {session_id}",
        chunk_index=chunk_index,
        start_message_id=start_message_id,
        end_message_id=end_message_id,
    )


def _upsert_one(
    store: VectorStore,
    *,
    header: VectorHeader,
    record: ChunkVectorRecord,
    vector: list[float],
) -> None:
    """Seed a single chunk; production indexing batches via ``upsert_many_chunks``."""
    store.upsert_many_chunks(header=header, records=[(record, vector)])


def test_vector_store_creates_index_file_under_recall_dir(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="openrouter", model_id="model-a", dimension=4)
    _upsert_one(
        store,
        header=header,
        record=_record("sess-1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )

    assert store.path == tmp_path / "recall" / "session_vectors.sqlite"
    assert store.path.is_file()


def test_vector_store_pins_provider_model_and_dimension_in_header(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="openrouter", model_id="model-a", dimension=4)
    _upsert_one(store, header=header, record=_record("sess-1"), vector=[0.1, 0.2, 0.3, 0.4])

    stored = store.read_header()
    assert stored is not None
    assert stored.provider_id == "openrouter"
    assert stored.model_id == "model-a"
    assert stored.dimension == 4


def test_vector_store_creates_vec0_table_lazily_on_first_insert(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    assert store.read_header() is None

    _upsert_one(
        store,
        header=VectorHeader(provider_id="p", model_id="m", dimension=3),
        record=_record("s1"),
        vector=[0.1, 0.2, 0.3],
    )

    # After the first insert the vec0 table must exist with the observed dim.
    with sqlite3.connect(store.path) as conn:
        rows = list(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='session_vectors'"
            )
        )
    assert len(rows) == 1


def test_vector_store_drops_and_rebuilds_on_model_change(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header_a = VectorHeader(provider_id="openrouter", model_id="model-a", dimension=4)
    header_b = VectorHeader(provider_id="openrouter", model_id="model-b", dimension=4)
    _upsert_one(store, header=header_a, record=_record("a-1"), vector=[0.1, 0.2, 0.3, 0.4])
    _upsert_one(store, header=header_a, record=_record("a-2"), vector=[0.2, 0.3, 0.4, 0.5])
    assert set(store.list_indexed_sessions("coder")) == {"a-1", "a-2"}

    _upsert_one(store, header=header_b, record=_record("b-1"), vector=[0.9, 0.8, 0.7, 0.6])

    indexed = store.list_indexed_sessions("coder")
    assert set(indexed) == {"b-1"}
    stored = store.read_header()
    assert stored is not None
    assert stored.model_id == "model-b"


def test_vector_store_drops_and_rebuilds_on_provider_change(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _upsert_one(
        store,
        header=VectorHeader(provider_id="openrouter", model_id="m", dimension=4),
        record=_record("s1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )
    _upsert_one(
        store,
        header=VectorHeader(provider_id="openai", model_id="m", dimension=4),
        record=_record("s2"),
        vector=[0.5, 0.6, 0.7, 0.8],
    )

    stored = store.read_header()
    assert stored is not None
    assert stored.provider_id == "openai"
    assert set(store.list_indexed_sessions("coder")) == {"s2"}


def test_vector_store_rebuilds_on_schema_version_mismatch(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _upsert_one(
        store,
        header=VectorHeader(provider_id="p", model_id="m", dimension=4),
        record=_record("s1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )

    # Bump the schema version to simulate an old index.
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA user_version = 999")
        conn.commit()

    _upsert_one(
        store,
        header=VectorHeader(provider_id="p", model_id="m", dimension=4),
        record=_record("s2"),
        vector=[0.4, 0.5, 0.6, 0.7],
    )

    # The first session is gone — the index was wiped and rebuilt.
    assert set(store.list_indexed_sessions("coder")) == {"s2"}


def test_vector_store_rejects_vector_with_wrong_dimension(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _upsert_one(
        store,
        header=VectorHeader(provider_id="p", model_id="m", dimension=4),
        record=_record("s1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )

    with pytest.raises(VectorStoreError, match="does not match pinned dimension"):
        _upsert_one(
            store,
            header=VectorHeader(provider_id="p", model_id="m", dimension=4),
            record=_record("s2"),
            vector=[0.1, 0.2, 0.3, 0.4, 0.5],
        )


def test_vector_store_knn_search_returns_nearest_by_cosine(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)
    _upsert_one(store, header=header, record=_record("near"), vector=[1.0, 0.0, 0.0])
    _upsert_one(store, header=header, record=_record("mid"), vector=[0.7, 0.7, 0.0])
    _upsert_one(store, header=header, record=_record("far"), vector=[0.0, 0.0, 1.0])

    results = store.knn_search(header=header, query_vector=[1.0, 0.0, 0.0], limit=3)
    assert [rowid for rowid, _ in results] == [1, 2, 3]
    assert results[0][1] == pytest.approx(0.0, abs=1e-5)
    assert results[-1][1] > results[0][1]


def test_vector_store_get_chunks_by_rowids_round_trip(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)
    _upsert_one(store, header=header, record=_record("a"), vector=[1.0, 0.0, 0.0])
    _upsert_one(store, header=header, record=_record("b"), vector=[0.0, 1.0, 0.0])

    records = store.get_chunks_by_rowids([1, 2])
    assert set(records) == {1, 2}
    assert {record.session_id for record in records.values()} == {"a", "b"}

    # New fields round-trip from the chunk row.
    record_a = records[1]
    assert record_a.chunk_index == 0
    assert record_a.start_message_id == "m1"
    assert record_a.end_message_id == "m1"


def test_vector_store_bulk_upsert_writes_all_records(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)
    records = [(_record(f"s{i}"), [float(i), 0.0, 0.0]) for i in range(5)]
    written = store.upsert_many_chunks(header=header, records=records)
    assert written == 5
    assert len(store.list_indexed_sessions("coder")) == 5


def test_vector_store_list_indexed_sessions_reports_mtime_and_size(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=2)
    record = ChunkVectorRecord(
        session_id="s1",
        agent_id="coder",
        started_at=datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        mtime_ns=12345,
        size_bytes=67890,
        anchor_message_id="m1",
        snippet="snippet",
        chunk_index=0,
        start_message_id="m1",
        end_message_id="m1",
    )
    _upsert_one(store, header=header, record=record, vector=[0.1, 0.2])

    indexed = store.list_indexed_sessions("coder")
    assert indexed == {"s1": (12345, 67890)}


def test_vector_store_drop_indexed_sessions_removes_only_listed(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=2)
    for sid in ("keep-1", "drop-1", "keep-2", "drop-2"):
        _upsert_one(store, header=header, record=_record(sid), vector=[0.1, 0.2])

    removed = store.drop_indexed_sessions("coder", ["drop-1", "drop-2"])
    assert removed == 2
    assert set(store.list_indexed_sessions("coder")) == {"keep-1", "keep-2"}


def test_vector_store_returns_empty_when_chunk_table_missing(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    # Brand-new database: no chunk table yet, no header.
    assert store.list_indexed_sessions("coder") == {}
    assert store.get_chunks_by_rowids([1, 2]) == {}
    assert store.read_header() is None


def test_vector_store_delete_session_is_noop_when_chunk_table_missing(tmp_path: Path) -> None:
    # Regression: on a fresh index the chunk table does not exist yet. The
    # vector backend deletes empty sessions during eager backfill before any
    # upsert creates the schema; that must not raise ``no such table: chunks``.
    store = VectorStore(tmp_path)
    store.delete_session("coder", "never-indexed")  # must not raise


def test_vector_store_drop_indexed_sessions_is_noop_when_chunk_table_missing(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    assert store.drop_indexed_sessions("coder", ["a", "b"]) == 0  # must not raise


def test_vector_store_truncate_to_input_limit_uses_context_window(tmp_path: Path) -> None:
    text = "lorem ipsum " * 200
    truncated = VectorStore.truncate_to_input_limit(text, context_window=40)
    # int(40 * 0.9) * 3 chars/token → 108 chars
    assert len(truncated) == 108


def test_vector_store_truncate_to_input_limit_falls_back_to_default(tmp_path: Path) -> None:
    long = "x" * 50_000
    truncated = VectorStore.truncate_to_input_limit(long, context_window=None)
    # Unknown window assumes the 8192-token floor: int(8192 * 0.9) * 3 = 22116
    assert len(truncated) == 22_116


def test_vector_store_truncate_keeps_dense_text_under_token_window(tmp_path: Path) -> None:
    """The character budget must stay under the model's token cap even for
    dense text near 3 chars/token — the bge-m3 8192 overflow on German
    sessions that motivated the conservative heuristic.
    """

    window = 8192
    truncated = VectorStore.truncate_to_input_limit("x" * 1_000_000, context_window=window)
    # Even at a worst-case dense 3 chars/token, the result stays under the cap.
    assert len(truncated) / 3 < window


# ---------------------------------------------------------------------------
# Chunk-keyed schema tests (Phase 1 of the per-session chunking plan)
# ---------------------------------------------------------------------------


def _chunk_record(
    session_id: str,
    chunk_index: int,
    *,
    agent_id: str = "coder",
    mtime_ns: int = 1,
    size_bytes: int = 1,
    anchor: str = "m1",
    start_message_id: str = "m1",
    end_message_id: str = "m1",
) -> ChunkVectorRecord:
    """Helper for chunk-keyed tests: builds a record with chunk_index/anchor boundaries."""

    return ChunkVectorRecord(
        session_id=session_id,
        agent_id=agent_id,
        started_at=datetime(2026, 5, 1, 12, tzinfo=UTC).isoformat(),
        mtime_ns=mtime_ns,
        size_bytes=size_bytes,
        anchor_message_id=anchor,
        snippet=f"snippet {session_id}#{chunk_index}",
        chunk_index=chunk_index,
        start_message_id=start_message_id,
        end_message_id=end_message_id,
    )


def _vec0_row_count(path: Path) -> int:
    """Count vec0 rows on a freshly opened connection with the extension loaded.

    ``vec0`` is a virtual table provided by the ``sqlite-vec`` extension;
    a bare ``sqlite3.connect`` does not know how to read it. This helper
    loads the extension, then runs a plain ``COUNT(*)`` against the vec0
    table — equivalent to the number of indexed vectors on disk.
    """

    connection = sqlite3.connect(path)
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        row = connection.execute("SELECT COUNT(*) FROM session_vectors").fetchone()
    finally:
        connection.close()
    return int(row[0])


def test_vector_store_multi_chunk_upsert_writes_all_rows_without_clobber(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)

    # Same session, three distinct chunks.
    records = [
        (
            _chunk_record("s1", 0, anchor="m1", start_message_id="m1", end_message_id="m2"),
            [1.0, 0.0, 0.0],
        ),
        (
            _chunk_record("s1", 1, anchor="m3", start_message_id="m3", end_message_id="m4"),
            [0.0, 1.0, 0.0],
        ),
        (
            _chunk_record("s1", 2, anchor="m5", start_message_id="m5", end_message_id="m6"),
            [0.0, 0.0, 1.0],
        ),
    ]

    written = store.upsert_many_chunks(header=header, records=records)

    assert written == 3

    with sqlite3.connect(store.path) as conn:
        # The vec0 table holds one row per chunk; sqlite-vec's introspection
        # counts via ``vec0_row_count`` style queries, but a simple COUNT
        # against the metadata table mirrors it (both tables share rowids).
        chunk_rows = list(
            conn.execute(
                "SELECT chunk_index FROM chunks WHERE agent_id = ? AND session_id = ? "
                "ORDER BY chunk_index",
                ("coder", "s1"),
            )
        )
        assert len(chunk_rows) == 3
        assert [int(row[0]) for row in chunk_rows] == [0, 1, 2]
        assert _vec0_row_count(store.path) == 3


def test_vector_store_reupsert_session_replaces_all_chunks(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)

    initial = [
        (_chunk_record("s1", 0), [1.0, 0.0, 0.0]),
        (_chunk_record("s1", 1), [0.0, 1.0, 0.0]),
    ]
    assert store.upsert_many_chunks(header=header, records=initial) == 2

    # Re-upsert with a different chunk count — all of the previous chunks
    # must be gone, replaced by the new set (delete-each-session-once
    # must not clobber chunks that haven't been re-inserted yet).
    replacement = [
        (
            _chunk_record("s1", 0, anchor="a1", start_message_id="a1", end_message_id="a2"),
            [0.9, 0.1, 0.0],
        ),
        (
            _chunk_record("s1", 1, anchor="a3", start_message_id="a3", end_message_id="a4"),
            [0.1, 0.9, 0.0],
        ),
        (
            _chunk_record("s1", 2, anchor="a5", start_message_id="a5", end_message_id="a6"),
            [0.0, 0.9, 0.1],
        ),
    ]
    assert store.upsert_many_chunks(header=header, records=replacement) == 3

    with sqlite3.connect(store.path) as conn:
        rows = list(
            conn.execute(
                "SELECT chunk_index FROM chunks WHERE agent_id = ? AND session_id = ? "
                "ORDER BY chunk_index",
                ("coder", "s1"),
            )
        )
        assert [int(row[0]) for row in rows] == [0, 1, 2]
        assert _vec0_row_count(store.path) == 3


def test_vector_store_list_indexed_sessions_dedups_to_one_per_session(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)

    records: list[tuple[ChunkVectorRecord, list[float]]] = []
    # Three chunks for s1, two for s2.
    for session_id, chunk_count, mtime, size in (
        ("s1", 3, 100, 1000),
        ("s2", 2, 200, 2000),
    ):
        for idx in range(chunk_count):
            records.append(
                (
                    _chunk_record(
                        session_id,
                        idx,
                        mtime_ns=mtime,
                        size_bytes=size,
                    ),
                    [0.0, 0.0, 0.0],
                )
            )
    store.upsert_many_chunks(header=header, records=records)

    indexed = store.list_indexed_sessions("coder")
    assert set(indexed) == {"s1", "s2"}
    assert indexed["s1"] == (100, 1000)
    assert indexed["s2"] == (200, 2000)


def test_vector_store_get_chunks_by_rowids_returns_new_fields(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)

    records = [
        (
            _chunk_record(
                "s1",
                0,
                anchor="m1",
                start_message_id="m1",
                end_message_id="m2",
            ),
            [1.0, 0.0, 0.0],
        ),
        (
            _chunk_record(
                "s1",
                1,
                anchor="m3",
                start_message_id="m3",
                end_message_id="m4",
            ),
            [0.0, 1.0, 0.0],
        ),
    ]
    store.upsert_many_chunks(header=header, records=records)

    hydrated = store.get_chunks_by_rowids([1, 2])
    assert set(hydrated) == {1, 2}

    by_chunk_index = {record.chunk_index: record for record in hydrated.values()}
    chunk_0 = by_chunk_index[0]
    assert chunk_0.start_message_id == "m1"
    assert chunk_0.end_message_id == "m2"
    assert chunk_0.snippet == "snippet s1#0"
    assert chunk_0.anchor_message_id == "m1"
    chunk_1 = by_chunk_index[1]
    assert chunk_1.start_message_id == "m3"
    assert chunk_1.end_message_id == "m4"
    assert chunk_1.snippet == "snippet s1#1"
    assert chunk_1.anchor_message_id == "m3"


def test_vector_store_knn_search_returns_nearest_chunk_across_sessions(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)

    # s1 has a chunk near the query, s2 only has a far one.
    records = [
        (_chunk_record("s1", 0), [1.0, 0.0, 0.0]),  # nearest
        (_chunk_record("s1", 1), [0.0, 0.0, 1.0]),  # far
        (_chunk_record("s2", 0), [0.5, 0.5, 0.0]),  # mid
    ]
    store.upsert_many_chunks(header=header, records=records)

    results = store.knn_search(header=header, query_vector=[1.0, 0.0, 0.0], limit=3)

    nearest_rowid, nearest_distance = results[0]
    assert nearest_distance == pytest.approx(0.0, abs=1e-5)

    hydrated = store.get_chunks_by_rowids([rowid for rowid, _ in results])
    nearest = hydrated[nearest_rowid]
    assert nearest.session_id == "s1"
    assert nearest.chunk_index == 0
