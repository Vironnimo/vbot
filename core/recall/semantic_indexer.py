"""Background document embedding for semantic Recall.

:class:`SemanticIndexer` is the single owner of document embedding. One
instance per Runtime fills the vectors of the shared Passage index
(:mod:`core.recall.passage_index`) while the selected Recall backend ranks by
meaning and an embedding model is configured. Searches only embed their query.

A pass first refreshes the Passage catalog for every scope with live Sessions,
over the Sessions Recall may return (conversation and Sub-Agent Sessions,
never hidden ones), and evicts scopes that have none left. It then drains the
waiting texts newest first, one provider call at a time.

Passes run on a schedule: shortly after start, after a Run ends (coalesced),
periodically as a sweep that also catches edits, imports and Channel traffic,
right after a Settings change or rebuild, and when a search finds Passages that
still wait. Failures are isolated:

- a transient provider failure (network, timeout, 429, 5xx) backs off from
  30 seconds, doubling up to 30 minutes, and honors ``Retry-After``;
- a batch the provider rejects permanently (another 4xx, context overflow) is
  bisected: accepted halves are stored, and a single text that still fails is
  recorded as skipped until the embedding space changes or the index is reset;
- a configuration failure (unusable binding, rejected credentials, unknown
  model) puts the indexer into ``error`` until a Settings change, a rebuild or
  the next attempt after the same backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from core.model_tasks import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingSpaceIdentity,
    EmbeddingUnsupportedTargetError,
    EmbeddingUsage,
)
from core.models.pricing import TokenPricing
from core.providers.errors import ProviderAuthError
from core.recall.canonical import RecallScope
from core.recall.passage_index import (
    IndexCounts,
    IndexSpent,
    PassageIndex,
    VectorHeader,
    space_change_reason,
)
from core.sessions import ChatSessionManager, recall_visibilities
from core.utils.log_conditions import LoggedConditions
from core.utils.timestamps import format_canonical_timestamp

IndexState = Literal["disabled", "unconfigured", "idle", "indexing", "retrying", "error"]

START_DELAY_S = 5.0
RUN_DEBOUNCE_S = 20.0
SWEEP_INTERVAL_S = 600.0
BACKOFF_INITIAL_S = 30.0
BACKOFF_MAX_S = 1800.0
BATCH_SIZE = 64
# Pushed status updates while indexing are at least this far apart.
STATUS_INTERVAL_S = 2.0
# The estimate assumes this many characters per token.
CHARS_PER_TOKEN = 4
# Space changes one pass follows before it gives up as unstable.
_MAX_SPACE_MOVES = 3
_TOKENS_PER_PRICE_UNIT = 1_000_000

_FailureKind = Literal["transient", "permanent", "configuration"]

# Stable failure codes with their short English explanation.
_MESSAGES = {
    "provider_unavailable": "The embedding provider is unreachable or failing temporarily.",
    "provider_rate_limited": "The embedding provider is limiting the request rate.",
    "provider_auth": "The embedding provider rejected the credentials.",
    "embedding_unusable": "The configured embedding model cannot be used.",
    "embedding_model_unavailable": "The embedding provider does not offer the configured model.",
    "embedding_failed": "The embedding provider returned an unusable response.",
    "provider_rejected": "The embedding provider rejected the texts.",
    "context_overflow": "The embedding model rejected texts longer than its input limit.",
    "index_unavailable": "The Passage index could not be read or written.",
    "space_unstable": "The embedding space kept changing during indexing.",
}


@dataclass(frozen=True)
class IndexFailure:
    """The last indexing failure: a stable code and a short English message."""

    code: str
    message: str

    @classmethod
    def of(cls, code: str) -> IndexFailure:
        return cls(code, _MESSAGES[code])


@dataclass(frozen=True)
class IndexStatus:
    """Observable state of semantic indexing.

    Counts are distinct stored Passages of the Sessions Recall may return.
    ``spent`` is the document embedding usage since the current space was
    pinned; ``estimate_*`` describe the texts still waiting. Costs are USD and
    ``None`` when unknown.
    """

    semantic_enabled: bool
    state: IndexState
    provider: str | None = None
    model: str | None = None
    indexed: int = 0
    waiting: int = 0
    skipped: int = 0
    last_error: IndexFailure | None = None
    next_attempt_at: str | None = None
    last_completed_at: str | None = None
    spent_requests: int = 0
    spent_input_tokens: int = 0
    spent_total_tokens: int = 0
    spent_cost: float | None = 0.0
    estimate_characters: int = 0
    estimate_tokens: int = 0
    estimate_cost: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_enabled": self.semantic_enabled,
            "state": self.state,
            "provider": self.provider,
            "model": self.model,
            "indexed": self.indexed,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "last_error": (
                None
                if self.last_error is None
                else {"code": self.last_error.code, "message": self.last_error.message}
            ),
            "next_attempt_at": self.next_attempt_at,
            "last_completed_at": self.last_completed_at,
            "spent": {
                "requests": self.spent_requests,
                "input_tokens": self.spent_input_tokens,
                "total_tokens": self.spent_total_tokens,
                "cost": self.spent_cost,
            },
            "estimate": {
                "characters": self.estimate_characters,
                "tokens": self.estimate_tokens,
                "cost": self.estimate_cost,
            },
        }


@dataclass(frozen=True)
class _Classified:
    kind: _FailureKind
    failure: IndexFailure
    retry_after: float | None = None


class _HaltError(Exception):
    """Stop the pass; the classified failure decides the backoff and state."""

    def __init__(self, classified: _Classified) -> None:
        super().__init__(classified.failure.code)
        self.classified = classified


class _RestartError(Exception):
    """The binding changed during the pass; run a new pass at once."""


class _SpaceMovedError(Exception):
    """The index left the space the pass embeds for; re-read it and continue."""


@dataclass
class _BatchOutcome:
    embedded: int = 0
    succeeded: bool = False
    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    rejected: dict[str, str] = field(default_factory=dict)
    first_rejection: _Classified | None = None


@dataclass
class _PassTotals:
    embedded: int = 0
    skipped: int = 0
    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    # Pinned spaces this pass left: a re-pin or a space another writer pinned.
    space_moves: int = 0

    def moved_space(self) -> None:
        self.space_moves += 1
        if self.space_moves > _MAX_SPACE_MOVES:
            raise _HaltError(_Classified("configuration", IndexFailure.of("space_unstable")))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SemanticIndexer:
    """Fill the Passage index's vectors in the background, one provider call at a time.

    The clock, monotonic time and delays are injectable so tests run without
    real waits. ``binding_configured`` reports whether a ``text_embedding``
    binding is set at all; ``pricing`` returns the Model DB price of a
    ``provider/model`` reference.
    """

    def __init__(
        self,
        *,
        index: PassageIndex,
        sessions: ChatSessionManager,
        embeddings: Any | None,
        binding_configured: Callable[[], bool],
        pricing: Callable[[str], TokenPricing | None] | None = None,
        logger: Any | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        start_delay_s: float = START_DELAY_S,
        run_debounce_s: float = RUN_DEBOUNCE_S,
        sweep_interval_s: float = SWEEP_INTERVAL_S,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._index = index
        self._sessions = sessions
        self._embeddings = embeddings
        self._binding_configured = binding_configured
        self._pricing = pricing
        self.logger = logger
        self._clock = clock
        self._monotonic = monotonic
        self._start_delay_s = start_delay_s
        self._run_debounce_s = run_debounce_s
        self._sweep_interval_s = sweep_interval_s
        self._batch_size = batch_size
        self._enabled = False
        self._state: IndexState = "idle"
        self._last_error: IndexFailure | None = None
        self._failures = 0
        self._due: float | None = None
        self._backoff_until: float | None = None
        self._completed_once = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[IndexStatus], None]] = []
        self._published: tuple[bool, IndexState] | None = None
        self._last_publish = -math.inf
        self._conditions = LoggedConditions(limit=8)

    # -- Control -------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        """Run passes on the running Event Loop until :meth:`aclose`."""
        if self._task is not None or self._closed:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._run(), name="recall-semantic-indexer")
        # Triggers before the start do not skip the start delay.
        self._due = None
        self._schedule(self._start_delay_s, force=True)

    def set_enabled(self, enabled: bool) -> None:
        """Follow the selected backend: only a backend ranking by meaning needs vectors."""
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            self._schedule(0.0, force=True)
        else:
            self._due = None
            self._signal()

    def settings_changed(self) -> None:
        """The embedding binding changed: retry at once, without the backoff."""
        self._schedule(0.0, force=True)

    def run_finished(self) -> None:
        """A Run ended; its Messages are indexed within the debounce delay."""
        self._schedule(self._run_debounce_s)

    def waiting_found(self) -> None:
        """A search found Passages without a vector."""
        self._schedule(0.0)

    async def rebuild(self) -> None:
        """Drop every vector and skipped text and index everything again."""
        if self._index.path.exists():
            await self._index.reset_vectors()
            self._log_info("Reset Recall Passage vectors")
        self._schedule(0.0, force=True)
        await self._publish(force=True)

    def add_listener(self, listener: Callable[[IndexStatus], None]) -> Callable[[], None]:
        """Call *listener* with each published status; returns the unsubscribe call.

        Status is published on every state change and at most every
        ``STATUS_INTERVAL_S`` seconds while indexing.
        """
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def aclose(self) -> None:
        """Stop the pass in progress and the schedule."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        self._listeners.clear()

    def close(self) -> None:
        """Synchronous :meth:`aclose`: the task is cancelled, not awaited."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done() and not task.get_loop().is_closed():
            task.get_loop().call_soon_threadsafe(task.cancel)
        self._listeners.clear()

    async def run_pass(self) -> None:
        """Run one pass now, outside the schedule (scripts and tests)."""
        await self._run_one_pass()

    # -- Status --------------------------------------------------------------------

    async def status(self) -> IndexStatus:
        """The current state, coverage, spent usage and estimate."""
        configured, identity, _error = await asyncio.to_thread(self._resolve)
        counts, spent = IndexCounts(), IndexSpent()
        # Without its file the index holds nothing; status never creates it.
        if self._index.path.exists():
            try:
                counts = await self._index.counts()
                spent = await self._index.spent()
            except Exception as error:
                self._debug("Recall index status unavailable (error=%s)", type(error).__name__)
        price = self._input_price(identity)
        tokens = math.ceil(counts.waiting_characters / CHARS_PER_TOKEN)
        return IndexStatus(
            semantic_enabled=self._enabled,
            state=self._visible_state(configured),
            provider=identity.provider_id if identity is not None else None,
            model=identity.model_id if identity is not None else None,
            indexed=counts.indexed,
            waiting=counts.waiting,
            skipped=counts.skipped,
            last_error=self._last_error if self._enabled else None,
            next_attempt_at=self._next_attempt_at(),
            last_completed_at=spent.last_completed_at,
            spent_requests=spent.usage.requests,
            spent_input_tokens=spent.usage.input_tokens,
            spent_total_tokens=spent.usage.total_tokens,
            spent_cost=_spent_cost(spent.usage, price),
            estimate_characters=counts.waiting_characters,
            estimate_tokens=tokens,
            estimate_cost=None if price is None else tokens * price / _TOKENS_PER_PRICE_UNIT,
        )

    def _visible_state(self, configured: bool) -> IndexState:
        if not self._enabled:
            return "disabled"
        if not configured:
            return "unconfigured"
        if self._state in ("disabled", "unconfigured"):
            return "idle"
        return self._state

    def _next_attempt_at(self) -> str | None:
        if self._due is None or not self._enabled:
            return None
        delay = max(0.0, self._due - self._monotonic())
        return format_canonical_timestamp(self._clock() + timedelta(seconds=delay))

    def _input_price(self, identity: EmbeddingSpaceIdentity | None) -> float | None:
        if identity is None or self._pricing is None:
            return None
        pricing = self._pricing(f"{identity.provider_id}/{identity.model_id}")
        if pricing is None or not pricing.supported:
            return None
        return pricing.rates.input

    # -- Scheduling ----------------------------------------------------------------

    def _schedule(self, delay_s: float, *, force: bool = False) -> None:
        """Bring the next pass forward to *delay_s* from now.

        Without ``force`` a pending backoff still holds; ``force`` clears it.
        """
        if not self._enabled or self._closed:
            return
        due = self._monotonic() + max(0.0, delay_s)
        if force:
            self._backoff_until = None
            self._failures = 0
        elif self._backoff_until is not None:
            due = max(due, self._backoff_until)
        if self._due is None or due < self._due:
            self._due = due
        self._signal()

    def _signal(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake.set()
        else:
            loop.call_soon_threadsafe(wake.set)

    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        while not self._closed:
            wake.clear()
            await self._publish_state_change()
            delay = None if self._due is None else self._due - self._monotonic()
            if delay is None or delay > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                continue
            await self._run_one_pass()

    async def _run_one_pass(self) -> None:
        self._due = None
        if not self._enabled:
            return
        try:
            await self._pass()
        except _HaltError as halt:
            self._fail(halt.classified)
        except _RestartError:
            self._schedule(0.0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._index.discard_if_damaged(error)
            self._warn_index_failure(error)
            self._fail(_Classified("transient", IndexFailure.of("index_unavailable")))
        else:
            self._recovered()
        if self._due is None and self._enabled:
            self._due = self._monotonic() + self._sweep_interval_s
        await self._publish(force=True)

    def _fail(self, classified: _Classified) -> None:
        self._failures += 1
        backoff = min(BACKOFF_INITIAL_S * 2 ** (self._failures - 1), BACKOFF_MAX_S)
        if classified.retry_after is not None:
            backoff = max(backoff, classified.retry_after)
        self._state = "retrying" if classified.kind == "transient" else "error"
        self._last_error = classified.failure
        self._due = self._backoff_until = self._monotonic() + backoff
        if self._conditions.started("indexing", classified.failure.code):
            self._log_warning(
                "Paused Recall indexing (code=%s state=%s retry_in_s=%d)",
                classified.failure.code,
                self._state,
                round(backoff),
            )

    def _recovered(self) -> None:
        self._last_error = None
        if self._failures:
            self._failures = 0
            self._backoff_until = None
        if self._conditions.ended("indexing"):
            self._log_info("Resumed Recall indexing")

    # -- One pass --------------------------------------------------------------------

    def _resolve(self) -> tuple[bool, EmbeddingSpaceIdentity | None, EmbeddingError | None]:
        """Whether a binding is set, its space, or why it cannot be used. Blocking."""
        if self._embeddings is None or not self._binding_configured():
            return False, None, None
        try:
            return True, self._embeddings.resolve_space(), None
        except EmbeddingError as error:
            return True, None, error

    async def _pass(self) -> None:
        configured, identity, error = await asyncio.to_thread(self._resolve)
        if not configured:
            self._state = "unconfigured"
            self._last_error = None
            return
        if identity is None:
            raise _HaltError(_classify(error or EmbeddingConfigurationError("unusable binding")))
        binding = VectorHeader.for_space(identity)
        await self._refresh_catalog()
        await self._drain(binding)

    async def _refresh_catalog(self) -> None:
        """Bring every live scope's Recall-visible Sessions into the catalog."""
        sessions = self._sessions
        live = await sessions.run_async(sessions.list_live_scopes)
        for project_id, agent_id in live:
            if not self._enabled or self._closed:
                return
            scope = await sessions.run_async(_index_scope, sessions, agent_id, project_id)
            await self._index.recovering(
                functools.partial(self._index.refresh, sessions, agent_id, project_id, scope),
                warning=self._warn_index_failure,
            )
        for project_id, agent_id in await self._index.indexed_scopes() - set(live):
            await self._index.remove_scope(agent_id, project_id)

    async def _drain(self, binding: VectorHeader) -> None:
        totals = _PassTotals()
        started_at = self._monotonic()
        started = False
        header = await self._pinned(binding)
        while self._enabled and not self._closed:
            batch = await self._index.pending_texts(limit=self._batch_size)
            if not batch:
                break
            if not started:
                started = True
                self._state = "indexing"
                counts = await self._index.counts()
                self._log_info(
                    "Started Recall indexing (waiting=%d provider=%s model=%s)",
                    counts.waiting,
                    binding.provider_id,
                    binding.model_id,
                )
                await self._publish(force=True)
            outcome = _BatchOutcome()
            try:
                header = await self._embed(binding, header, batch, outcome, totals)
            except _SpaceMovedError:
                totals.moved_space()
                header = await self._pinned(binding)
                continue
            finally:
                totals.embedded += outcome.embedded
                totals.usage = totals.usage.combined(outcome.usage)
            if outcome.rejected:
                if not outcome.succeeded and len(batch) > 1 and outcome.first_rejection:
                    # Every text failed alone: the provider rejects this request
                    # shape, not individual texts.
                    rejection = outcome.first_rejection
                    raise _HaltError(_Classified("configuration", rejection.failure))
                await self._index.record_skipped(header, outcome.rejected, at=self._clock())
                totals.skipped += len(outcome.rejected)
            self._recovered()
            self._debug(
                "Embedded Recall Passage batch (texts=%d embedded=%d skipped=%d requests=%d)",
                len(batch),
                outcome.embedded,
                len(outcome.rejected),
                outcome.usage.requests,
            )
            await self._publish()
        self._state = "idle"
        if not self._enabled or self._closed:
            return
        if started or not self._completed_once:
            self._completed_once = True
            await self._index.mark_completed(self._clock())
        if started:
            self._log_info(
                "Indexed Recall Passages (embedded=%d skipped=%d duration_ms=%d requests=%d "
                "input_tokens=%d cost=%.6g)",
                totals.embedded,
                totals.skipped,
                round((self._monotonic() - started_at) * 1000),
                totals.usage.requests,
                totals.usage.input_tokens,
                totals.usage.cost,
            )

    async def _pinned(self, binding: VectorHeader) -> VectorHeader | None:
        """The pinned header when it belongs to the binding's space."""
        stored = await self._index.read_header()
        return stored if stored is not None and stored.same_space(binding) else None

    async def _embed(
        self,
        binding: VectorHeader,
        header: VectorHeader | None,
        items: list[tuple[str, str]],
        outcome: _BatchOutcome,
        totals: _PassTotals,
    ) -> VectorHeader | None:
        """Embed and store *items*, bisecting a batch the provider rejects.

        A served model that differs from the pinned one re-pins the space, which
        queues every Passage again; a pass gives up after a few such moves.
        """
        embeddings = self._embeddings
        if embeddings is None:
            raise _HaltError(_Classified("configuration", IndexFailure.of("embedding_unusable")))
        try:
            result = await embeddings.embed(
                [text for _text_hash, text in items], purpose="document"
            )
        except EmbeddingError as error:
            classified = _classify(error)
            if classified.kind != "permanent":
                raise _HaltError(classified) from error
            if len(items) == 1:
                outcome.rejected[items[0][0]] = classified.failure.code
                if outcome.first_rejection is None:
                    outcome.first_rejection = classified
                return header
            middle = len(items) // 2
            header = await self._embed(binding, header, items[:middle], outcome, totals)
            return await self._embed(binding, header, items[middle:], outcome, totals)
        outcome.usage = outcome.usage.combined(result.usage)
        resolved = VectorHeader.from_result(result)
        if not resolved.same_space(binding):
            # The binding changed while the call ran.
            raise _RestartError()
        if header != resolved:
            previous = await self._index.read_header()
            if await self._index.use_space(resolved):
                if previous is not None:
                    totals.moved_space()
                self._log_info(
                    "Pinned Recall embedding space (provider=%s model=%s dimension=%d reason=%s)",
                    resolved.provider_id,
                    resolved.response_model_id or resolved.model_id,
                    resolved.dimension,
                    space_change_reason(previous, resolved),
                )
            header = resolved
        vectors = {
            text_hash: vector
            for (text_hash, _text), vector in zip(items, result.vectors, strict=True)
        }
        if not await self._index.store_vectors(header, vectors, usage=result.usage):
            raise _SpaceMovedError()
        outcome.succeeded = True
        outcome.embedded += len(items)
        return header

    # -- Publishing --------------------------------------------------------------------

    async def _publish_state_change(self) -> None:
        if self._listeners and self._published != (self._enabled, self._state):
            await self._publish(force=True)

    async def _publish(self, *, force: bool = False) -> None:
        if not self._listeners:
            return
        now = self._monotonic()
        if not force and now - self._last_publish < STATUS_INTERVAL_S:
            return
        self._last_publish = now
        self._published = (self._enabled, self._state)
        status = await self.status()
        for listener in list(self._listeners):
            try:
                listener(status)
            except Exception as error:
                self._log_warning("Recall index status listener failed: %s", error)

    # -- Logging -----------------------------------------------------------------------

    def _warn_index_failure(self, error: BaseException) -> None:
        if self._conditions.started("index", type(error).__name__):
            self._log_warning("Recall Passage index failed (error=%s)", error)

    def _log_info(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "info"):
            self.logger.info(message, *args)

    def _log_warning(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)

    def _debug(self, message: str, *args: object) -> None:
        if self.logger is not None and hasattr(self.logger, "debug"):
            self.logger.debug(message, *args)


