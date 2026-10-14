"""Compaction Model invocation, acceptance, and durable usage boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from core.chat.streaming import StreamingAccumulator, iter_with_chunk_timeout
from core.compaction.errors import CompactionError
from core.providers.adapter import TERMINAL_OUTCOME_STOP
from core.utils.logging import get_logger
from core.utils.retry import compute_retry_delay

if TYPE_CHECKING:
    from core.sessions import SessionAddress
    from core.usage import UsageRecorder

_LOGGER = get_logger("compaction")

# Tests patch this seam instead of the process-wide ``asyncio.sleep``.
_sleep = asyncio.sleep

# A retryable Provider failure, such as a stream cut short by a dropped
# connection, gets one more attempt; recovery beyond it belongs to the caller.
MODEL_REQUEST_ATTEMPTS = 2
MODEL_REQUEST_TIMEOUT_SECONDS = 7 * 60
MAX_SUMMARY_CHARACTERS = 100_000
_TIME_LIMIT_MESSAGE = "Compaction exceeded its 7-minute time limit. No checkpoint was saved."


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

    ``messages`` and ``request_options["tools"]`` are already the Model-facing
    projection (``core.chat.wire_shaping.model_facing_request``).

    Some providers (observed on OpenRouter's stealth tier) reject large
    non-streaming completions outright while streaming the same payload fine,
    so Compaction always streams. Adapter deltas are already normalized and must
    never be passed back through a raw-wire response parser.

    A retryable Provider failure is retried in place once after the shared
    backoff. Acceptance failures and cancellation are never retried. Every
    attempt records its own Usage. One absolute deadline covers all attempts
    and their backoff, even while the Model keeps producing deltas.
    """
    model = model_reference or str(request_options.get("model_id") or "unknown")
    budget = asyncio.timeout(MODEL_REQUEST_TIMEOUT_SECONDS)
    try:
        async with budget:
            attempt = 0
            while True:
                if _budget_exhausted(budget):
                    raise CompactionError(_TIME_LIMIT_MESSAGE)
                attempt += 1
                try:
                    response = await _stream_attempt(
                        adapter,
                        messages,
                        request_options,
                        budget=budget,
                        model=model,
                        usage_recorder=usage_recorder,
                        session_address=session_address,
                        run_id=run_id,
                        owner_name=owner_name,
                        group_id=group_id,
                    )
                    if _budget_exhausted(budget):
                        raise CompactionError(_TIME_LIMIT_MESSAGE)
                    return response
                except Exception as error:
                    if attempt >= MODEL_REQUEST_ATTEMPTS or not getattr(error, "retryable", False):
                        raise
                    hint = getattr(error, "retry_after", None)
                    delay, _ = compute_retry_delay(
                        attempt - 1, retry_after=hint if isinstance(hint, (int, float)) else None
                    )
                    _LOGGER.debug(
                        "Compaction Model request failed; retrying once in %.2fs "
                        "(run=%s session=%s model=%s cause=%s: %s)",
                        delay,
                        run_id,
                        session_address.session_id if session_address is not None else None,
                        model,
                        type(error).__name__,
                        error,
                    )
                    await _sleep(delay)
    except TimeoutError as error:
        if not budget.expired():
            raise
        raise CompactionError(_TIME_LIMIT_MESSAGE) from error


async def _stream_attempt(
    adapter: Any,
    messages: list[dict[str, Any]],
    request_options: dict[str, Any],
    *,
    budget: asyncio.Timeout,
    model: str,
    usage_recorder: UsageRecorder | None,
    session_address: SessionAddress | None,
    run_id: str | None,
    owner_name: str | None,
    group_id: str | None,
) -> dict[str, Any]:
    """Run one stream attempt and record its Usage, whatever its outcome."""
    accumulator = StreamingAccumulator()
    call_id = (
        await usage_recorder.start(
            model=model,
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
        # Close the Provider iterator before recording failure or cancellation;
        # an output limit can stop consumption between two stream reads.
        source = adapter.stream(messages, **request_options)
        stream_failed = False
        try:
            summary_characters = 0
            async for delta in iter_with_chunk_timeout(source):
                if _budget_exhausted(budget):
                    raise CompactionError(_TIME_LIMIT_MESSAGE)
                if delta.get("type") == "heartbeat":
                    continue
                if delta.get("type") == "content_delta" and isinstance(delta.get("text"), str):
                    summary_characters += len(delta["text"])
                    if summary_characters > MAX_SUMMARY_CHARACTERS:
                        raise CompactionError(
                            f"Compaction exceeded its {MAX_SUMMARY_CHARACTERS:,}-character "
                            "summary limit. No checkpoint was saved."
                        )
                accumulator.add_delta(delta)
        except BaseException:
            stream_failed = True
            raise
        finally:
            if close := getattr(source, "aclose", None):
                try:
                    await close()
                except Exception as error:
                    if not stream_failed:
                        raise
                    # Cleanup must not turn a limit or cancellation into a
                    # retryable Provider error and start another request.
                    _LOGGER.warning(
                        "Compaction stream cleanup failed while aborting "
                        "(run=%s model=%s cause=%s)",
                        run_id,
                        model,
                        type(error).__name__,
                    )
        if _budget_exhausted(budget):
            # A Provider can suppress cancellation while closing its stream.
            raise CompactionError(_TIME_LIMIT_MESSAGE)
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
                status=(
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError) and not budget.expired()
                    else "failed"
                ),
            )
        raise
    if usage_recorder is not None and call_id is not None:
        response["usage"] = await usage_recorder.finish(call_id, accumulator.usage)
    return response


def _budget_exhausted(budget: asyncio.Timeout) -> bool:
    """A buffered stream can reach the deadline without yielding to its timer."""
    deadline = budget.when()
    return budget.expired() or (
        deadline is not None and asyncio.get_running_loop().time() >= deadline
    )
