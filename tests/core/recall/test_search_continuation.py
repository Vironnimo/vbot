"""Search continuations remain bound to their original selection."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import (
    CanonicalSessionRecallBackend,
    RecallBackendContext,
    RecallBackendRegistry,
    RecallSearchError,
    RecallSearchRequest,
)
from core.sessions import ChatSessionManager, FtsHealth, _store_fts
from tests.core.recall.vector_helpers import _StubEmbeddings


def request() -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        project_id=None,
        session_id=None,
        query="needle",
        since=None,
        until=None,
        roles=("user", "assistant"),
        match_mode="all_terms",
        order="relevance",
        offset=0,
        limit=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["canonical_scan", "sqlite_fts", "vector", "hybrid"])
@pytest.mark.parametrize(
    "changed",
    [
        {"query": "different"},
        {"roles": ("user",)},
        {"since": datetime(2026, 1, 1, tzinfo=UTC)},
        {"until": datetime(2026, 12, 31, tzinfo=UTC)},
        {"match_mode": "phrase"},
        {"order": "oldest"},
        {"session_id": "one"},
        {"excluded_session_ids": ("nonexistent",)},
        {"include_subagents": True},
    ],
)
async def test_changed_selection_rejects_continuation(
    tmp_path: Path, backend_name: str, changed: dict[str, Any]
) -> None:
    sessions = ChatSessionManager(tmp_path)
    try:
        session = sessions.create("coder", session_id="one")
        for day in (1, 2):
            session.append(ChatMessage.user("needle", timestamp=datetime(2026, 5, day, tzinfo=UTC)))
        context = RecallBackendContext(tmp_path, sessions, embeddings=_StubEmbeddings())
        recall = (
            CanonicalSessionRecallBackend(sessions)
            if backend_name == "canonical_scan"
            else RecallBackendRegistry.with_builtins().create(backend_name, context)
        )
        original = request()
        first = await recall.search_page(original)
        continuation = replace(original, offset=1, limit=2, snapshot_id=first.snapshot_id)

        continued = await recall.search_page(continuation)
        assert continued.snapshot_id == first.snapshot_id
        with pytest.raises(RecallSearchError) as error:
            await recall.search_page(replace(continuation, **changed))
        assert error.value.code == "stale_cursor"
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_fts_fallback_preserves_original_selection_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    try:
        session = sessions.create("coder", session_id="one")
        session.append(ChatMessage.user("needle"))
        recall = RecallBackendRegistry.with_builtins().create(
            "sqlite_fts", RecallBackendContext(tmp_path, sessions)
        )

        monkeypatch.setattr(
            _store_fts,
            "_fts_health_from_connection",
            lambda *_args, **_kwargs: FtsHealth(state="unavailable", reason="test"),
        )
        original = request()
        first = await recall.search_page(original)
        continued = await recall.search_page(
            replace(original, offset=1, snapshot_id=first.snapshot_id)
        )
        assert first.degraded and continued.degraded
        assert continued.snapshot_id == first.snapshot_id
        assert continued.hits == ()
    finally:
        sessions.close()
