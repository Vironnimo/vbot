"""The semantic indexer: background document embedding, failure isolation and status."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.model_tasks import EmbeddingExecutionError, EmbeddingResult, EmbeddingUsage
from core.models.pricing import TokenPricing, TokenRates
from core.providers.errors import ProviderAuthError
from core.recall import IndexStatus, PassageIndex, SemanticIndexer
from core.runs import RunKind
from core.sessions import ChatSessionManager, SessionAddress
from core.utils.errors import ProviderError
from core.utils.timestamps import format_canonical_timestamp
from tests.core.recall.recall_test_support import StubEmbeddings, timestamp
from tests.core.sessions.history_fixtures import admit_run

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 5, 10, 12, tzinfo=UTC)
PRICE_PER_MILLION = 2.0


def _at(seconds: float) -> str:
    return format_canonical_timestamp(NOW + timedelta(seconds=seconds))


class _Embeddings(StubEmbeddings):
    """Stub embeddings that report usage, reject chosen calls and track overlap."""

    def __init__(self, reject: Callable[[list[str]], BaseException | None] | None = None) -> None:
        super().__init__()
        self.reject = reject
        self.in_flight = 0
        self.max_in_flight = 0

    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            failure = self.reject(texts) if self.reject is not None else None
            if failure is not None:
                self.embed_calls.append(list(texts))
                self.embed_purposes.append(purpose)
                raise failure
            result = await super().embed(texts, purpose=purpose)
        finally:
            self.in_flight -= 1
        characters = sum(len(text) for text in texts)
        return dataclasses.replace(
            result,
            usage=EmbeddingUsage(
                requests=1, token_reports=1, input_tokens=characters, total_tokens=characters
            ),
        )


def _provider_failure(
    status: int, *, retryable: bool = False, retry_after: float | None = None
) -> EmbeddingExecutionError:
    cause = ProviderError(f"HTTP {status}", retryable=retryable)
    cause.status_code = status
    cause.retry_after = retry_after
    error = EmbeddingExecutionError(f"embedding request failed: HTTP {status}")
    error.__cause__ = cause
    return error


def _overflow() -> EmbeddingExecutionError:
    return EmbeddingExecutionError(
        "HTTP 400: This model's maximum context length is 8192 tokens. (parameter=input_tokens)"
    )


def _pricing(reference: str) -> TokenPricing | None:
    if reference != "openrouter/stub-embed":
        return None
    return TokenPricing(source="test", rates=TokenRates(input=PRICE_PER_MILLION))


@pytest.fixture
def index(tmp_path: Path) -> Iterator[PassageIndex]:
    passage_index = PassageIndex(tmp_path)
    yield passage_index
    passage_index.close()


def _indexer(
    index: PassageIndex,
    sessions: ChatSessionManager,
    embeddings: Any,
    *,
    enabled: bool = True,
    configured: bool = True,
    **options: Any,
) -> SemanticIndexer:
    indexer = SemanticIndexer(
        index=index,
        sessions=sessions,
        embeddings=embeddings,
        binding_configured=lambda: configured,
        pricing=_pricing,
        clock=lambda: NOW,
        monotonic=lambda: 1000.0,
        **options,
    )
    indexer.set_enabled(enabled)
    return indexer


def _conversation(sessions: ChatSessionManager, session_id: str, text: str, day: int) -> None:
    sessions.create("coder", session_id=session_id).append(
        ChatMessage.user(text, timestamp=timestamp(day))
    )


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------


async def test_pass_embeds_waiting_texts_newest_first_one_call_at_a_time(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    for day in (1, 2, 3):
        _conversation(sessions, f"day-{day}", f"banana fruit {day}", day)
    embeddings = _Embeddings()
    indexer = _indexer(index, sessions, embeddings, batch_size=2)

    await indexer.run_pass()
    after = await indexer.status()

    assert embeddings.embed_calls == [["banana fruit 3", "banana fruit 2"], ["banana fruit 1"]]
    assert set(embeddings.embed_purposes) == {"document"}
    assert embeddings.max_in_flight == 1
    assert after.spent_cost == pytest.approx(42 * PRICE_PER_MILLION / 1_000_000)
    assert dataclasses.replace(after, spent_cost=None) == IndexStatus(
        semantic_enabled=True,
        state="idle",
        provider="openrouter",
        model="stub-embed",
        indexed=3,
        waiting=0,
        skipped=0,
        last_error=None,
        # The next pass is the periodic sweep.
        next_attempt_at=_at(600),
        last_completed_at=_at(0),
        spent_requests=2,
        spent_input_tokens=42,
        spent_total_tokens=42,
        spent_cost=None,
        estimate_characters=0,
        estimate_tokens=0,
        estimate_cost=0.0,
    )


async def test_status_estimates_waiting_texts_and_keeps_spent_usage_per_space(
    index: PassageIndex, sessions: ChatSessionManager, tmp_path: Path
) -> None:
    _conversation(sessions, "one", "banana fruit 1", 1)
    embeddings = _Embeddings()
    indexer = _indexer(index, sessions, embeddings)
    await indexer.run_pass()
    _conversation(sessions, "two", "banana fruit 22", 2)
    # A pass that fails at the provider still brings the new Session into the catalog.
    embeddings.reject = lambda _texts: _provider_failure(503, retryable=True)
    await indexer.run_pass()
    embeddings.reject = None

    waiting = await indexer.status()

    assert (waiting.waiting, waiting.estimate_characters, waiting.estimate_tokens) == (1, 15, 4)
    assert waiting.estimate_cost == pytest.approx(4 * PRICE_PER_MILLION / 1_000_000)
    # Spent usage is persisted with the index.
    reopened = PassageIndex(tmp_path)
    try:
        assert (await _indexer(reopened, sessions, embeddings).status()).spent_input_tokens == 14
    finally:
        reopened.close()

    # Another embedding model starts a new space: every text waits again, spent restarts.
    embeddings.model_id = "other-embed"
    await indexer.run_pass()
    moved = await indexer.status()

    assert (moved.model, moved.indexed, moved.waiting) == ("other-embed", 2, 0)
    assert moved.spent_input_tokens == 29
    assert moved.spent_cost is None


async def test_pass_indexes_recall_visible_sessions_and_evicts_scopes_that_left(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    _conversation(sessions, "conversation", "banana fruit", 1)
    for session_id, kinds in (
        ("delegated", [RunKind.SUBAGENT]),
        ("reflection", [RunKind.USER, RunKind.SKILL_REFLECTION]),
    ):
        session = sessions.create("coder", session_id=session_id)
        session.append(ChatMessage.user(f"{session_id} fruit", timestamp=timestamp(2)))
        for kind in kinds:
            await admit_run(sessions, session.address, kind)
    writer = sessions.create("writer", session_id="draft")
    writer.append(ChatMessage.user("draft fruit", timestamp=timestamp(3)))
    indexer = _indexer(index, sessions, _Embeddings())

    await indexer.run_pass()

    assert await index.indexed_scopes() == {(None, "coder"), (None, "writer")}
    assert set(await index.list_indexed_sessions("coder")) == {"conversation", "delegated"}

    await asyncio.to_thread(
        sessions.delete, SessionAddress(project_id=None, agent_id="writer", session_id="draft")
    )
    await indexer.run_pass()

    assert await index.indexed_scopes() == {(None, "coder")}
    assert (await indexer.status()).indexed == 2


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_rejected_text_is_isolated_by_bisection_and_skipped_until_rebuild(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    for day, text in enumerate(("fruit a", "poison text", "fruit c", "fruit d"), start=1):
        _conversation(sessions, f"day-{day}", text, day)
    embeddings = _Embeddings(
        reject=lambda texts: _overflow() if any("poison" in text for text in texts) else None
    )
    indexer = _indexer(index, sessions, embeddings, batch_size=4)

    await indexer.run_pass()
    await indexer.run_pass()
    status = await indexer.status()

    # Accepted halves are stored; the one text the provider rejects is skipped
    # and never sent again in this embedding space.
    assert embeddings.embed_calls == [
        ["fruit d", "fruit c", "poison text", "fruit a"],
        ["fruit d", "fruit c"],
        ["poison text", "fruit a"],
        ["poison text"],
        ["fruit a"],
    ]
    assert (status.state, status.indexed, status.waiting, status.skipped) == ("idle", 3, 0, 1)
    assert status.last_error is None

    await indexer.rebuild()
    rebuilt = await indexer.status()

    assert (rebuilt.indexed, rebuilt.waiting, rebuilt.skipped) == (0, 4, 0)
    assert rebuilt.next_attempt_at == _at(0)
    calls = len(embeddings.embed_calls)
    await indexer.run_pass()
    assert ["poison text"] in embeddings.embed_calls[calls:]


async def test_batch_whose_every_text_fails_alone_stops_without_skipping(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    _conversation(sessions, "one", "fruit a", 1)
    _conversation(sessions, "two", "fruit b", 2)
    embeddings = _Embeddings(reject=lambda _texts: _provider_failure(400))
    indexer = _indexer(index, sessions, embeddings)

    await indexer.run_pass()
    status = await indexer.status()

    assert len(embeddings.embed_calls) == 3
    assert (status.state, status.waiting, status.skipped) == ("error", 2, 0)
    assert status.last_error is not None
    assert status.last_error.code == "provider_rejected"
    assert status.next_attempt_at == _at(30)


async def test_transient_failures_back_off_and_honor_retry_after(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    _conversation(sessions, "one", "fruit a", 1)
    failures = [
        _provider_failure(503, retryable=True),
        _provider_failure(503, retryable=True),
        _provider_failure(429, retryable=True, retry_after=300.0),
    ]
    embeddings = _Embeddings(reject=lambda _texts: failures.pop(0) if failures else None)
    indexer = _indexer(index, sessions, embeddings)
    observed: list[tuple[str, str | None, str | None]] = []

    for _attempt in range(4):
        await indexer.run_pass()
        status = await indexer.status()
        code = status.last_error.code if status.last_error is not None else None
        observed.append((status.state, code, status.next_attempt_at))
        if status.state == "retrying":
            # A Run that ends during the backoff does not bring the attempt forward.
            indexer.run_finished()
            assert (await indexer.status()).next_attempt_at == status.next_attempt_at

    assert observed == [
        ("retrying", "provider_unavailable", _at(30)),
        ("retrying", "provider_unavailable", _at(60)),
        ("retrying", "provider_rate_limited", _at(300)),
        ("idle", None, _at(600)),
    ]
    assert (await indexer.status()).indexed == 1


async def test_rejected_credentials_stop_indexing_until_the_settings_change(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    _conversation(sessions, "one", "fruit a", 1)

    def reject(_texts: list[str]) -> BaseException:
        error = EmbeddingExecutionError("embedding request failed: HTTP 401")
        error.__cause__ = ProviderAuthError("invalid API key")
        return error

    indexer = _indexer(index, sessions, _Embeddings(reject=reject))

    await indexer.run_pass()
    failed = await indexer.status()
    indexer.settings_changed()
    retrying = await indexer.status()

    assert failed.state == "error"
    assert failed.last_error is not None
    assert (failed.last_error.code, failed.last_error.message) == (
        "provider_auth",
        "The embedding provider rejected the credentials.",
    )
    assert failed.next_attempt_at == _at(30)
    assert retrying.next_attempt_at == _at(0)


async def test_served_model_that_keeps_changing_stops_the_pass(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    for day in range(1, 7):
        _conversation(sessions, f"day-{day}", f"fruit {day}", day)

    class _Drifting(_Embeddings):
        async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
            self.response_model_id = f"served/embed-{len(self.embed_calls)}"
            return await super().embed(texts, purpose=purpose)

    embeddings = _Drifting()
    indexer = _indexer(index, sessions, embeddings, batch_size=1)

    await indexer.run_pass()
    status = await indexer.status()

    # The first call pins the space; each later one moves it and requeues everything.
    assert len(embeddings.embed_calls) == 5
    assert status.state == "error"
    assert status.last_error is not None and status.last_error.code == "space_unstable"


# ---------------------------------------------------------------------------
# Enablement and lifecycle
# ---------------------------------------------------------------------------


async def test_indexer_without_a_binding_or_a_semantic_backend_embeds_nothing(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    _conversation(sessions, "one", "fruit a", 1)
    embeddings = _Embeddings()
    unconfigured = _indexer(index, sessions, embeddings, configured=False)
    disabled = _indexer(index, sessions, embeddings, enabled=False)

    await unconfigured.run_pass()
    await disabled.run_pass()
    first = await unconfigured.status()
    second = await disabled.status()

    assert embeddings.embed_calls == []
    assert (first.semantic_enabled, first.state, first.provider) == (True, "unconfigured", None)
    assert (second.semantic_enabled, second.state, second.next_attempt_at) == (
        False,
        "disabled",
        None,
    )
    # Neither touched the index, which the first semantic search or pass creates.
    assert not index.path.exists()


async def test_started_indexer_runs_on_its_triggers_and_stops_cleanly(
    index: PassageIndex, sessions: ChatSessionManager
) -> None:
    _conversation(sessions, "one", "fruit a", 1)
    indexer = SemanticIndexer(
        index=index,
        sessions=sessions,
        embeddings=_Embeddings(),
        binding_configured=lambda: True,
        start_delay_s=0.0,
        run_debounce_s=0.0,
    )
    states: list[str] = []
    indexed = asyncio.Event()
    target = 1

    def listener(status: IndexStatus) -> None:
        states.append(status.state)
        if status.state == "idle" and status.indexed == target:
            indexed.set()

    indexer.add_listener(listener)
    indexer.set_enabled(True)
    indexer.start()
    await asyncio.wait_for(indexed.wait(), timeout=5)

    # A Run that ended added a conversation; its trigger indexes it.
    indexed.clear()
    target = 2
    await asyncio.to_thread(_conversation, sessions, "two", "fruit b", 2)
    indexer.run_finished()
    await asyncio.wait_for(indexed.wait(), timeout=5)
    await indexer.aclose()

    assert "indexing" in states
    assert not [
        task for task in asyncio.all_tasks() if task.get_name() == "recall-semantic-indexer"
    ]
