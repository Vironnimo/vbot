"""Continuation: the active Model writes the checkpoint as an append to its own request."""

from __future__ import annotations

import pytest

from core.compaction import CompactionSettings
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    StubStorage,
    _tail_token_span,
    assistant,
    checkpoint,
    compact,
    provider_request,
    user,
)

_CONTINUATION = CompactionSettings(strategy="continuation")


@pytest.mark.asyncio
async def test_continuation_preserves_request_prefix_and_active_tools() -> None:
    active = StubAdapter("ACTIVE SUMMARY")
    summary = StubAdapter("must not be used")
    storage = StubStorage()
    request = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]

    result = await compact(
        [user("u1", "hello"), assistant("a1", "answer")],
        summary_adapter=summary,
        storage=storage,
        settings=_CONTINUATION,
        request_messages=request,
        active_adapter=active,
        active_model_id="openai/active",
        active_tools=tools,
        summary_temperature=1.0,
        active_temperature=0.2,
    )

    assert summary.requests == []
    [sent] = active.requests
    assert sent["messages"][:-1] == request
    assert "checkpoint" in sent["messages"][-1]["content"]
    assert sent["tools"] == tools
    assert sent["temperature"] == 0.2
    assert result.content == "ACTIVE SUMMARY"
    assert result.projection is not None
    assert len(result.projection) == 1
    assert storage.read_names == ["compaction-continuation.md"]


@pytest.mark.asyncio
async def test_continuation_counts_only_content_after_the_previous_checkpoint() -> None:
    later = [user("u2", "later request " * 50), assistant("a2", "later answer " * 50)]
    prior = checkpoint([], count=1_000)

    result = await compact(
        [user("u1", "old request"), prior, *later],
        summary_adapter=StubAdapter("must not be used"),
        settings=_CONTINUATION,
        request_messages=provider_request(later),
        active_adapter=StubAdapter("NEXT CHECKPOINT"),
        active_model_id="openai/active",
    )

    assert result.usage is not None
    assert result.usage["compacted_token_count"] == 1_000 + _tail_token_span(later)