def _index_scope(
    sessions: ChatSessionManager, agent_id: str, project_id: str | None
) -> RecallScope:
    """Every Recall-visible live Session of one scope as refresh candidates.

    Hidden Sessions count as gone, so the catalog prunes any it still holds.
    """
    admitted = recall_visibilities(include_subagents=True)
    candidates: dict[str, tuple[str, int]] = {}
    creation_orders: dict[str, int] = {}
    for revision in sessions.list_history_revisions(agent_id, project_id):
        if revision.recall_visibility in admitted:
            session_id = revision.address.session_id
            candidates[session_id] = (revision.generation_id, revision.history_revision)
            creation_orders[session_id] = revision.creation_order
    return RecallScope(
        live_session_ids=frozenset(candidates),
        candidates=candidates,
        snapshot_id="",
        creation_orders=creation_orders,
    )


def _classify(error: BaseException) -> _Classified:
    """Sort an embedding failure into transient, permanent or configuration."""
    if isinstance(error, EmbeddingConfigurationError | EmbeddingUnsupportedTargetError):
        return _Classified("configuration", IndexFailure.of("embedding_unusable"))
    cause = error.__cause__ if error.__cause__ is not None else error
    status = getattr(cause, "status_code", None)
    if isinstance(cause, ProviderAuthError) or status in (401, 403):
        return _Classified("configuration", IndexFailure.of("provider_auth"))
    if getattr(cause, "retryable", False):
        retry_after = getattr(cause, "retry_after", None)
        code = "provider_rate_limited" if status == 429 else "provider_unavailable"
        return _Classified(
            "transient",
            IndexFailure.of(code),
            retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
        )
    if _is_context_overflow(error):
        return _Classified("permanent", IndexFailure.of("context_overflow"))
    if status in (400, 413, 422):
        return _Classified("permanent", IndexFailure.of("provider_rejected"))
    if status == 404:
        return _Classified("configuration", IndexFailure.of("embedding_model_unavailable"))
    return _Classified("configuration", IndexFailure.of("embedding_failed"))


def _is_context_overflow(error: BaseException) -> bool:
    """True when an embedding error is the provider's context-length rejection.

    The provider's 4xx body reaches Recall only through the error message.
    OpenRouter wraps the upstream ``BadRequestError`` text verbatim, so the
    stable phrases that identify a token-window overflow are matched.
    """
    message = str(error).lower()
    return (
        "context length" in message
        or "maximum context" in message
        or "context_length_exceeded" in message
        or "input_tokens" in message
    )


def _spent_cost(usage: EmbeddingUsage, price: float | None) -> float | None:
    """Reported cost when the provider reported any, else a catalog price estimate."""
    if usage.cost_reports > 0:
        return usage.cost
    if usage.requests == 0:
        return 0.0
    if price is not None and usage.token_reports > 0:
        return usage.input_tokens * price / _TOKENS_PER_PRICE_UNIT
    return None


__all__ = [
    "BATCH_SIZE",
    "IndexFailure",
    "IndexState",
    "IndexStatus",
    "SemanticIndexer",
]
