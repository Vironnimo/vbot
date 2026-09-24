"""Tests for the sqlite-vec vector store."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlite_vec  # type: ignore[import-untyped]

from core.recall.passages import Passage
from core.recall.vector_store import (
    RefreshPlan,
    SessionPassages,
    StoredPassage,
    VectorHeader,
    VectorStore,
    VectorStoreError,
)
from core.sessions.schema import JOURNAL_MODE_DELETE

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

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
    timestamp: str = "2026-05-01T12:00:00+00:00",
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


def _plan(
    store: VectorStore,
    sessions: Mapping[str, Sequence[Passage]],
    *,
    header: VectorHeader = HEADER,
    agent_id: str = "coder",
    project_id: str = "",
    revision: int = 1,
    pruned: Sequence[str] = (),
) -> RefreshPlan:
    indexed = store.list_indexed_sessions(agent_id, project_id)
    return store.plan_refresh(
        agent_id,
        project_id,
        header=header,
        sessions=[
            SessionPassages(
                session_id=session_id,
                version=("generation", revision),
                previous_version=indexed.get(session_id),
                passages=tuple(passages),
            )
            for session_id, passages in sessions.items()
        ],
        pruned_session_ids=pruned,
    )


def _refresh(
    store: VectorStore,
    sessions: Mapping[str, Sequence[Passage]],
    *,
    header: VectorHeader = HEADER,
    agent_id: str = "coder",
    project_id: str = "",
    revision: int = 1,
    pruned: Sequence[str] = (),
    vectors: Mapping[str, list[float]] = VECTORS,
) -> RefreshPlan:
    """Plan and apply one refresh, embedding each requested text from *vectors*."""

    plan = _plan(
        store,
        sessions,
        header=header,
        agent_id=agent_id,
        project_id=project_id,
        revision=revision,
        pruned=pruned,
    )
    store.apply_refresh(plan, [vectors[text] for text in plan.texts_to_embed])
    return plan


def _rows(store: VectorStore, session_id: str, *, project_id: str = "") -> list[tuple[int, str]]:
    """Return ``(rowid, text)`` for one Session's rows that have a vector."""

    connection = sqlite3.connect(store.path)
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        rows = connection.execute(
            """
            SELECT p.rowid, p.text FROM passages AS p
            JOIN session_vectors AS v ON v.rowid = p.rowid
            WHERE p.project_id = ? AND p.agent_id = 'coder' AND p.session_id = ?
            ORDER BY p.rowid
            """,
            (project_id, session_id),
        ).fetchall()
    finally:
        connection.close()
    return [(int(rowid), str(text)) for rowid, text in rows]


def _vec0_row_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        row = connection.execute("SELECT COUNT(*) FROM session_vectors").fetchone()
    finally:
        connection.close()
    return int(row[0])


