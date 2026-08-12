"""Recall backend interfaces and registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from core.sessions import ChatSessionManager

JsonObject = dict[str, Any]
RecallMatchMode = Literal["all_terms", "any_term", "phrase"]
RecallSortMode = Literal["newest", "oldest"]
RecallOrder = Literal["relevance", "newest", "oldest"]
RecallResultType = Literal["message", "passage", "backend_defined"]

RECALL_BACKEND_JSONL_SCAN = "jsonl_scan"
RECALL_BACKEND_SQLITE_FTS = "sqlite_fts"
RECALL_BACKEND_VECTOR = "vector"
RECALL_BACKEND_HYBRID = "hybrid"
DEFAULT_RECALL_BACKEND = RECALL_BACKEND_JSONL_SCAN
FIRST_PARTY_RECALL_BACKENDS = frozenset(
    {
        RECALL_BACKEND_JSONL_SCAN,
        RECALL_BACKEND_SQLITE_FTS,
        RECALL_BACKEND_VECTOR,
        RECALL_BACKEND_HYBRID,
    }
)


class RecallSearchError(RuntimeError):
    """Expected first-party search failure with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RecallRequest:
    agent_id: str
    session_id: str | None
    around_message_id: str | None
    query: str | None
    since: datetime | None
    until: datetime | None
    roles: tuple[str, ...]
    match_mode: RecallMatchMode
    limit: int
    context_messages: int
    bookend_messages: int
    sort: RecallSortMode
    # Project the recall is scoped to, or ``None`` for the identity/global scope.
    # A recall run searches and indexes the Sessions of *its* scope: ``None``
    # reaches the identity Sessions under ``agents/<id>/sessions/`` exactly as
    # before; a project id reaches that project's anchored Sessions. Additive
    # with a ``None`` default so every existing caller keeps today's behavior.
    project_id: str | None = None


@dataclass(frozen=True)
class RecallSearchCapabilities:
    """Model-facing search behavior implemented by one first-party backend."""

    result_type: RecallResultType
    guidance: str
    tool_summary: str | None = None
    query_description: str | None = None
    match_argument: str | None = None
    match_modes: tuple[RecallMatchMode, ...] = ()
    order_modes: tuple[RecallOrder, ...] = ("relevance",)
    default_order: RecallOrder = "relevance"
    supports_roles: bool = False


@dataclass(frozen=True)
class RecallSearchRequest:
    """Normalized query-only request for the first-party Recall contract."""

    agent_id: str
    project_id: str | None
    session_id: str | None
    query: str
    since: datetime | None
    until: datetime | None
    roles: tuple[str, ...]
    match_mode: RecallMatchMode
    order: RecallOrder
    offset: int
    limit: int
    snapshot_id: str | None = None
    excluded_session_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecallSearchHit:
    """One ranked retrieval item before Tool-level excerpt fitting."""

    result_type: RecallResultType
    session_id: str
    message_id: str
    role: str
    timestamp: str
    text: str
    score: float
    passage_id: str | None = None
    start_message_id: str | None = None
    end_message_id: str | None = None
    end_timestamp: str | None = None
    match_start: int | None = None
    match_end: int | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecallSearchPage:
    """A deterministic slice of one backend ranking."""

    hits: tuple[RecallSearchHit, ...]
    result_type: RecallResultType
    ranking: str
    snapshot_id: str
    has_more: bool
    total_candidate_sessions: int
    degraded: bool = False
    degradation_reason: str | None = None


@dataclass(frozen=True)
class RecallBackendContext:
    data_dir: Path
    sessions: ChatSessionManager
    logger: Any | None = None
    # The vector recall backend uses these to resolve the embedding binding
    # and look up the bound model's context window; both are optional so
    # the JSONL/FTS backends keep working unchanged.
    embeddings: Any | None = None
    model_registry: Any | None = None


class RecallBackend(Protocol):
    async def browse(self, request: RecallRequest) -> JsonObject:
        """Return session summaries for a recall request."""

    async def overview(self, request: RecallRequest) -> JsonObject:
        """Return one session's overview: start/end messages and a total count."""

    async def search(self, request: RecallRequest) -> JsonObject:
        """Return query matches for a recall request."""

    async def scroll(self, request: RecallRequest) -> JsonObject:
        """Return an anchored context view for a recall request."""


@runtime_checkable
class SupportsRecallSearch(Protocol):
    """Typed query contract used by first-party and upgraded extension backends."""

    def search_capabilities(self) -> RecallSearchCapabilities:
        """Describe the search controls and result unit this backend implements."""

    async def search_page(self, request: RecallSearchRequest) -> RecallSearchPage:
        """Return one deterministic page from the backend-native ranking."""


@runtime_checkable
class SupportsSessionRemoval(Protocol):
    """Optional backend capability: drop one session from a derived index.

    Recall is otherwise read-only (:class:`RecallBackend`). Backends that keep a
    derived index (SQLite FTS, vector) implement this so session deletion can
    evict a removed session immediately instead of waiting for the next
    self-healing reconcile on search. The JSONL live-scan backend has no derived
    index and deliberately does not implement it — an archived session is already
    absent from the live directory it scans. The runtime checks ``isinstance``
    before calling, so a backend without removal simply falls back to
    self-healing rather than erroring.
    """

    async def remove_session(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Remove all index entries for one session in the given project scope."""


RecallBackendFactory = Callable[[RecallBackendContext], RecallBackend]


class RecallBackendRegistry:
    """Registry for first-party and extension-provided recall backends."""

    def __init__(self) -> None:
        self._factories: dict[str, RecallBackendFactory] = {}

    @classmethod
    def with_builtins(cls) -> RecallBackendRegistry:
        from core.recall.hybrid import HybridRecallBackend
        from core.recall.jsonl import JsonlSessionRecallBackend
        from core.recall.sqlite_fts import SqliteFtsRecallBackend
        from core.recall.vector import VectorRecallBackend

        registry = cls()
        registry.register(
            RECALL_BACKEND_JSONL_SCAN,
            lambda context: JsonlSessionRecallBackend(context.sessions),
        )
        registry.register(RECALL_BACKEND_SQLITE_FTS, SqliteFtsRecallBackend)
        registry.register(RECALL_BACKEND_VECTOR, VectorRecallBackend)
        registry.register(RECALL_BACKEND_HYBRID, HybridRecallBackend)
        return registry

    def register(self, name: str, factory: RecallBackendFactory) -> None:
        normalized_name = name.strip()
        if not normalized_name or normalized_name != normalized_name.lower():
            raise ValueError("recall backend names must use lowercase snake_case")
        if normalized_name in self._factories:
            raise ValueError(f"recall backend already registered: {normalized_name}")
        self._factories[normalized_name] = factory

    def create(self, name: str, context: RecallBackendContext) -> RecallBackend:
        try:
            factory = self._factories[name]
        except KeyError as error:
            raise KeyError(f"unknown recall backend: {name}") from error
        return factory(context)

    def names(self) -> list[str]:
        return sorted(self._factories)
