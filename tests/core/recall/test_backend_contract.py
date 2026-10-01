"""Contracts every Recall backend shares: scope isolation and continuation binding."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.recall import (
    CanonicalSessionRecallBackend,
    HybridRecallBackend,
    RecallBackend,
    RecallBackendContext,
    RecallBackendRegistry,
    RecallSearchError,
    VectorRecallBackend,
)
from core.sessions import ChatSessionManager
from tests.core.recall.recall_test_support import (
    StubEmbeddings,
    embed_documents,
    request,
    timestamp,
)

pytestmark = pytest.mark.asyncio

BACKENDS = ["canonical_scan", "sqlite_fts", "vector", "hybrid"]


async def _backend(name: str, tmp_path: Path, sessions: ChatSessionManager) -> RecallBackend:
    """Create *name* over the existing Sessions, their documents already embedded."""
    if name == "canonical_scan":
        return CanonicalSessionRecallBackend(sessions)
    embeddings = StubEmbeddings()
    context = RecallBackendContext(tmp_path, sessions, embeddings=embeddings)
    backend = RecallBackendRegistry.with_builtins().create(name, context)
    if isinstance(backend, VectorRecallBackend | HybridRecallBackend):
        await embed_documents(backend.index, sessions, embeddings)
    return backend


# Every backend binds its continuation through the shared scope read, so the
# internal scan proves each selection field and every other backend one of them.
_SELECTION_CHANGES: dict[str, dict[str, Any]] = {
    "query": {"query": "different"},
    "roles": {"roles": ("user",)},
    "since": {"since": datetime(2026, 1, 1, tzinfo=UTC)},
    "until": {"until": datetime(2026, 12, 31, tzinfo=UTC)},
    "match-mode": {"match_mode": "phrase"},
    "order": {"order": "oldest"},
    "session": {"session_id": "one"},
    "exclusions": {"excluded_session_ids": ("nonexistent",)},
    "subagents": {"include_subagents": True},
}


@pytest.mark.parametrize(
    ("backend_name", "changed"),
    [
        *(
            pytest.param("canonical_scan", change, id=f"canonical_scan-{field}")
            for field, change in _SELECTION_CHANGES.items()
        ),
        pytest.param("sqlite_fts", _SELECTION_CHANGES["roles"], id="sqlite_fts-roles"),
        pytest.param("vector", _SELECTION_CHANGES["session"], id="vector-session"),
        pytest.param("hybrid", _SELECTION_CHANGES["subagents"], id="hybrid-subagents"),
    ],
)
async def test_changed_selection_rejects_continuation(
    tmp_path: Path, sessions: ChatSessionManager, backend_name: str, changed: dict[str, Any]
) -> None:
    session = sessions.create("coder", session_id="one")
    for day in (1, 2):
        session.append(ChatMessage.user("needle", timestamp=datetime(2026, 5, day, tzinfo=UTC)))
    recall = await _backend(backend_name, tmp_path, sessions)
    original = request("needle", limit=1)
    first = await recall.search_page(original)
    continuation = replace(original, offset=1, limit=2, snapshot_id=first.snapshot_id)

    # Offset and page size may change within one selection.
    continued = await recall.search_page(continuation)
    assert continued.snapshot_id == first.snapshot_id
    with pytest.raises(RecallSearchError) as error:
        await recall.search_page(replace(continuation, **changed))
    assert error.value.code == "stale_cursor"


@pytest.mark.parametrize("backend_name", BACKENDS)
async def test_project_scope_isolates_sessions_that_share_an_id(
    tmp_path: Path, sessions: ChatSessionManager, backend_name: str
) -> None:
    shared_id = "11111111-1111-1111-1111-111111111111"
    sessions.create("coder", session_id=shared_id).append(
        ChatMessage.user("global carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id=shared_id, project_id="alpha").append(
        ChatMessage.user("project bananas", timestamp=timestamp(2))
    )
    recall = await _backend(backend_name, tmp_path, sessions)

    for project_id, text in ((None, "global carrots"), ("alpha", "project bananas")):
        page = await recall.search_page(
            request("carrots bananas", project_id=project_id, match_mode="any_term")
        )
        assert [(hit.session_id, hit.text) for hit in page.hits] == [(shared_id, text)]
