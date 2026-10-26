"""Continuation: the active Model writes the checkpoint as an append to its own request."""

from __future__ import annotations

from typing import Any

import pytest

from core.compaction import (
    CompactionInsufficientReclaimError,
    CompactionService,
    CompactionSettings,
)
from core.utils.tokens import REPLAYED_REASONING_FRAMING_TOKENS
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    StubStorage,
    assistant,
    checkpoint,
    compact,
    provider_request,
    tool_step,
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
    )

    assert summary.requests == []
    [sent] = active.requests
    assert sent["messages"][:-1] == request
    assert "checkpoint" in sent["messages"][-1]["content"]
    assert sent["tools"] == tools
    assert "temperature" not in sent
    assert result.content == "ACTIVE SUMMARY"
    assert result.projection is not None
    assert len(result.projection) == 1
    assert storage.read_names == ["compaction-continuation.md"]


class _WireAdapter(StubAdapter):
    """Counts every request message as 10 tokens on its wire."""

    def estimate_request_input_tokens(
        self, messages: list[dict[str, Any]], *, model_id: str, tools: Any = None
    ) -> int:
        return 10 * len(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("programmatic", [False, True])
async def test_continuation_counts_new_content_as_the_active_request_sends_it(
    programmatic: bool,
) -> None:
    later = [user("u2", "later request " * 50), assistant("a2", "later answer " * 50)]
    messages = [user("u1", "old request"), checkpoint([], count=1_000), *later]
    request = provider_request(later)
    if programmatic:
        messages[-1:-1] = tool_step(
            "child", "intermediate " * 5_000, parent_tool_call_id="call-code"
        )
    # Replayed reasoning costs its reported tokens, never its text or opaque state.
    request[2].update(
        reasoning="long thought " * 2_000,
        reasoning_meta={"signature": "sealed" * 500},
        reported_reasoning_tokens=7_000,
    )
    # Two request messages (20) and the reported reasoning with its framing, doubled.
    later_tokens = (20 + 7_000 + REPLAYED_REASONING_FRAMING_TOKENS) * 2
    active = _WireAdapter("NEXT CHECKPOINT")
    options: dict[str, Any] = {
        "request_messages": request,
        "active_adapter": active,
        "active_model_id": "openai/active",
        "estimate_factor": 2.0,
    }

    for floor, worth_it in ((later_tokens, True), (later_tokens + 1, False)):
        assert (
            CompactionService().has_new_compactable_context(
                messages, _CONTINUATION, minimum_reclaim_tokens=floor, **options
            )
            is worth_it
        )
    result = await compact(
        messages,
        summary_adapter=StubAdapter("must not be used"),
        settings=_CONTINUATION,
        minimum_reclaim_tokens=later_tokens,
        **options,
    )

    # Only content after the previous checkpoint counts as newly compacted.
    assert result.usage is not None
    assert result.usage["compacted_token_count"] == 1_000 + later_tokens
    assert "intermediate" not in str(active.requests)
    # The new summary replaces the previous one: the reclaim is the later content.
    with pytest.raises(CompactionInsufficientReclaimError):
        await compact(
            messages,
            summary_adapter=StubAdapter("must not be used"),
            settings=_CONTINUATION,
            minimum_reclaim_tokens=later_tokens + 1,
            **options,
        )
