"""Tests for the sqlite-vec Passage vector store and its shared Passage catalog."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.database import (
    APPLICATION_IDS,
    DatabaseCorruptError,
    DatabaseUnavailableError,
    projection_failure,
)
from core.recall._passage_catalog import (
    Candidates,
    CatalogPlan,
    SessionPassages,
    text_hash,
)
from core.recall.passages import Passage
from core.recall.vector_store import VectorHeader, VectorStore, VectorStoreError
from tests.core.recall.vector_helpers import _connect_store

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::DeprecationWarning")]

HEADER = VectorHeader(provider_id="p", model_id="m", dimension=3)
VECTORS: dict[str, list[float]] = {
    "alpha": [1.0, 0.0, 0.0],
    "beta": [0.0, 1.0, 0.0],
    "gamma": [0.0, 0.0, 1.0],
    "delta": [0.7, 0.7, 0.0],
    "beta grown": [0.1, 0.9, 0.0],
}


def _passage(
    text: str,
    *,
    passage_id: str | None = None,
    start_message_id: str = "m1",
    end_message_id: str = "m1",
    timestamp: str = "2026-05-01T12:00:00.000000Z",
) -> Passage:
    return Passage(
        passage_id=passage_id or f"id-{text}",
        text=text,
        start_message_id=start_message_id,
        end_message_id=end_message_id,
        start_timestamp=timestamp,
        end_timestamp=timestamp,
        start_role="user",
        end_role="user",
        start_offset=0,
        end_offset=len(text),
    )


def _change(
    session_id: str,
    passages: Sequence[Passage],
    *,
    revision: int = 1,
    previous: tuple[str, int] | None = None,
    owned: Sequence[bool] | None = None,
) -> SessionPassages:
    return SessionPassages(
        session_id=session_id,
        previous=previous,
        version=("generation", revision),
        passages=tuple(passages),
        owned=tuple(owned if owned is not None else [True] * len(passages)),
    )


async def _apply(
    store: VectorStore,
    *changes: SessionPassages,
    project: str = "",
    pruned: Sequence[str] = (),
) -> None:
    await store.apply(CatalogPlan("coder", project, tuple(pruned), tuple(changes)))


async def _index(
    store: VectorStore,
    sessions: Mapping[str, Sequence[Passage]],
    *,
    project: str = "",
    header: VectorHeader = HEADER,
) -> None:
    """Pin *header*, add new Sessions and embed every waiting Passage."""
    await store.use_space(header)
    await _apply(
        store,
        *(_change(session_id, passages) for session_id, passages in sessions.items()),
        project=project,
    )
    await _embed_waiting(store, header)


async def _embed_waiting(store: VectorStore, header: VectorHeader = HEADER) -> list[str]:
    batch = await store.pending_texts(None, limit=1000)
    assert await store.store_vectors(header, {key: VECTORS[text] for key, text in batch})
    return sorted(text for _key, text in batch)


def _candidates(*session_ids: str, project: str = "") -> Candidates:
    return Candidates(
        "coder", project, {session_id: order for order, session_id in enumerate(session_ids)}
    )


async def _nearest(
    store: VectorStore,
    vector: Sequence[float],
    candidates: Candidates,
    *,
    limit: int = 10,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[tuple[str, str]]:
    matches = await store.knn_search(
        header=HEADER,
        query_vector=vector,
        limit=limit,
        candidates=candidates,
        since=since,
        until=until,
    )
    return [(session_id, passage.text) for passage, session_id, _distance in matches]


def _identity(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        identity: dict[str, object] = dict(
            connection.execute("SELECT key, value FROM kernel_meta").fetchall()
        )
        identity["application_id"] = connection.execute("PRAGMA application_id").fetchone()[0]
    return identity


def _refs(path: Path) -> dict[str, int]:
    with closing(_connect_store(path)) as connection:
        return {
            str(text): int(ref)
            for ref, text in connection.execute("SELECT passage_ref, text FROM passages")
        }


# ---------------------------------------------------------------------------
# The kernel disposable projection
# ---------------------------------------------------------------------------


async def test_vector_store_is_a_kernel_disposable_projection(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha")]})

    identity = _identity(store.path)
    assert store.path == tmp_path / "recall" / "session_passage_vectors.sqlite"
    assert identity["application_id"] == APPLICATION_IDS["recall_vectors"]
    assert identity["database_name"] == "recall_vectors"
    assert identity["projection_version"] == "1"
    store.close()


async def test_projection_version_mismatch_discards_and_rebuilds_the_store(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha")]})
    old_identity = _identity(store.path)
    store.close()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE kernel_meta SET value = '0' WHERE key = 'projection_version'")

    reopened = VectorStore(tmp_path)

    assert await reopened.read_header() is None
    assert await reopened.list_indexed_sessions("coder") == {}
    identity = _identity(reopened.path)
    assert identity["projection_version"] == "1"
    assert identity["database_id"] != old_identity["database_id"]
    reopened.close()


async def test_unreadable_store_file_is_rebuilt_on_open(tmp_path: Path) -> None:
    path = tmp_path / "recall" / "session_passage_vectors.sqlite"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a sqlite database")
    store = VectorStore(tmp_path)

    await _index(store, {"one": [_passage("alpha")]})

    assert await _nearest(store, [1.0, 0.0, 0.0], _candidates("one")) == [("one", "alpha")]
    store.close()


async def test_pinned_space_without_its_vector_table_is_damage(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha")]})
    with closing(_connect_store(store.path)) as connection, connection:
        connection.execute("DROP TABLE passage_vectors")

    with pytest.raises(Exception) as failure:
        await store.read_header()

    assert projection_failure(failure.value) == "rebuild"
    store.close()


async def test_busy_store_fails_as_busy_and_keeps_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.recall._passage_catalog.WRITE_PATIENCE_S", 0.2)
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha")]})
    identity = _identity(store.path)

    with closing(sqlite3.connect(store.path, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(DatabaseUnavailableError) as failure:
            await _apply(store, _change("two", [_passage("beta")]))
        blocker.execute("ROLLBACK")

    assert projection_failure(failure.value) == "busy"
    await store.discard_if_damaged(failure.value)
    assert _identity(store.path)["database_id"] == identity["database_id"]
    assert await store.list_indexed_sessions("coder") == {"one": ("generation", 1)}
    store.close()


# ---------------------------------------------------------------------------
# Embedding space and the pending queue
# ---------------------------------------------------------------------------


async def test_vector_store_first_space_pins_header_and_queues_new_passages(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    assert await store.read_header() is None

    assert await store.use_space(HEADER) is True
    assert await store.use_space(HEADER) is False
    await _apply(store, _change("one", [_passage("alpha"), _passage("beta")]))

    assert await store.read_header() == HEADER
    assert await store.count_pending(_candidates("one")) == 2
    assert await _embed_waiting(store) == ["alpha", "beta"]
    assert await store.count_pending(_candidates("one")) == 0
    store.close()


async def test_vector_store_refuses_non_positive_dimension(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)

    with pytest.raises(VectorStoreError):
        await store.use_space(VectorHeader(provider_id="p", model_id="m", dimension=0))
    store.close()


async def test_vector_store_new_passage_reuses_a_stored_vector_for_its_text(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha")]})

    await _apply(
        store, _change("two", [_passage("alpha", passage_id="other", end_message_id="m2")])
    )
    await _apply(store, _change("one", [_passage("alpha")]), project="proj")

    assert await store.pending_texts(None, limit=10) == []
    assert await _nearest(store, [1.0, 0.0, 0.0], _candidates("two")) == [("two", "alpha")]
    assert await _nearest(store, [1.0, 0.0, 0.0], _candidates("one", project="proj")) == [
        ("one", "alpha")
    ]
    store.close()


async def test_vector_store_one_vector_serves_every_waiting_passage_with_its_text(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    await store.use_space(HEADER)
    await _apply(store, _change("one", [_passage("alpha")]))
    await _apply(store, _change("one", [_passage("alpha")]), project="proj")

    batch = await store.pending_texts(None, limit=10)
    assert batch == [(text_hash("alpha"), "alpha")]
    assert await store.store_vectors(HEADER, {text_hash("alpha"): VECTORS["alpha"]})

    assert await store.count_pending(_candidates("one")) == 0
    assert await store.count_pending(_candidates("one", project="proj")) == 0
    store.close()


async def test_vector_store_pending_texts_are_newest_first_bounded_and_filtered(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    await store.use_space(HEADER)
    await _apply(
        store,
        _change("old", [_passage("alpha", timestamp="2026-01-01T00:00:00.000000Z")]),
        _change("new", [_passage("beta", timestamp="2026-06-01T00:00:00.000000Z")]),
        _change("newest", [_passage("gamma", timestamp="2026-07-01T00:00:00.000000Z")]),
    )

    assert [text for _key, text in await store.pending_texts(None, limit=2)] == ["gamma", "beta"]
    assert [
        text
        for _key, text in await store.pending_texts(None, limit=10, exclude={text_hash("gamma")})
    ] == ["beta", "alpha"]
    candidates = _candidates("old", "new")
    assert [text for _key, text in await store.pending_texts(candidates, limit=10)] == [
        "beta",
        "alpha",
    ]
    since = datetime(2026, 3, 1, tzinfo=UTC)
    assert [
        text for _key, text in await store.pending_texts(candidates, limit=10, since=since)
    ] == ["beta"]
    assert await store.count_pending(candidates, since=since) == 1
    store.close()


async def test_vector_store_refuses_vectors_of_a_space_it_left(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await store.use_space(HEADER)
    await _apply(store, _change("one", [_passage("alpha")]))
    other = VectorHeader(provider_id="p", model_id="m2", dimension=3)

    assert await store.store_vectors(other, {text_hash("alpha"): VECTORS["alpha"]}) is False
    assert await store.count_pending(_candidates("one")) == 1
    with pytest.raises(VectorStoreError):
        await store.store_vectors(HEADER, {text_hash("alpha"): [1.0, 0.0]})
    store.close()


async def test_vector_store_space_change_queues_every_passage_again(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha")], "two": [_passage("beta")]})
    wider = VectorHeader(provider_id="p", model_id="m", dimension=4)

    assert await store.use_space(wider) is True

    assert await store.read_header() == wider
    assert await store.count_pending(_candidates("one", "two")) == 2
    matches = await store.knn_search(
        header=wider, query_vector=[1.0, 0.0, 0.0, 0.0], limit=5, candidates=_candidates("one")
    )
    assert matches == []
    with pytest.raises(VectorStoreError):
        await _nearest(store, [1.0, 0.0, 0.0], _candidates("one"))
    store.close()


# ---------------------------------------------------------------------------
# Catalog refresh
# ---------------------------------------------------------------------------


async def test_vector_store_refresh_keeps_unchanged_passages_and_drops_vanished_ones(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha"), _passage("beta")]})
    before = _refs(store.path)

    await _apply(
        store,
        _change(
            "one",
            [_passage("alpha"), _passage("beta grown", passage_id="id-beta")],
            revision=2,
            previous=("generation", 1),
        ),
    )

    after = _refs(store.path)
    assert after["alpha"] == before["alpha"]
    assert set(after) == {"alpha", "beta grown"}
    assert await _embed_waiting(store) == ["beta grown"]
    assert await store.list_indexed_sessions("coder") == {"one": ("generation", 2)}
    with closing(_connect_store(store.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0] == 2
    store.close()


async def test_vector_store_skips_a_session_refreshed_by_another_writer(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"one": [_passage("alpha")]})

    await _apply(store, _change("one", [_passage("beta")], revision=3, previous=None))

    assert await store.list_indexed_sessions("coder") == {"one": ("generation", 1)}
    assert set(_refs(store.path)) == {"alpha"}
    store.close()


async def test_vector_store_stamps_a_session_without_passages(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await store.use_space(HEADER)

    await _apply(store, _change("inert", []))

    assert await store.list_indexed_sessions("coder") == {"inert": ("generation", 1)}
    store.close()


async def test_vector_store_prune_keeps_passages_another_session_shows(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    shared = _passage("alpha")
    await _index(store, {"origin": [shared, _passage("beta")], "fork": [shared]})

    await _apply(store, pruned=["origin"])

    assert await store.list_indexed_sessions("coder") == {"fork": ("generation", 1)}
    assert set(_refs(store.path)) == {"alpha"}
    assert await _nearest(store, [1.0, 0.0, 0.0], _candidates("fork")) == [("fork", "alpha")]

    await store.remove_session("coder", None, "fork")

    assert _refs(store.path) == {}
    with closing(_connect_store(store.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0] == 0
    store.close()


# ---------------------------------------------------------------------------
# KNN and attribution
# ---------------------------------------------------------------------------


async def test_vector_store_knn_returns_nearest_passages_by_cosine(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await _index(
        store,
        {"a": [_passage("alpha")], "b": [_passage("beta")], "d": [_passage("delta")]},
    )

    assert await _nearest(store, [1.0, 0.1, 0.0], _candidates("a", "b", "d"), limit=2) == [
        ("a", "alpha"),
        ("d", "delta"),
    ]
    with pytest.raises(VectorStoreError):
        await store.knn_search(
            header=VectorHeader(provider_id="p", model_id="other", dimension=3),
            query_vector=[1.0, 0.0, 0.0],
            limit=1,
            candidates=_candidates("a"),
        )
    store.close()


async def test_vector_store_shared_passage_is_reported_once_by_owner_then_newest(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    shared = _passage("alpha")
    await store.use_space(HEADER)
    await _apply(
        store,
        _change("origin", [shared]),
        _change("older", [shared, _passage("beta", end_message_id="o1")], owned=[False, True]),
        _change("newer", [shared], owned=[False]),
    )
    await _embed_waiting(store)
    everyone = Candidates("coder", "", {"origin": 1, "older": 2, "newer": 3})

    assert await _nearest(store, [1.0, 0.0, 0.0], everyone) == [
        ("origin", "alpha"),
        ("older", "beta"),
    ]
    without_origin = Candidates("coder", "", {"older": 2, "newer": 3})
    assert await _nearest(store, [1.0, 0.0, 0.0], without_origin) == [
        ("newer", "alpha"),
        ("older", "beta"),
    ]
    only_older = Candidates("coder", "", {"older": 2})
    assert await _nearest(store, [1.0, 0.0, 0.0], only_older) == [
        ("older", "alpha"),
        ("older", "beta"),
    ]
    store.close()


@pytest.mark.parametrize("hidden_count", [3, 40])
async def test_vector_store_candidate_filter_runs_inside_knn_without_starving(
    tmp_path: Path, hidden_count: int
) -> None:
    store = VectorStore(tmp_path)
    hidden = {
        f"hidden-{index}": [_passage("alpha", passage_id=f"h{index}", end_message_id=f"h{index}")]
        for index in range(hidden_count)
    }
    await _index(store, {**hidden, "visible": [_passage("delta")], "far": [_passage("gamma")]})

    matches = await _nearest(
        store,
        [1.0, 0.0, 0.0],
        _candidates("visible", "far", "not-indexed-yet"),
        limit=2,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 12, 31, tzinfo=UTC),
    )

    assert matches == [("visible", "delta"), ("far", "gamma")]
    store.close()


async def test_vector_store_treats_a_non_canonical_passage_timestamp_as_damage(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    await store.use_space(HEADER)
    await _apply(
        store, _change("offset", [_passage("alpha", timestamp="2026-05-01T12:00:00+00:00")])
    )

    with pytest.raises(DatabaseCorruptError) as caught:
        await store.store_vectors(HEADER, {text_hash("alpha"): VECTORS["alpha"]})

    assert projection_failure(caught.value) == "rebuild"
    assert await store.count_pending(_candidates("offset")) == 1
    store.close()


async def test_vector_store_time_filters_exclude_passages_outside_the_period(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    await _index(
        store,
        {
            "old": [_passage("alpha", timestamp="2026-01-01T00:00:00.000000Z")],
            "new": [_passage("delta", timestamp="2026-06-01T00:00:00.000000Z")],
        },
    )

    matches = await _nearest(
        store, [1.0, 0.0, 0.0], _candidates("old", "new"), since=datetime(2026, 3, 1, tzinfo=UTC)
    )

    assert matches == [("new", "delta")]
    store.close()


# ---------------------------------------------------------------------------
# Scope isolation: the same Session UUID in two scopes never collides
# ---------------------------------------------------------------------------


async def test_vector_store_same_uuid_in_two_scopes_are_distinct(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"shared": [_passage("alpha")]})
    await _index(store, {"shared": [_passage("beta")]}, project="proj")

    assert set(await store.list_indexed_sessions("coder")) == {"shared"}
    assert set(await store.list_indexed_sessions("coder", "proj")) == {"shared"}
    assert await _nearest(store, [1.0, 0.0, 0.0], _candidates("shared")) == [("shared", "alpha")]
    assert await _nearest(store, [1.0, 0.0, 0.0], _candidates("shared", project="proj")) == [
        ("shared", "beta")
    ]
    store.close()


async def test_vector_store_prune_and_remove_affect_only_the_named_scope(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    await _index(store, {"shared": [_passage("alpha")]})
    await _index(store, {"shared": [_passage("beta")], "other": []}, project="proj")

    await _apply(store, pruned=["shared"], project="proj")
    await store.remove_session("coder", "proj", "other")

    assert set(await store.list_indexed_sessions("coder")) == {"shared"}
    assert await store.list_indexed_sessions("coder", "proj") == {}
    assert set(_refs(store.path)) == {"alpha"}
    store.close()
