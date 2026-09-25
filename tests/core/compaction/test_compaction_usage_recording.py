"""Compaction costs survive rejected results and checkpoint persistence boundaries."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from core.compaction import CompactionError, CompactionService, CompactionSettings
from core.compaction._model_request import _send_streaming_model_request
from core.sessions import SessionAddress
from tests.core.chat.usage_recorder_support import RecordingUsageRecorder
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    StubStorage,
    assistant,
    provider_request,
    user,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_compaction_attempt_records_usage_before_checkpoint_acceptance(rejected):
    class UsageAdapter(StubAdapter):
        async def stream(self, messages, **kwargs):
            yield {"type": "content_delta", "text": "Summary"}
            yield {"type": "usage", "input_tokens": 100, "output_tokens": 6}
            yield {"type": "finish", "reason": "stop"}

    recorder = RecordingUsageRecorder()
    adapter = UsageAdapter()
    messages = [user("u1", "Request"), assistant("a1", "Answer")]
    operation = CompactionService(usage_recorder=cast(Any, recorder)).compact(
        messages,
        session_address=SessionAddress(project_id="project", agent_id="coder", session_id="one"),
        prompt_cache_affinity_id="affinity",
        summary_adapter=adapter,
        summary_model_id="summary",
        summary_model_reference="openai/summary",
        active_adapter=adapter,
        active_model_id="active",
        active_model_reference="openai/active",
        run_id="run-one",
        owner_name="example",
        group_id="group-one",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="continuation"),
        request_messages=provider_request(messages),
        minimum_reclaim_tokens=100_000 if rejected else 0,
    )
    checkpoint = None
    if rejected:
        with pytest.raises(CompactionError):
            await operation
    else:
        checkpoint = await operation

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["model"] == "openai/active"
    assert call["kind"] == "compaction"
    assert call["run_id"] == "run-one"
    assert call["owner_name"] == "example"
    assert call["group_id"] == "group-one"
    assert call["usage"]["input_tokens"] == 100
    assert call["usage"]["output_tokens"] == 6
    if checkpoint is not None:
        assert checkpoint.usage["model_call"]["usage"]["usage_call_id"] == call["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_compaction_incomplete_attempt_retains_partial_usage(cancelled):
    class IncompleteAdapter:
        async def stream(self, messages, **kwargs):
            yield {"type": "usage", "input_tokens": 75}
            if cancelled:
                raise asyncio.CancelledError
            yield {"type": "finish", "reason": "output_truncated"}

    recorder = RecordingUsageRecorder()
    with pytest.raises(asyncio.CancelledError if cancelled else CompactionError):
        await _send_streaming_model_request(
            IncompleteAdapter(),
            [],
            {"model_id": "summary"},
            model_reference="openai/summary",
            usage_recorder=cast(Any, recorder),
        )
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] == ("cancelled" if cancelled else "failed")
    assert recorder.calls[0]["usage"]["input_tokens"] == 75
    assert "output_tokens" not in recorder.calls[0]["usage"]
