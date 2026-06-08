"""Tests for the sqlite-vec vector store."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.recall.vector_store import (
    SessionVectorRecord,
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
) -> SessionVectorRecord:
    return SessionVectorRecord(
        session_id=session_id,
        agent_id=agent_id,
        started_at=datetime(2026, 5, 1, 12, tzinfo=UTC).isoformat(),
        mtime_ns=mtime_ns,
        size_bytes=size_bytes,
        anchor_message_id=anchor,
        snippet=f"snippet for {session_id}",
    )


def test_vector_store_creates_index_file_under_recall_dir(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="openrouter", model_id="model-a", dimension=4)
    store.upsert_session(
        header=header,
        record=_record("sess-1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )

    assert store.path == tmp_path / "recall" / "session_vectors.sqlite"
    assert store.path.is_file()


def test_vector_store_pins_provider_model_and_dimension_in_header(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="openrouter", model_id="model-a", dimension=4)
    store.upsert_session(header=header, record=_record("sess-1"), vector=[0.1, 0.2, 0.3, 0.4])

    stored = store.read_header()
    assert stored is not None
    assert stored.provider_id == "openrouter"
    assert stored.model_id == "model-a"
    assert stored.dimension == 4


def test_vector_store_creates_vec0_table_lazily_on_first_insert(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    assert store.read_header() is None

    store.upsert_session(
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
    store.upsert_session(header=header_a, record=_record("a-1"), vector=[0.1, 0.2, 0.3, 0.4])
    store.upsert_session(header=header_a, record=_record("a-2"), vector=[0.2, 0.3, 0.4, 0.5])
    assert set(store.list_indexed_sessions("coder")) == {"a-1", "a-2"}

    store.upsert_session(header=header_b, record=_record("b-1"), vector=[0.9, 0.8, 0.7, 0.6])

    indexed = store.list_indexed_sessions("coder")
    assert set(indexed) == {"b-1"}
    stored = store.read_header()
    assert stored is not None
    assert stored.model_id == "model-b"


def test_vector_store_drops_and_rebuilds_on_provider_change(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    store.upsert_session(
        header=VectorHeader(provider_id="openrouter", model_id="m", dimension=4),
        record=_record("s1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )
    store.upsert_session(
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
    store.upsert_session(
        header=VectorHeader(provider_id="p", model_id="m", dimension=4),
        record=_record("s1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )

    # Bump the schema version to simulate an old index.
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA user_version = 999")
        conn.commit()

    store.upsert_session(
        header=VectorHeader(provider_id="p", model_id="m", dimension=4),
        record=_record("s2"),
        vector=[0.4, 0.5, 0.6, 0.7],
    )

    # The first session is gone — the index was wiped and rebuilt.
    assert set(store.list_indexed_sessions("coder")) == {"s2"}


def test_vector_store_rejects_vector_with_wrong_dimension(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    store.upsert_session(
        header=VectorHeader(provider_id="p", model_id="m", dimension=4),
        record=_record("s1"),
        vector=[0.1, 0.2, 0.3, 0.4],
    )

    with pytest.raises(VectorStoreError, match="does not match pinned dimension"):
        store.upsert_session(
            header=VectorHeader(provider_id="p", model_id="m", dimension=4),
            record=_record("s2"),
            vector=[0.1, 0.2, 0.3, 0.4, 0.5],
        )


def test_vector_store_knn_search_returns_nearest_by_cosine(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)
    store.upsert_session(header=header, record=_record("near"), vector=[1.0, 0.0, 0.0])
    store.upsert_session(header=header, record=_record("mid"), vector=[0.7, 0.7, 0.0])
    store.upsert_session(header=header, record=_record("far"), vector=[0.0, 0.0, 1.0])

    results = store.knn_search(header=header, query_vector=[1.0, 0.0, 0.0], limit=3)
    assert [rowid for rowid, _ in results] == [1, 2, 3]
    assert results[0][1] == pytest.approx(0.0, abs=1e-5)
    assert results[-1][1] > results[0][1]


def test_vector_store_get_sessions_by_rowids_round_trip(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)
    store.upsert_session(header=header, record=_record("a"), vector=[1.0, 0.0, 0.0])
    store.upsert_session(header=header, record=_record("b"), vector=[0.0, 1.0, 0.0])

    records = store.get_sessions_by_rowids([1, 2])
    assert set(records) == {1, 2}
    assert {record.session_id for record in records.values()} == {"a", "b"}


def test_vector_store_bulk_upsert_writes_all_records(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=3)
    records = [(_record(f"s{i}"), [float(i), 0.0, 0.0]) for i in range(5)]
    written = store.upsert_many_sessions(header=header, records=records)
    assert written == 5
    assert len(store.list_indexed_sessions("coder")) == 5


def test_vector_store_list_indexed_sessions_reports_mtime_and_size(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=2)
    record = SessionVectorRecord(
        session_id="s1",
        agent_id="coder",
        started_at=datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        mtime_ns=12345,
        size_bytes=67890,
        anchor_message_id="m1",
        snippet="snippet",
    )
    store.upsert_session(header=header, record=record, vector=[0.1, 0.2])

    indexed = store.list_indexed_sessions("coder")
    assert indexed == {"s1": (12345, 67890)}


def test_vector_store_drop_indexed_sessions_removes_only_listed(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(provider_id="p", model_id="m", dimension=2)
    for sid in ("keep-1", "drop-1", "keep-2", "drop-2"):
        store.upsert_session(header=header, record=_record(sid), vector=[0.1, 0.2])

    removed = store.drop_indexed_sessions("coder", ["drop-1", "drop-2"])
    assert removed == 2
    assert set(store.list_indexed_sessions("coder")) == {"keep-1", "keep-2"}


def test_vector_store_returns_empty_when_metadata_table_missing(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    # Brand-new database: no metadata table yet, no header.
    assert store.list_indexed_sessions("coder") == {}
    assert store.get_sessions_by_rowids([1, 2]) == {}
    assert store.read_header() is None


def test_vector_store_truncate_to_input_limit_uses_context_window(tmp_path: Path) -> None:
    text = "lorem ipsum " * 200
    truncated = VectorStore.truncate_to_input_limit(text, context_window=40)
    # 40 tokens // 4 chars/token → at most 10 chars
    assert len(truncated) == 10


def test_vector_store_truncate_to_input_limit_falls_back_to_default(tmp_path: Path) -> None:
    long = "x" * 50_000
    truncated = VectorStore.truncate_to_input_limit(long, context_window=None)
    assert len(truncated) == 32_000
