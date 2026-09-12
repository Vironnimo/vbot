"""Vector: scope behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.recall import (
    RecallSearchRequest,
)
from core.sessions import ChatSessionManager
from tests.core.recall.vector_helpers import (
    _count_vec_rows,
    _StubEmbeddings,
    backend,
    request,
    timestamp,
)

pytestmark = pytest.mark.asyncio


def _project_request(
    *,
    query: str,
    project_id: str | None,
    limit: int = 5,
) -> RecallSearchRequest:
    return RecallSearchRequest(
        agent_id="coder",
        offset=0,
        session_id=None,
        query=query,
        since=None,
        until=None,
        roles=("user", "assistant", "tool", "error", "compaction_checkpoint"),
        match_mode="all_terms",
        limit=limit,
        order="newest",
        project_id=project_id,
    )


async def test_vector_backend_project_recall_finds_only_project_sessions(tmp_path: Path) -> None:
    """A project-scoped recall searches the project's Sessions, not the global ones."""

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="global-fruit").append(
        ChatMessage.user("I love bananas and fruit", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="proj-fruit", project_id="alpha").append(
        ChatMessage.user("I love bananas and fruit too", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    project = await recall.search_page(_project_request(query="fruit", project_id="alpha"))
    identity = await recall.search_page(_project_request(query="fruit", project_id=None))

    assert [m.session_id for m in project.hits] == ["proj-fruit"]
    assert [m.session_id for m in identity.hits] == ["global-fruit"]


async def test_vector_backend_same_uuid_global_and_project_do_not_collide(tmp_path: Path) -> None:
    """The same session UUID under global and project scope index separately.

    Each scope must surface its own session content — the project session's
    chunk must not overwrite the global one in the shared vector index.
    """

    sessions = ChatSessionManager(tmp_path)
    shared_id = "11111111-1111-1111-1111-111111111111"
    sessions.create("coder", session_id=shared_id).append(
        ChatMessage.user("I bought some carrots", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id=shared_id, project_id="alpha").append(
        ChatMessage.user("I love bananas and fruit", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    # Global scope: "carrot" hits; "fruit" (only in the project session) does not.
    global_carrot = await recall.search_page(_project_request(query="carrot", project_id=None))
    assert [m.session_id for m in global_carrot.hits] == [shared_id]

    # Project scope: "fruit" hits the project session of the same UUID.
    project_fruit = await recall.search_page(_project_request(query="fruit", project_id="alpha"))
    assert [m.session_id for m in project_fruit.hits] == [shared_id]
    # The project chunk did not overwrite the global one — both vec0 rows exist.
    assert _count_vec_rows(recall.store.path, "coder", shared_id) == 2


async def test_vector_backend_identity_recall_unchanged_by_project_field(tmp_path: Path) -> None:
    """An identity recall (``project_id=None``) behaves exactly as the legacy default.

    Passing ``project_id=None`` explicitly must match the implicit-default
    behavior: same sessions, same matches, distances present.
    """

    sessions = ChatSessionManager(tmp_path)
    sessions.create("coder", session_id="cars").append(
        ChatMessage.user("My car broke down", timestamp=timestamp(1))
    )
    sessions.create("coder", session_id="vehicles").append(
        ChatMessage.user("I was driving my vehicle", timestamp=timestamp(2))
    )
    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())

    explicit_none = await recall.search_page(
        _project_request(query="car", project_id=None, limit=2)
    )
    default = await recall.search_page(request(query="car", limit=2))

    assert [m.session_id for m in explicit_none.hits] == ["cars", "vehicles"]
    assert [m.session_id for m in default.hits] == ["cars", "vehicles"]


async def test_vector_backend_does_not_match_its_own_search_output(tmp_path: Path) -> None:
    """A session_search result is excluded from the index — no self-matching loop.

    The session's only "fruit" text lives in a persisted session_search result;
    searching "fruit" must not surface the session via that artifact. Searching
    "carrot" (the real user message) still surfaces it, anchored on the user
    message — proving the artifact text was dropped, not the whole session.
    """

    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("coder", session_id="selfref")
    session.append(
        ChatMessage.tool(
            tool_call_id="c1",
            name="session_search",
            content="I love bananas and fruit",
            timestamp=timestamp(1),
        )
    )
    session.append(ChatMessage.user("I bought some carrots", timestamp=timestamp(2)))

    recall = backend(tmp_path, sessions, embeddings=_StubEmbeddings())
    fruit = await recall.search_page(request(query="fruit", limit=5))
    carrot = await recall.search_page(request(query="carrot", limit=5))

    assert all("fruit" not in hit.text for hit in fruit.hits)
    carrot_matches = [match for match in carrot.hits if match.session_id == "selfref"]
    assert len(carrot_matches) == 1
    assert carrot_matches[0].role == "user"