def _nearest(
    store: VectorStore,
    query: list[float],
    *,
    header: VectorHeader = HEADER,
    limit: int = 10,
    **filters: object,
) -> list[tuple[StoredPassage, float]]:
    return store.knn_search(
        header=header,
        query_vector=query,
        limit=limit,
        **filters,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# File, journal, header and schema lifecycle
# ---------------------------------------------------------------------------


def test_vector_store_uses_required_rollback_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.recall import vector_store

    monkeypatch.setattr(vector_store, "required_journal_mode", lambda _version: JOURNAL_MODE_DELETE)
    store = VectorStore(tmp_path)

    connection = store._connect()
    try:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()

    assert mode == JOURNAL_MODE_DELETE


def test_vector_store_reset_removes_rollback_journal(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    rollback_journal = Path(f"{store.path}-journal")
    rollback_journal.parent.mkdir(parents=True, exist_ok=True)
    rollback_journal.write_bytes(b"stale")

    store.reset_index()

    assert rollback_journal.exists() is False


def test_vector_store_fresh_file_is_empty(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)

    assert store.read_header() is None
    assert store.list_indexed_sessions("coder") == {}
    store.delete_session("coder", "", "never-indexed")  # must not raise


def test_vector_store_first_refresh_creates_file_header_and_vec0_table(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    header = VectorHeader(
        provider_id="openrouter",
        model_id="model-a",
        dimension=3,
        space_fingerprint="space-a",
        index_policy="passage-v2",
        response_model_id="served/model-a-202607",
    )

    _refresh(store, {"s1": [_passage("alpha")]}, header=header)

    assert store.path == tmp_path / "recall" / "session_passage_vectors.sqlite"
    assert store.path.is_file()
    assert store.read_header() == header
    assert _vec0_row_count(store.path) == 1


def test_vector_store_refresh_requires_resolved_dimension(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)

    with pytest.raises(VectorStoreError):
        _plan(store, {"s1": [_passage("alpha")]}, header=replace(HEADER, dimension=0))


@pytest.mark.parametrize(
    "changed",
    [
        replace(HEADER, provider_id="other"),
        replace(HEADER, model_id="other"),
        replace(HEADER, response_model_id="served/other"),
        replace(HEADER, space_fingerprint="other-space"),
        replace(HEADER, index_policy="other-policy"),
        replace(HEADER, dimension=4),
    ],
)
def test_vector_store_refuses_refresh_in_another_embedding_space(
    tmp_path: Path, changed: VectorHeader
) -> None:
    """Vectors of one embedding space never mix with another; the caller resets first."""

    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")]})

    with pytest.raises(VectorStoreError):
        _plan(store, {"s2": [_passage("beta")]}, header=changed)

    assert store.read_header() == HEADER
    assert set(store.list_indexed_sessions("coder")) == {"s1"}


def test_vector_store_apply_rejects_plan_whose_store_changed_space(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    stale = _plan(store, {"s1": [_passage("alpha")]})
    other = replace(HEADER, model_id="other")
    _refresh(store, {"s2": [_passage("beta")]}, header=other)

    with pytest.raises(VectorStoreError, match="header changed"):
        store.apply_refresh(stale, [VECTORS["alpha"]])

    assert store.read_header() == other


def test_vector_store_apply_rejects_plan_whose_store_was_discarded(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")]})
    stale = _plan(store, {"s2": [_passage("beta")]})
    store.reset_index()

    with pytest.raises(VectorStoreError, match="discarded"):
        store.apply_refresh(stale, [VECTORS["beta"]])


def test_vector_store_rejects_populated_file_of_another_schema_version(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")]})
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA user_version = 999")
    finally:
        connection.close()

    with pytest.raises(VectorStoreError):
        store.read_header()
    with pytest.raises(VectorStoreError):
        store.list_indexed_sessions("coder")
    store.delete_session("coder", "", "s1")  # tolerated; the next search rebuilds

    store.reset_index()
    _refresh(store, {"s2": [_passage("beta")]})
    assert set(store.list_indexed_sessions("coder")) == {"s2"}


def test_vector_store_rejects_vectors_that_do_not_fit_the_plan(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")]})
    plan = _plan(store, {"s2": [_passage("beta")]})

    with pytest.raises(VectorStoreError):
        store.apply_refresh(plan, [[0.1, 0.2, 0.3, 0.4]])
    with pytest.raises(VectorStoreError):
        store.apply_refresh(plan, [])

    assert set(store.list_indexed_sessions("coder")) == {"s1"}


# ---------------------------------------------------------------------------
# Incremental refresh
# ---------------------------------------------------------------------------


def test_vector_store_refresh_keeps_unchanged_rows_and_embeds_only_new_texts(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    alpha = _passage("alpha")
    first = _refresh(store, {"s1": [alpha, _passage("beta", passage_id="tail")]})
    assert first.texts_to_embed == ("alpha", "beta")
    [(alpha_rowid, _alpha), _beta] = _rows(store, "s1")

    # The tail Passage keeps its id but grows; a new Passage follows it.
    second = _refresh(
        store,
        {"s1": [alpha, _passage("beta grown", passage_id="tail"), _passage("gamma")]},
        revision=2,
    )

    assert second.texts_to_embed == ("beta grown", "gamma")
    rows = _rows(store, "s1")
    assert (alpha_rowid, "alpha") in rows
    assert sorted(text for _rowid, text in rows) == ["alpha", "beta grown", "gamma"]
    assert store.list_indexed_sessions("coder") == {"s1": ("generation", 2)}


def test_vector_store_refresh_replaces_rows_whose_boundaries_moved(tmp_path: Path) -> None:
    """Same id and text with other boundary metadata is a different row."""

    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha", end_message_id="m1")]})

    plan = _refresh(store, {"s1": [_passage("alpha", end_message_id="m2")]}, revision=2)

    # The row is rewritten, but its text keeps the stored vector.
    assert plan.texts_to_embed == ()
    [(_rowid, text)] = _rows(store, "s1")
    assert text == "alpha"
    [(passage, _distance)] = _nearest(store, VECTORS["alpha"])
    assert passage.end_message_id == "m2"


def test_vector_store_refresh_removes_vanished_passages(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha"), _passage("beta"), _passage("gamma")]})

    plan = _refresh(store, {"s1": [_passage("alpha")]}, revision=2)

    assert plan.texts_to_embed == ()
    assert [text for _rowid, text in _rows(store, "s1")] == ["alpha"]
    assert _vec0_row_count(store.path) == 1


def test_vector_store_refresh_reuses_vectors_across_sessions(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha"), _passage("beta")]})

    plan = _refresh(store, {"s2": [_passage("alpha", passage_id="fork-id")]})

    assert plan.texts_to_embed == ()
    nearest = _nearest(store, VECTORS["alpha"], limit=2)
    assert [(passage.session_id, passage.text) for passage, _ in nearest] == [
        ("s1", "alpha"),
        ("s2", "alpha"),
    ]
    assert nearest[1][1] == pytest.approx(0.0, abs=1e-5)


def test_vector_store_refresh_embeds_each_distinct_text_once(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)

    plan = _refresh(
        store,
        {
            "s1": [_passage("alpha", passage_id="a1"), _passage("alpha", passage_id="a2")],
            "s2": [_passage("alpha", passage_id="a3")],
        },
    )

    assert plan.texts_to_embed == ("alpha",)
    assert len(_rows(store, "s1")) == 2
    assert len(_rows(store, "s2")) == 1


def test_vector_store_refresh_matches_duplicate_rows_as_a_multiset(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    duplicate = _passage("alpha")
    _refresh(store, {"s1": [duplicate, duplicate]})
    [first, second] = _rows(store, "s1")

    _refresh(store, {"s1": [duplicate]}, revision=2)

    remaining = _rows(store, "s1")
    assert len(remaining) == 1
    assert remaining[0] in (first, second)


def test_vector_store_stamps_session_without_passages(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")], "empty": []})

    assert store.list_indexed_sessions("coder") == {
        "s1": ("generation", 1),
        "empty": ("generation", 1),
    }

    _refresh(store, {"s1": []}, revision=2)

    assert _rows(store, "s1") == []
    assert store.list_indexed_sessions("coder")["s1"] == ("generation", 2)


def test_vector_store_skips_session_refreshed_by_another_writer(tmp_path: Path) -> None:
    """A plan applies only on top of the stamp it was planned against."""

    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")]})
    stale = _plan(store, {"s1": [_passage("beta")]}, revision=2)
    _refresh(store, {"s1": [_passage("gamma")]}, revision=3)

    store.apply_refresh(stale, [VECTORS["beta"]])

    assert [text for _rowid, text in _rows(store, "s1")] == ["gamma"]
    assert store.list_indexed_sessions("coder") == {"s1": ("generation", 3)}


def test_vector_store_empty_plan_writes_nothing(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")]})
    modified = store.path.stat().st_mtime_ns

    plan = _plan(store, {})
    assert plan.is_empty
    store.apply_refresh(plan)

    assert store.path.stat().st_mtime_ns == modified


def test_vector_store_prunes_named_sessions(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"keep": [_passage("alpha")], "drop": [_passage("beta")]})

    _refresh(store, {}, pruned=["drop"])

    assert set(store.list_indexed_sessions("coder")) == {"keep"}
    assert _rows(store, "drop") == []
    assert _vec0_row_count(store.path) == 1


def test_vector_store_delete_session_removes_rows_and_stamp(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")], "s2": [_passage("beta")]})

    store.delete_session("coder", "", "s1")

    assert set(store.list_indexed_sessions("coder")) == {"s2"}
    assert _rows(store, "s1") == []


# ---------------------------------------------------------------------------
# KNN
# ---------------------------------------------------------------------------


def test_vector_store_knn_search_returns_nearest_passages_by_cosine(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(
        store,
        {
            "near": [_passage("alpha", start_message_id="n1", end_message_id="n2")],
            "mid": [_passage("delta")],
            "far": [_passage("gamma")],
        },
    )

    results = _nearest(store, [1.0, 0.0, 0.0], limit=3)

    assert [passage.session_id for passage, _ in results] == ["near", "mid", "far"]
    assert results[0][0] == StoredPassage(
        session_id="near",
        passage_id="id-alpha",
        text="alpha",
        start_message_id="n1",
        end_message_id="n2",
        start_timestamp="2026-05-01T12:00:00+00:00",
        end_timestamp="2026-05-01T12:00:00+00:00",
        start_role="user",
        end_role="user",
    )
    assert results[0][1] == pytest.approx(0.0, abs=1e-5)
    assert results[-1][1] > results[0][1]


def test_vector_store_knn_header_mismatch_is_read_only(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"s1": [_passage("alpha")]})

    with pytest.raises(VectorStoreError):
        _nearest(store, [1.0, 0.0, 0.0, 0.0], header=replace(HEADER, dimension=4))
    with pytest.raises(VectorStoreError):
        _nearest(store, [1.0, 0.0, 0.0], header=replace(HEADER, model_id="other"))

    assert store.read_header() == HEADER
    assert _nearest(store, [1.0, 0.0, 0.0])


def test_vector_store_knn_search_applies_session_filters_before_ranking(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(
        store,
        {"excluded": [_passage("alpha")], "included": [_passage("delta")], "other": []},
    )

    excluded = _nearest(store, [1.0, 0.0, 0.0], limit=1, session_ids={"included", "other"})
    selected = _nearest(store, [1.0, 0.0, 0.0], limit=1, session_ids={"included"})

    assert [passage.session_id for passage, _ in excluded] == ["included"]
    assert [passage.session_id for passage, _ in selected] == ["included"]
    assert _nearest(store, [1.0, 0.0, 0.0], session_ids=set()) == []
    assert _nearest(store, [1.0, 0.0, 0.0], session_ids={"never-indexed"}) == []


@pytest.mark.parametrize("excluded_count", [8, 40])
def test_vector_store_knn_search_excludes_many_sessions_without_starving(
    tmp_path: Path, excluded_count: int
) -> None:
    """Session filters run inside KNN, even beyond the constraints one vec0 query accepts."""

    store = VectorStore(tmp_path)
    excluded_ids = [f"hidden-{index}" for index in range(excluded_count)]
    _refresh(
        store,
        {
            **{
                session_id: [_passage("alpha", passage_id=session_id)]
                for session_id in excluded_ids
            },
            "visible": [_passage("delta")],
            "far": [_passage("gamma")],
        },
    )

    results = _nearest(
        store,
        [1.0, 0.0, 0.0],
        limit=2,
        agent_id="coder",
        session_ids={"visible", "far", "not-indexed-yet"},
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 12, 31, tzinfo=UTC),
    )

    assert [passage.session_id for passage, _ in results] == ["visible", "far"]


def test_vector_store_time_filters_keep_passages_with_invalid_timestamps(
    tmp_path: Path,
) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"malformed-time": [_passage("alpha", timestamp="not-a-timestamp")]})

    results = _nearest(
        store,
        [1.0, 0.0, 0.0],
        limit=1,
        agent_id="coder",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 12, 31, tzinfo=UTC),
    )

    assert len(results) == 1


def test_vector_store_time_filters_exclude_passages_outside_the_period(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(
        store,
        {
            "old": [_passage("alpha", timestamp="2026-01-01T00:00:00+00:00")],
            "new": [_passage("delta", timestamp="2026-06-01T00:00:00+00:00")],
        },
    )

    results = _nearest(
        store, [1.0, 0.0, 0.0], agent_id="coder", since=datetime(2026, 3, 1, tzinfo=UTC)
    )

    assert [passage.session_id for passage, _ in results] == ["new"]


# ---------------------------------------------------------------------------
# Scope isolation — the same Session UUID in two scopes never collides
# ---------------------------------------------------------------------------


def test_vector_store_same_uuid_in_two_scopes_are_distinct(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"shared": [_passage("alpha")]}, project_id="")
    _refresh(store, {"shared": [_passage("beta")]}, project_id="proj")

    assert set(store.list_indexed_sessions("coder", "")) == {"shared"}
    assert set(store.list_indexed_sessions("coder", "proj")) == {"shared"}
    assert _vec0_row_count(store.path) == 2
    global_hits = _nearest(store, [1.0, 0.0, 0.0], agent_id="coder", project_id="")
    project_hits = _nearest(store, [1.0, 0.0, 0.0], agent_id="coder", project_id="proj")
    assert [passage.text for passage, _ in global_hits] == ["alpha"]
    assert [passage.text for passage, _ in project_hits] == ["beta"]


def test_vector_store_refresh_of_one_scope_leaves_other_scope_intact(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"shared": [_passage("alpha")]}, project_id="")
    _refresh(store, {"shared": [_passage("beta")]}, project_id="proj")

    _refresh(store, {"shared": [_passage("gamma")]}, project_id="proj", revision=2)
    _refresh(store, {}, project_id="proj", pruned=["other"])

    assert [text for _rowid, text in _rows(store, "shared", project_id="")] == ["alpha"]
    assert [text for _rowid, text in _rows(store, "shared", project_id="proj")] == ["gamma"]


def test_vector_store_prune_and_delete_affect_only_the_named_scope(tmp_path: Path) -> None:
    store = VectorStore(tmp_path)
    _refresh(store, {"shared": [_passage("alpha")]}, project_id="")
    _refresh(store, {"shared": [_passage("beta")], "other": []}, project_id="proj")

    _refresh(store, {}, project_id="proj", pruned=["shared"])
    store.delete_session("coder", "proj", "other")

    assert set(store.list_indexed_sessions("coder", "")) == {"shared"}
    assert store.list_indexed_sessions("coder", "proj") == {}
    assert [text for _rowid, text in _rows(store, "shared", project_id="")] == ["alpha"]
