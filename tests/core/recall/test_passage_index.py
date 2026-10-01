"""The one disposable Passage index: catalog, literal FTS, vectors and indexing state."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.database import (
    APPLICATION_IDS,
    DatabaseCorruptError,
    projection_failure,
)
from core.model_tasks import EmbeddingUsage
from core.recall import (
    HybridRecallBackend,
    PassageIndex,
    PassageIndexError,
    RecallBackendContext,
    RecallSearchError,
    VectorHeader,
)
from core.recall import passage_index as passage_index_module
from core.recall._passage_catalog import (
    Candidates,
    CatalogPlan,
    PassageCatalog,
    SessionPassages,
    text_hash,
)
from core.recall.passages import Passage
from core.sessions import ChatSession, ChatSessionManager
from tests.core.recall.recall_test_support import connect_store, request, timestamp

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::DeprecationWarning")]

HEADER = VectorHeader(provider_id="p", model_id="m", dimension=3)
VECTORS: dict[str, list[float]] = {
    "alpha": [1.0, 0.0, 0.0],
    "beta": [0.0, 1.0, 0.0],
    "gamma": [0.0, 0.0, 1.0],
    "delta": [0.7, 0.7, 0.0],
    "beta grown": [0.1, 0.9, 0.0],
}
AT = datetime(2026, 5, 1, 12, tzinfo=UTC)


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
    index: PassageIndex,
    *changes: SessionPassages,
    project: str = "",
    pruned: Sequence[str] = (),
    agent_id: str = "coder",
) -> None:
    await index.apply(CatalogPlan(agent_id, project, tuple(pruned), tuple(changes)))


async def _index(
    index: PassageIndex,
    sessions: Mapping[str, Sequence[Passage]],
    *,
    project: str = "",
    header: VectorHeader = HEADER,
) -> None:
    """Pin *header*, add new Sessions and embed every waiting Passage."""
    await index.use_space(header)
    await _apply(
        index,
        *(_change(session_id, passages) for session_id, passages in sessions.items()),
        project=project,
    )
    await _embed_waiting(index, header)


async def _embed_waiting(
    index: PassageIndex,
    header: VectorHeader = HEADER,
    *,
    usage: EmbeddingUsage | None = None,
) -> list[str]:
    batch = await index.pending_texts(limit=1000)
    assert await index.store_vectors(
        header, {key: VECTORS[text] for key, text in batch}, usage=usage
    )
    return sorted(text for _key, text in batch)


def _candidates(*session_ids: str, project: str = "") -> Candidates:
    return Candidates(
        "coder", project, {session_id: order for order, session_id in enumerate(session_ids)}
    )


async def _nearest(
    index: PassageIndex,
    vector: Sequence[float],
    candidates: Candidates,
    *,
    limit: int = 10,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[tuple[str, str]]:
    result = await index.knn_search(
        header=HEADER,
        query_vector=vector,
        limit=limit,
        candidates=candidates,
        since=since,
        until=until,
    )
    return [(session_id, passage.text) for passage, session_id, _distance in result.matches]


def _identity(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        identity: dict[str, object] = dict(
            connection.execute("SELECT key, value FROM kernel_meta").fetchall()
        )
        identity["application_id"] = connection.execute("PRAGMA application_id").fetchone()[0]
    return identity


def _refs(path: Path) -> dict[str, int]:
    with closing(connect_store(path)) as connection:
        return {
            str(text): int(ref)
            for ref, text in connection.execute("SELECT passage_ref, text FROM passages")
        }


# ---------------------------------------------------------------------------
# The kernel disposable projection
# ---------------------------------------------------------------------------


def _delete(path: Path) -> None:
    path.unlink()


def _outdate(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE kernel_meta SET value = '0' WHERE key = 'projection_version'")


def _overwrite(path: Path) -> None:
    path.write_bytes(b"not a sqlite database")


@pytest.mark.parametrize(
    "damage",
    [None, _delete, _outdate, _overwrite],
    ids=["current", "deleted", "old-version", "unreadable"],
)
async def test_passage_index_opens_as_the_current_disposable_projection(
    tmp_path: Path, damage: Callable[[Path], None] | None
) -> None:
    index = PassageIndex(tmp_path)
    await _index(index, {"one": [_passage("alpha")]})
    old_identity = _identity(index.path)
    index.close()
    if damage is not None:
        damage(index.path)

    reopened = PassageIndex(tmp_path)
    indexed = await reopened.list_indexed_sessions("coder")
    await _index(reopened, {"two": [_passage("beta")]})

    identity = _identity(reopened.path)
    assert reopened.path == tmp_path / "recall" / "passage_index.sqlite"
    assert identity["application_id"] == APPLICATION_IDS["recall_index"]
    assert identity["database_name"] == "recall_index"
    assert identity["projection_version"] == "1"
    # A stale or unreadable index is discarded, never migrated.
    assert (identity["database_id"] == old_identity["database_id"]) is (damage is None)
    assert set(indexed) == ({"one"} if damage is None else set())
    assert await _nearest(reopened, [0.0, 1.0, 0.0], _candidates("two")) == [("two", "beta")]
    with closing(connect_store(reopened.path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    # One database holds the catalog, both literal indexes, the vectors and the
    # indexing state; Messages stay in the Session store.
    assert {
        "passages",
        "passages_fts",
        "passages_fts_tokens",
        "passage_vectors",
        "pending_vectors",
        "skipped_texts",
        "indexer_state",
    } <= tables
    assert not {"messages", "messages_fts"} & tables
    reopened.close()


async def test_pinned_space_without_its_vector_table_is_damage(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await _index(index, {"one": [_passage("alpha")]})
    with closing(connect_store(index.path)) as connection, connection:
        connection.execute("DROP TABLE passage_vectors")

    with pytest.raises(Exception) as failure:
        await index.read_header()

    assert projection_failure(failure.value) == "rebuild"
    index.close()


# ---------------------------------------------------------------------------
# Embedding space, the embedding queue and skipped texts
# ---------------------------------------------------------------------------


async def test_first_space_pins_header_and_queues_new_passages(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    assert await index.read_header() is None

    assert await index.use_space(HEADER) is True
    assert await index.use_space(HEADER) is False
    await _apply(index, _change("one", [_passage("alpha"), _passage("beta")]))

    assert await index.read_header() == HEADER
    assert await index.count_pending(_candidates("one")) == 2
    assert await _embed_waiting(index) == ["alpha", "beta"]
    assert await index.count_pending(_candidates("one")) == 0
    index.close()


async def test_each_text_is_embedded_once_for_every_passage_that_shows_it(
    tmp_path: Path,
) -> None:
    index = PassageIndex(tmp_path)
    await index.use_space(HEADER)
    await _apply(index, _change("one", [_passage("alpha")]))
    await _apply(index, _change("one", [_passage("alpha")]), project="proj")

    # Waiting Passages with the same text wait for one vector.
    assert await index.pending_texts(limit=10) == [(text_hash("alpha"), "alpha")]
    assert await index.store_vectors(HEADER, {text_hash("alpha"): VECTORS["alpha"]})
    assert await index.count_pending(_candidates("one")) == 0
    assert await index.count_pending(_candidates("one", project="proj")) == 0

    # A new Passage with a stored text reuses its vector without waiting.
    await _apply(
        index, _change("two", [_passage("alpha", passage_id="other", end_message_id="m2")])
    )
    assert await index.pending_texts(limit=10) == []
    assert await _nearest(index, [1.0, 0.0, 0.0], _candidates("two")) == [("two", "alpha")]
    assert await _nearest(index, [1.0, 0.0, 0.0], _candidates("one", project="proj")) == [
        ("one", "alpha")
    ]
    index.close()


async def test_waiting_texts_are_newest_first_and_skipped_texts_never_wait(
    tmp_path: Path,
) -> None:
    index = PassageIndex(tmp_path)
    await index.use_space(HEADER)
    await _apply(
        index,
        _change("old", [_passage("alpha", timestamp="2026-01-01T00:00:00.000000Z")]),
        _change("new", [_passage("beta", timestamp="2026-06-01T00:00:00.000000Z")]),
        _change("newest", [_passage("gamma", timestamp="2026-07-01T00:00:00.000000Z")]),
    )

    assert [text for _key, text in await index.pending_texts(limit=2)] == ["gamma", "beta"]
    candidates = _candidates("old", "new")
    since = datetime(2026, 3, 1, tzinfo=UTC)
    assert await index.count_pending(candidates) == 2
    assert await index.count_pending(candidates, since=since) == 1

    assert await index.record_skipped(HEADER, {text_hash("beta"): "context_overflow"}, at=AT)

    assert [text for _key, text in await index.pending_texts(limit=10)] == ["gamma", "alpha"]
    assert await index.count_pending(candidates) == 1
    counts = await index.counts()
    assert (counts.indexed, counts.waiting, counts.skipped) == (0, 2, 1)
    assert (counts.waiting_texts, counts.waiting_characters) == (2, len("gamma") + len("alpha"))
    # A space the index has left records nothing.
    other = VectorHeader(provider_id="p", model_id="m2", dimension=3)
    assert await index.record_skipped(other, {text_hash("gamma"): "provider_rejected"}, at=AT) is (
        False
    )
    assert (await index.counts()).skipped == 1
    index.close()


async def test_index_refuses_invalid_spaces_and_vectors_of_a_space_it_left(
    tmp_path: Path,
) -> None:
    index = PassageIndex(tmp_path)
    with pytest.raises(PassageIndexError):
        await index.use_space(VectorHeader(provider_id="p", model_id="m", dimension=0))
    await index.use_space(HEADER)
    await _apply(index, _change("one", [_passage("alpha")]))
    other = VectorHeader(provider_id="p", model_id="m2", dimension=3)

    assert await index.store_vectors(other, {text_hash("alpha"): VECTORS["alpha"]}) is False
    assert await index.count_pending(_candidates("one")) == 1
    with pytest.raises(PassageIndexError):
        await index.store_vectors(HEADER, {text_hash("alpha"): [1.0, 0.0]})
    index.close()


async def test_spent_usage_accumulates_with_stored_vectors(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await index.use_space(HEADER)
    await _apply(index, _change("one", [_passage("alpha")]), _change("two", [_passage("beta")]))
    usage = EmbeddingUsage(
        requests=1, token_reports=1, input_tokens=7, total_tokens=7, cost_reports=1, cost=0.25
    )
    assert (await index.spent()).usage == EmbeddingUsage()

    await _embed_waiting(index, usage=usage)
    await _embed_waiting(index, usage=usage)
    await index.mark_completed(AT)

    spent = await index.spent()
    assert spent.usage == usage.combined(usage)
    assert spent.last_completed_at == "2026-05-01T12:00:00.000000Z"
    counts = await index.counts()
    assert (counts.indexed, counts.waiting, counts.waiting_characters) == (2, 0, 0)
    index.close()


async def _change_space(index: PassageIndex) -> VectorHeader | None:
    wider = VectorHeader(provider_id="p", model_id="m", dimension=4)
    assert await index.use_space(wider) is True
    return wider


async def _reset(index: PassageIndex) -> VectorHeader | None:
    await index.reset_vectors()
    return None


@pytest.mark.parametrize("leave", [_change_space, _reset], ids=["space-change", "reset"])
async def test_leaving_a_space_queues_every_passage_and_clears_its_state(
    tmp_path: Path, leave: Callable[[PassageIndex], Any]
) -> None:
    index = PassageIndex(tmp_path)
    await index.use_space(HEADER)
    await _apply(index, _change("one", [_passage("alpha")]), _change("two", [_passage("beta")]))
    await index.record_skipped(HEADER, {text_hash("beta"): "provider_rejected"}, at=AT)
    await _embed_waiting(index, usage=EmbeddingUsage(requests=1, cost_reports=1, cost=0.5))
    await index.mark_completed(AT)

    header = await leave(index)

    assert await index.read_header() == header
    assert await index.count_pending(_candidates("one", "two")) == 2
    counts = await index.counts()
    assert (counts.indexed, counts.waiting, counts.skipped) == (0, 2, 0)
    assert await index.spent() == type(await index.spent())()
    with pytest.raises(PassageIndexError):
        await _nearest(index, [1.0, 0.0, 0.0], _candidates("one"))
    if header is not None:
        result = await index.knn_search(
            header=header,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            limit=5,
            candidates=_candidates("one"),
        )
        assert result.matches == ()
    index.close()


# ---------------------------------------------------------------------------
# Catalog refresh and scope eviction
# ---------------------------------------------------------------------------


async def test_refresh_keeps_unchanged_passages_and_drops_vanished_ones(
    tmp_path: Path,
) -> None:
    index = PassageIndex(tmp_path)
    await _index(index, {"one": [_passage("alpha"), _passage("beta")]})
    before = _refs(index.path)

    await _apply(
        index,
        _change(
            "one",
            [_passage("alpha"), _passage("beta grown", passage_id="id-beta")],
            revision=2,
            previous=("generation", 1),
        ),
    )

    after = _refs(index.path)
    assert after["alpha"] == before["alpha"]
    assert set(after) == {"alpha", "beta grown"}
    assert await _embed_waiting(index) == ["beta grown"]
    assert await index.list_indexed_sessions("coder") == {"one": ("generation", 2)}
    with closing(connect_store(index.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0] == 2
    index.close()


async def test_refresh_skips_a_session_refreshed_by_another_writer(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await _index(index, {"one": [_passage("alpha")]})

    await _apply(index, _change("one", [_passage("beta")], revision=3, previous=None))

    assert await index.list_indexed_sessions("coder") == {"one": ("generation", 1)}
    assert set(_refs(index.path)) == {"alpha"}
    index.close()


async def test_refresh_stamps_a_session_without_passages(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await index.use_space(HEADER)

    await _apply(index, _change("inert", []))

    assert await index.list_indexed_sessions("coder") == {"inert": ("generation", 1)}
    index.close()


async def test_prune_keeps_passages_another_session_shows(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    shared = _passage("alpha")
    await _index(index, {"origin": [shared, _passage("beta")], "fork": [shared]})

    await _apply(index, pruned=["origin"])

    assert await index.list_indexed_sessions("coder") == {"fork": ("generation", 1)}
    assert set(_refs(index.path)) == {"alpha"}
    assert await _nearest(index, [1.0, 0.0, 0.0], _candidates("fork")) == [("fork", "alpha")]

    await index.remove_session("coder", None, "fork")

    assert _refs(index.path) == {}
    with closing(connect_store(index.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0] == 0
    index.close()


async def test_prune_remove_and_scope_eviction_affect_only_the_named_scope(
    tmp_path: Path,
) -> None:
    index = PassageIndex(tmp_path)
    await _index(index, {"shared": [_passage("alpha")]})
    await _index(index, {"shared": [_passage("beta")], "other": []}, project="proj")
    await _apply(index, _change("own", [_passage("gamma")]), agent_id="writer")
    await _embed_waiting(index)

    assert await index.indexed_scopes() == {(None, "coder"), ("proj", "coder"), (None, "writer")}

    await _apply(index, pruned=["shared"], project="proj")
    await index.remove_session("coder", "proj", "other")
    await index.remove_scope("writer", None)

    assert set(await index.list_indexed_sessions("coder")) == {"shared"}
    assert await index.list_indexed_sessions("coder", "proj") == {}
    assert await index.list_indexed_sessions("writer") == {}
    assert await index.indexed_scopes() == {(None, "coder")}
    assert set(_refs(index.path)) == {"alpha"}
    index.close()


# ---------------------------------------------------------------------------
# KNN and attribution
# ---------------------------------------------------------------------------


async def test_knn_returns_nearest_passages_by_cosine(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await _index(
        index,
        {"a": [_passage("alpha")], "b": [_passage("beta")], "d": [_passage("delta")]},
    )

    assert await _nearest(index, [1.0, 0.1, 0.0], _candidates("a", "b", "d"), limit=2) == [
        ("a", "alpha"),
        ("d", "delta"),
    ]
    with pytest.raises(PassageIndexError):
        await index.knn_search(
            header=VectorHeader(provider_id="p", model_id="other", dimension=3),
            query_vector=[1.0, 0.0, 0.0],
            limit=1,
            candidates=_candidates("a"),
        )
    index.close()


async def test_knn_ranks_at_most_its_limit_and_reports_the_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(passage_index_module, "KNN_MAX_K", 2)
    index = PassageIndex(tmp_path)
    await _index(
        index,
        {"a": [_passage("alpha")], "b": [_passage("beta")], "d": [_passage("delta")]},
    )
    candidates = _candidates("a", "b", "d")

    capped = await index.knn_search(
        header=HEADER, query_vector=[1.0, 0.1, 0.0], limit=3, candidates=candidates
    )
    within = await index.knn_search(
        header=HEADER, query_vector=[1.0, 0.1, 0.0], limit=2, candidates=candidates
    )

    assert [passage.text for passage, _session, _distance in capped.matches] == ["alpha", "delta"]
    assert capped.truncated is True
    assert within.truncated is False
    index.close()


async def test_knn_query_the_vector_index_rejects_is_not_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = PassageIndex(tmp_path)
    await _index(index, {"a": [_passage("alpha")]})
    identity = _identity(index.path)
    # Above sqlite-vec's own limit of 4096 the extension rejects the query.
    monkeypatch.setattr(passage_index_module, "KNN_MAX_K", 5000)

    with pytest.raises(PassageIndexError) as failure:
        await _nearest(index, [1.0, 0.0, 0.0], _candidates("a"), limit=5000)
    await index.discard_if_damaged(failure.value)

    assert projection_failure(failure.value) is None
    assert _identity(index.path)["database_id"] == identity["database_id"]
    assert await _nearest(index, [1.0, 0.0, 0.0], _candidates("a")) == [("a", "alpha")]
    index.close()


async def test_shared_passage_is_reported_once_by_owner_then_newest(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    shared = _passage("alpha")
    await index.use_space(HEADER)
    await _apply(
        index,
        _change("origin", [shared]),
        _change("older", [shared, _passage("beta", end_message_id="o1")], owned=[False, True]),
        _change("newer", [shared], owned=[False]),
    )
    await _embed_waiting(index)
    everyone = Candidates("coder", "", {"origin": 1, "older": 2, "newer": 3})

    assert await _nearest(index, [1.0, 0.0, 0.0], everyone) == [
        ("origin", "alpha"),
        ("older", "beta"),
    ]
    without_origin = Candidates("coder", "", {"older": 2, "newer": 3})
    assert await _nearest(index, [1.0, 0.0, 0.0], without_origin) == [
        ("newer", "alpha"),
        ("older", "beta"),
    ]
    only_older = Candidates("coder", "", {"older": 2})
    assert await _nearest(index, [1.0, 0.0, 0.0], only_older) == [
        ("older", "alpha"),
        ("older", "beta"),
    ]
    index.close()


@pytest.mark.parametrize("hidden_count", [3, 40])
async def test_candidate_filter_runs_inside_knn_without_starving(
    tmp_path: Path, hidden_count: int
) -> None:
    index = PassageIndex(tmp_path)
    hidden = {
        f"hidden-{number}": [
            _passage("alpha", passage_id=f"h{number}", end_message_id=f"h{number}")
        ]
        for number in range(hidden_count)
    }
    await _index(index, {**hidden, "visible": [_passage("delta")], "far": [_passage("gamma")]})

    matches = await _nearest(
        index,
        [1.0, 0.0, 0.0],
        _candidates("visible", "far", "not-indexed-yet"),
        limit=2,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 12, 31, tzinfo=UTC),
    )

    assert matches == [("visible", "delta"), ("far", "gamma")]
    index.close()


async def test_non_canonical_passage_timestamp_is_damage(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await index.use_space(HEADER)
    await _apply(
        index, _change("offset", [_passage("alpha", timestamp="2026-05-01T12:00:00+00:00")])
    )

    with pytest.raises(DatabaseCorruptError) as caught:
        await index.store_vectors(HEADER, {text_hash("alpha"): VECTORS["alpha"]})

    assert projection_failure(caught.value) == "rebuild"
    assert await index.count_pending(_candidates("offset")) == 1
    index.close()


async def test_knn_time_filters_exclude_passages_outside_the_period(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await _index(
        index,
        {
            "old": [_passage("alpha", timestamp="2026-01-01T00:00:00.000000Z")],
            "new": [_passage("delta", timestamp="2026-06-01T00:00:00.000000Z")],
        },
    )

    matches = await _nearest(
        index, [1.0, 0.0, 0.0], _candidates("old", "new"), since=datetime(2026, 3, 1, tzinfo=UTC)
    )

    assert matches == [("new", "delta")]
    index.close()


async def test_same_session_uuid_in_two_scopes_stays_distinct(tmp_path: Path) -> None:
    index = PassageIndex(tmp_path)
    await _index(index, {"shared": [_passage("alpha")]})
    await _index(index, {"shared": [_passage("beta")]}, project="proj")

    assert set(await index.list_indexed_sessions("coder")) == {"shared"}
    assert set(await index.list_indexed_sessions("coder", "proj")) == {"shared"}
    assert await _nearest(index, [1.0, 0.0, 0.0], _candidates("shared")) == [("shared", "alpha")]
    assert await _nearest(index, [1.0, 0.0, 0.0], _candidates("shared", project="proj")) == [
        ("shared", "beta")
    ]
    index.close()


# ---------------------------------------------------------------------------
# Literal Passage search, through Hybrid's keyword arm
# ---------------------------------------------------------------------------


def literal_backend(tmp_path: Path, sessions: ChatSessionManager) -> HybridRecallBackend:
    """Hybrid without an embedding model: only its literal Passage arm ranks."""
    return HybridRecallBackend(RecallBackendContext(data_dir=tmp_path, sessions=sessions))


async def test_literal_search_returns_multiple_source_faithful_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    original = "needle  spacing\n" * 250
    sessions.create("coder", session_id="passages").append(
        ChatMessage.user(original, timestamp=timestamp(1))
    )

    page = await literal_backend(tmp_path, sessions).search_page(request("needle", limit=20))

    assert page.result_type == "passage"
    assert len(page.hits) > 1
    assert all(hit.session_id == "passages" for hit in page.hits)
    assert all(hit.sources == ("literal",) for hit in page.hits)
    assert page.hits[0].text in original


@pytest.mark.parametrize(
    ("query", "match_mode", "expected"),
    [
        ("alpha beta", "phrase", {"phrase"}),
        ("beta alpha", "phrase", set()),
        ("missing gamma", "any_term", {"term"}),
    ],
    ids=["phrase", "phrase-order", "any-term"],
)
async def test_literal_search_honours_the_match_mode(
    tmp_path: Path,
    sessions: ChatSessionManager,
    query: str,
    match_mode: str,
    expected: set[str],
) -> None:
    for session_id, text in (("phrase", "alpha beta"), ("term", "gamma")):
        sessions.create("coder", session_id=session_id).append(
            ChatMessage.user(text, timestamp=timestamp(1))
        )

    page = await literal_backend(tmp_path, sessions).search_page(
        request(query, match_mode=match_mode)
    )

    assert {hit.session_id for hit in page.hits} == expected


async def test_literal_time_filters_include_the_bounds_as_instants(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    sessions.create("coder", session_id="bounds").append(
        ChatMessage.user("bounded needle", timestamp=timestamp(1))
    )
    recall = literal_backend(tmp_path, sessions)
    instant = timestamp(1)
    # Another offset names the same instant; the bound compares as canonical text.
    local = instant.astimezone(timezone(timedelta(hours=2)))
    microsecond = timedelta(microseconds=1)

    exact = await recall.search_page(request("needle", since=local, until=local))
    after = await recall.search_page(request("needle", since=instant + microsecond))
    before = await recall.search_page(request("needle", until=instant - microsecond))

    assert [hit.session_id for hit in exact.hits] == ["bounds"]
    assert after.hits == ()
    assert before.hits == ()


async def test_short_literal_terms_use_the_token_index_without_loading_histories(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(6):
        sessions.create("coder", session_id=f"noise-{number}").append(
            ChatMessage.user("c c c", timestamp=timestamp(1))
        )
    for number in range(3):
        sessions.create("coder", session_id=f"csharp-{number}").append(
            ChatMessage.user(
                f"We compared C# generics with Java, part {number}", timestamp=timestamp(2)
            )
        )
    recall = literal_backend(tmp_path, sessions)
    await recall.search_page(request("generics"))

    def reject_load(*_args: object) -> None:
        raise AssertionError("an indexed short-term search must not load Session histories")

    for method in ("load", "load_active", "load_since"):
        monkeypatch.setattr(ChatSession, method, reject_load)
    # The better-ranked ``c c c`` Passages fail the literal check, so pages stay full.
    first = await recall.search_page(request("C#", limit=2))
    second = await recall.search_page(request("C#", limit=2, offset=2))

    assert (len(first.hits), first.has_more) == (2, True)
    assert (len(second.hits), second.has_more) == (1, False)
    assert {hit.session_id for hit in first.hits + second.hits} == {
        f"csharp-{number}" for number in range(3)
    }


def _stored_passages(path: Path) -> dict[str, int]:
    with closing(connect_store(path)) as connection:
        for table in ("passages_fts", "passages_fts_tokens"):
            # rank=1 compares every indexed row with its external content.
            connection.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
        return {
            str(passage_id): int(passage_ref)
            for passage_ref, passage_id in connection.execute(
                "SELECT passage_ref, passage_id FROM passages"
            )
        }


async def test_search_refresh_rewrites_only_changed_passages(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    session = sessions.create("coder", session_id="growing")
    session.append_many(
        [
            ChatMessage.user(f"needle part {number} " + "x" * 1000, timestamp=timestamp(number + 1))
            for number in range(4)
        ]
    )
    recall = literal_backend(tmp_path, sessions)
    await recall.search_page(request("needle"))
    before = _stored_passages(recall.index.path)

    session.append(ChatMessage.user("needle tail " + "y" * 1000, timestamp=timestamp(9)))
    page = await recall.search_page(request("tail"))
    after = _stored_passages(recall.index.path)

    kept = before.keys() & after.keys()
    assert kept
    assert all(before[passage_id] == after[passage_id] for passage_id in kept)
    assert after.keys() - before.keys()
    assert page.hits and {hit.session_id for hit in page.hits} == {"growing"}
    with closing(sqlite3.connect(recall.index.path)) as connection:
        stamp = connection.execute(
            "SELECT generation_id, history_revision FROM indexed_sessions"
        ).fetchall()
    version = sessions.list_history_versions([session.address])[session.address]
    assert stamp == [(version[0], version[1])]


async def test_literal_search_reports_shared_fork_history_once(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    source = sessions.create("coder", session_id="source")
    for day in range(1, 4):
        source.append(ChatMessage.user(f"needle story {day} " * 120, timestamp=timestamp(day)))
    older = await sessions.fork(source.address)
    newer = await sessions.fork(source.address)
    recall = literal_backend(tmp_path, sessions)

    complete = await recall.search_page(request("needle", limit=20))
    without_origin = await recall.search_page(
        request("needle", limit=20, excluded_session_ids=("source",))
    )

    assert complete.hits
    assert {hit.session_id for hit in complete.hits} == {"source"}
    assert {hit.session_id for hit in without_origin.hits} == {newer.id}
    assert [hit.passage_id for hit in without_origin.hits] == [
        hit.passage_id for hit in complete.hits
    ]

    # Deleting the origin copies its history into both forks and bumps their
    # history revision: the forks are reindexed and report it once.
    await asyncio.to_thread(sessions.delete, source.address)
    after_delete = await recall.search_page(request("needle", limit=20))

    assert {hit.session_id for hit in after_delete.hits} == {newer.id}
    assert sorted(str(hit.passage_id) for hit in after_delete.hits) == sorted(
        str(hit.passage_id) for hit in complete.hits
    )
    with closing(sqlite3.connect(recall.index.path)) as connection:
        stamps = {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT session_id, generation_id, history_revision FROM indexed_sessions"
            )
        }
    revisions = await asyncio.to_thread(sessions.list_history_revisions, "coder")
    assert stamps == {
        revision.address.session_id: (revision.generation_id, revision.history_revision)
        for revision in revisions
    }
    assert set(stamps) == {older.id, newer.id}


async def test_literal_search_attributes_own_fork_content_to_the_fork(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    source = sessions.create("coder", session_id="source")
    source.append(ChatMessage.user("shared needle " * 150, timestamp=timestamp(1)))
    fork = await sessions.fork(source.address)
    await asyncio.to_thread(
        fork.append, ChatMessage.user("fork needle only " * 150, timestamp=timestamp(2))
    )
    recall = literal_backend(tmp_path, sessions)

    page = await recall.search_page(request("needle", limit=20))

    by_session = {hit.session_id for hit in page.hits}
    assert by_session == {"source", fork.id}
    assert len({hit.passage_id for hit in page.hits}) == len(page.hits)
    assert all("fork needle only" in hit.text for hit in page.hits if hit.session_id == fork.id)
    assert all(
        "fork needle only" not in hit.text for hit in page.hits if hit.session_id == "source"
    )


@pytest.mark.parametrize("persistent", [False, True], ids=["once", "persistent"])
async def test_index_damaged_during_a_search_is_rebuilt_once(
    tmp_path: Path,
    sessions: ChatSessionManager,
    monkeypatch: pytest.MonkeyPatch,
    persistent: bool,
) -> None:
    sessions.create("coder", session_id="damaged").append(
        ChatMessage.user("needle", timestamp=timestamp(1))
    )
    recall = literal_backend(tmp_path, sessions)
    await recall.search_page(request("needle"))
    old_identity = _identity(recall.index.path)
    refresh = PassageCatalog.refresh
    attempts = 0

    async def damaged(self: PassageCatalog, *args: Any) -> None:
        nonlocal attempts
        attempts += 1
        if persistent or attempts == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        await refresh(self, *args)

    monkeypatch.setattr(PassageCatalog, "refresh", damaged)

    if persistent:
        with pytest.raises(RecallSearchError) as error:
            await recall.search_page(request("needle"))
        assert error.value.code == "hybrid_unavailable"
    else:
        page = await recall.search_page(request("needle"))
        assert [hit.session_id for hit in page.hits] == ["damaged"]
        assert _identity(recall.index.path)["database_id"] != old_identity["database_id"]
    assert attempts == 2


async def test_busy_index_fails_the_search_without_discarding_it(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neither the SQLite busy timeout nor the write patience is waited out in full.
    monkeypatch.setattr("core.database._runtime.BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr("core.recall._passage_catalog.WRITE_PATIENCE_S", 0.05)
    session = sessions.create("coder", session_id="busy")
    session.append(ChatMessage.user("needle one", timestamp=timestamp(1)))
    recall = literal_backend(tmp_path, sessions)
    await recall.search_page(request("needle"))
    identity = _identity(recall.index.path)
    await asyncio.to_thread(session.append, ChatMessage.user("needle two", timestamp=timestamp(2)))

    with closing(sqlite3.connect(recall.index.path, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(RecallSearchError):
            await recall.search_page(request("needle"))
        blocker.execute("ROLLBACK")

    assert _identity(recall.index.path)["database_id"] == identity["database_id"]
    page = await recall.search_page(request("needle"))
    assert len(page.hits) == 1 and "needle two" in page.hits[0].text
