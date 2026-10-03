"""Shared requests, fake embeddings, index readers and loop guards for Recall tests."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pytest
import sqlite_vec  # type: ignore[import-untyped]

from core.database import Database
from core.model_tasks import EmbeddingResult, EmbeddingSpaceIdentity
from core.recall import (
    HybridRecallBackend,
    PassageIndex,
    RecallSearchRequest,
    SemanticIndexer,
    VectorRecallBackend,
)
from core.sessions import ChatSessionManager

CONVERSATION_ROLES = ("user", "assistant", "error", "compaction_checkpoint")
ALL_ROLES = ("user", "assistant", "tool", "error", "compaction_checkpoint")


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def request(query: str, **changes: Any) -> RecallSearchRequest:
    """A relevance-ordered search over Agent ``coder``'s conversation roles."""

    base = RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        session_id=None,
        query=query,
        since=None,
        until=None,
        roles=CONVERSATION_ROLES,
        match_mode="all_terms",
        order="relevance",
        offset=0,
        limit=10,
    )
    return dataclasses.replace(base, **changes)


class StubEmbeddings:
    """Deterministic embedding service: the same text always maps to the same vector.

    ``car`` text (without ``driving``) is ``[1, 0, 0, 0]``, ``vehicle``/``driving``
    text is its near neighbour, ``banana``/``fruit`` and ``carrot``/``vegetable``
    are orthogonal clusters, and everything else shares one default vector.
    """

    def __init__(self, *, dimension: int = 4) -> None:
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.response_model_id = ""
        self.space_fingerprint = "stub-space-a"
        self.local = False
        # Texts per document request the target prefers; ``None`` means no preference.
        self.batch_size_hint: int | None = None
        self.embed_calls: list[list[str]] = []
        self.embed_purposes: list[str | None] = []

    def inputs(self, purpose: str) -> list[str]:
        """Every text embedded for *purpose*, in call order."""

        return [
            text
            for texts, call_purpose in zip(self.embed_calls, self.embed_purposes, strict=True)
            if call_purpose == purpose
            for text in texts
        ]

    @property
    def document_inputs(self) -> list[str]:
        return self.inputs("document")

    def resolve_space(self) -> EmbeddingSpaceIdentity:
        return EmbeddingSpaceIdentity(
            provider_id=self.provider_id,
            model_id=self.model_id,
            fingerprint=self.space_fingerprint,
            local=self.local,
        )

    def document_batch_size(self) -> int | None:
        return self.batch_size_hint

    async def embed(self, texts: list[str], *, purpose: str | None = None) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        self.embed_purposes.append(purpose)
        return EmbeddingResult(
            vectors=tuple(self.vector_for(text) for text in texts),
            model_id=self.model_id,
            provider_id=self.provider_id,
            dimension=self.dimension,
            space_fingerprint=self.space_fingerprint,
            response_model_id=self.response_model_id,
        )

    def vector_for(self, text: str) -> list[float]:
        lowered = text.lower()
        if "car" in lowered and "driving" not in lowered:
            slots = [1.0, 0.0, 0.0, 0.0]
        elif "vehicle" in lowered or "driving" in lowered:
            slots = [0.9, 0.1, 0.0, 0.0]
        elif "banana" in lowered or "fruit" in lowered:
            slots = [0.0, 0.0, 1.0, 0.0]
        elif "carrot" in lowered or "vegetable" in lowered:
            slots = [0.0, 1.0, 0.0, 0.0]
        else:
            slots = [0.5, 0.5, 0.0, 0.0]
        return slots + [0.0] * (self.dimension - 4)


class VectorBackendFactory(Protocol):
    """The ``vector_backend`` fixture: opens a ``vector`` backend over the test's Sessions."""

    def __call__(
        self,
        *,
        embeddings: Any | None = None,
        logger: Any | None = None,
        on_waiting: Callable[[], None] | None = None,
    ) -> VectorRecallBackend: ...


class HybridBackendFactory(Protocol):
    """The ``hybrid_backend`` fixture: opens a ``hybrid`` backend over the test's Sessions."""

    def __call__(self, *, embeddings: Any | None = None) -> HybridRecallBackend: ...


async def embed_documents(
    index: PassageIndex, sessions: ChatSessionManager, embeddings: Any
) -> None:
    """Run one indexing pass: refresh every live scope and embed every waiting text."""

    indexer = SemanticIndexer(
        index=index, sessions=sessions, embeddings=embeddings, binding_configured=lambda: True
    )
    indexer.set_enabled(True)
    try:
        await indexer.run_pass()
    finally:
        await indexer.aclose()


def connect_store(store_path: Path) -> sqlite3.Connection:
    """Open the Passage index directly, with sqlite-vec loaded."""

    connection = sqlite3.connect(store_path)
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    return connection


def passage_rows(store_path: Path, agent_id: str, session_id: str) -> dict[int, str]:
    """Return ``{passage_ref: text}`` for the vectorized Passages one Session shows.

    Covers every scope that holds a Session with this id.
    """

    connection = connect_store(store_path)
    try:
        rows = connection.execute(
            """
            SELECT p.passage_ref, p.text FROM passages AS p
            JOIN passage_views AS v ON v.passage_ref = p.passage_ref
            JOIN indexed_sessions AS s ON s.session_ref = v.session_ref
            JOIN passage_vectors AS vec ON vec.rowid = p.passage_ref
            WHERE s.agent_id = ? AND s.session_id = ?
            ORDER BY p.passage_ref
            """,
            (agent_id, session_id),
        ).fetchall()
        return {int(row[0]): str(row[1]) for row in rows}
    finally:
        connection.close()


def pending_count(store_path: Path) -> int:
    """Count stored Passages still waiting for a vector."""

    connection = connect_store(store_path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM pending_vectors").fetchone()[0])
    finally:
        connection.close()


def forbid_event_loop_calls(monkeypatch: pytest.MonkeyPatch, *targets: object) -> list[str]:
    """Fail any synchronous public method of *targets* that runs on the event loop.

    Returns the names of the guarded methods called off the loop, in order.
    """

    calls: list[str] = []
    for target in targets:
        for name in dir(target):
            if name.startswith("_"):
                continue
            method = getattr(target, name)
            if not inspect.ismethod(method) or inspect.iscoroutinefunction(method):
                continue
            monkeypatch.setattr(target, name, _off_loop(name, method, calls))
    return calls


def forbid_database_calls_on_loop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail any blocking kernel database read or write made off its own worker pool.

    A call on the event loop fails, and so does one on any thread other than
    the database's bounded worker pool. Returns ``<database>.<read|write>`` for
    every call made on the pool, in order.
    """

    calls: list[str] = []
    for name in ("read", "write"):
        method = getattr(Database, name)

        def guarded(
            self: Database, *args: Any, _name: str = name, _method: Any = method, **kwargs: Any
        ) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                thread = threading.current_thread().name
                if not thread.startswith(f"vbot-db-{self.name}_"):
                    raise AssertionError(
                        f"{self.name}.{_name} ran on {thread}, not on the database's worker pool"
                    ) from None
                calls.append(f"{self.name}.{_name}")
                return _method(self, *args, **kwargs)
            raise AssertionError(f"{self.name}.{_name} ran synchronously on the event loop")

        monkeypatch.setattr(Database, name, guarded)
    return calls


def _off_loop(
    name: str,
    method: Callable[..., Any],
    calls: list[str],
) -> Callable[..., Any]:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(name)
            return method(*args, **kwargs)
        raise AssertionError(f"{name} ran synchronously on the event loop")

    return guarded
