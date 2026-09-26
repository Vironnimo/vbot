"""Compaction Model invocation, acceptance, and durable usage boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from core.chat.streaming import StreamingAccumulator, iter_with_chunk_timeout
from core.chat.wire_shaping import model_facing_request
from core.compaction.errors import CompactionError
from core.providers.adapter import TERMINAL_OUTCOME_STOP

if TYPE_CHECKING:
    from core.sessions import SessionAddress
    from core.usage import UsageRecorder


async def _send_streaming_model_request(
    adapter: Any,
    messages: list[dict[str, Any]],
    request_options: dict[str, Any],
    *,
    usage_recorder: UsageRecorder | None = None,
    model_reference: str | None = None,
    session_address: SessionAddress | None = None,
    run_id: str | None = None,
    owner_name: str | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    """Consume one canonical stream, accepting only a completed text response.

    Some providers (observed on OpenRouter's stealth tier) reject large
    non-streaming completions outright while streaming the same payload fine,
    so Compaction always streams. Adapter deltas are already normalized and must
    never be passed back through a raw-wire response parser.
    """
    accumulator = StreamingAccumulator()
    tools = request_options.get("tools")
    messages, model_tools = model_facing_request(messages, list(tools or []))
    if tools is not None:
        request_options = {**request_options, "tools": model_tools}
    call_id = (
        await usage_recorder.start(
            model=model_reference or str(request_options.get("model_id") or "unknown"),
            kind="compaction",
            agent_id=session_address.agent_id if session_address is not None else None,
            session_id=session_address.session_id if session_address is not None else None,
            project_id=session_address.project_id if session_address is not None else None,
            run_id=run_id,
            owner_name=owner_name,
            group_id=group_id,
        )
        if usage_recorder is not None
        else None
    )
    try:
        # Internal maintenance remains bounded even when a local Model stalls.
        async for delta in iter_with_chunk_timeout(adapter.stream(messages, **request_options)):
            if delta.get("type") != "heartbeat":
                accumulator.add_delta(delta)
        if accumulator.finish_reason != TERMINAL_OUTCOME_STOP:
            raise CompactionError(
                "Summary stream did not complete successfully "
                f"(outcome={accumulator.finish_reason or 'missing'})"
            )
        if accumulator.has_partial_tool_call:
            raise CompactionError(
                "Summary stream requested Tools instead of completing its summary"
            )
        response = accumulator.finalize_assistant_fields().to_response_dict()
    except BaseException as exc:
        if usage_recorder is not None and call_id is not None:
            await usage_recorder.finish(
                call_id,
                accumulator.usage,
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            )
        raise
    if usage_recorder is not None and call_id is not None:
        response["usage"] = await usage_recorder.finish(call_id, accumulator.usage)
    return response
