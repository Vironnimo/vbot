"""Recall backend interfaces and registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from core.sessions import ChatSessionManager

JsonObject = dict[str, Any]
RecallMatchMode = Literal["all_terms", "any_term", "phrase"]
RecallSortMode = Literal["newest", "oldest"]

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
    def browse(self, request: RecallRequest) -> JsonObject:
        """Return session summaries for a recall request."""

    def overview(self, request: RecallRequest) -> JsonObject:
        """Return one session's overview: start/end messages and a total count."""

    def search(self, request: RecallRequest) -> JsonObject:
        """Return query matches for a recall request."""

    def scroll(self, request: RecallRequest) -> JsonObject:
        """Return an anchored context view for a recall request."""


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
