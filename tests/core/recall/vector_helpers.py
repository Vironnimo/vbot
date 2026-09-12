"""Shared fixtures and fakes for vector behavior tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

from core.model_tasks import (
    EmbeddingResult,
    EmbeddingSpaceIdentity,
)
from core.recall import (
    RecallBackendContext,
    RecallSearchRequest,
    VectorRecallBackend,
)
from core.sessions import ChatSessionManager


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def request(
    *,
    query: str,
    match_mode: str = "all_terms",
    roles: tuple[str, ...] = ("user", "assistant", "tool", "error", "compaction_checkpoint"),
    limit: int = 5,
) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        offset=0,
        session_id=None,
        query=query,
        since=None,
        until=None,
        roles=roles,
        match_mode=match_mode,  # type: ignore[arg-type]
        limit=limit,
        order="newest",
    )


class _StubEmbeddings:
    """Deterministic stub embedding service for vector recall tests."""

    def __init__(self, *, dimension: int = 4) -> None:
        self.dimension = dimension
        self.provider_id = "openrouter"
        self.model_id = "stub-embed"
        self.response_model_id = ""
        self.space_fingerprint = "stub-space-a"
        self.embed_calls: list[list[str]] = []
        self.embed_purposes: list[str | None] = []
        self.resolve_calls = 0

    def resolve_model_id(self) -> tuple[str, str]:
        self.resolve_calls += 1
        return (self.provider_id, self.model_id)

    def resolve_space(self) -> EmbeddingSpaceIdentity:
        self.resolve_calls += 1
        return EmbeddingSpaceIdentity(
            provider_id=self.provider_id,
            model_id=self.model_id,
            fingerprint=self.space_fingerprint,
        )

    async def embed(
        self,
        texts: list[str],
        *,
        purpose: str | None = None,
    ) -> EmbeddingResult:
        self.embed_calls.append(list(texts))
        self.embed_purposes.append(purpose)
        vectors: list[list[float]] = [self._vector_for(text) for text in texts]
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id=self.model_id,
            provider_id=self.provider_id,
            dimension=self.dimension,
            space_fingerprint=self.space_fingerprint,
            response_model_id=self.response_model_id,
        )

    def _vector_for(self, text: str) -> list[float]:
        lowered = text.lower()
        # Deterministic slot assignments — the same text always maps to the
        # same vector so cosine distance is a stable test signal.
        if "car" in lowered and "driving" not in lowered:
            return [1.0, 0.0, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        if "vehicle" in lowered or "driving" in lowered:
            return [0.9, 0.1, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        if "banana" in lowered or "fruit" in lowered:
            return [0.0, 0.0, 1.0, 0.0] + [0.0] * (self.dimension - 4)
        if "carrot" in lowered or "vegetable" in lowered:
            return [0.0, 1.0, 0.0, 0.0] + [0.0] * (self.dimension - 4)
        return [0.5, 0.5, 0.0, 0.0] + [0.0] * (self.dimension - 4)


def backend(
    tmp_path: Path,
    sessions: ChatSessionManager,
    *,
    embeddings: Any | None = None,
    logger: Any | None = None,
) -> VectorRecallBackend:
    return VectorRecallBackend(
        RecallBackendContext(
            data_dir=tmp_path,
            sessions=sessions,
            embeddings=embeddings,
            logger=logger,
        )
    )


# Chunking policy — build_session_passages
# Chunked indexing + search integration
def _count_vec_rows(store_path: Path, agent_id: str, session_id: str) -> int:
    """Open the on-disk store and count vec0 rows for one session via the chunks table."""

    connection = sqlite3.connect(store_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE agent_id = ? AND session_id = ?",
            (agent_id, session_id),
        ).fetchone()
        return int(row["c"])
    finally:
        connection.close()
