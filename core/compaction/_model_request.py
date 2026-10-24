"""Compaction Model invocation, acceptance, and durable usage boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from core.chat.messages import usage_token_is_estimated
from core.chat.streaming import (
    STREAM_PROGRESS_TIMEOUT_SECONDS,
    StreamingAccumulator,
    iter_with_chunk_timeout,
    stream_stall_timeout,
)
from core.compaction._estimates import _model_request_input_budget, _ModelRequestInput
from core.compaction._overflow import (
    OVERFLOW_TARGET_RATIO,
    OVERFLOW_TOOL_CHARACTERS,
    OverflowProjection,
    smaller_compaction_request,
)
from core.compaction._workers import COMPACTION_WORKERS
from core.compaction.errors import CompactionIncompleteError, CompactionLimitError
from core.providers.adapter import (
    TERMINAL_OUTCOME_STOP,
    estimate_wire_request_input_tokens,
    request_input_budget,
)
from core.providers.errors import ProviderContextOverflowError
from core.providers.providers import RequestInput
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
    input_estimate: int | None = None,
    estimate_factor: float = 1.0,
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
    backoff. Confirmed Context rejection permits at most three smaller views,
    reducing Tool observations before dropping older complete exchanges.
    Acceptance failures and cancellation are never retried. Every
    attempt records its own Usage. One absolute deadline covers all attempts
    and their backoff, even while the Model keeps producing deltas.

    ``input_estimate`` is the uncorrected selected-wire estimate of exactly
    ``messages`` and their Tools. Every attempt whose Provider measures its
    input, whatever its outcome, teaches it with that measurement to the input
    estimate calibration of ``model_reference``.
    """
    model = model_reference or str(request_options.get("model_id") or "unknown")
    model_id = str(request_options.get("model_id") or model)
    original_messages = messages
    original_estimate = input_estimate
    recovery_budget: RequestInput | None = None
    reduction = 0
    omitted_exchanges = 0
    budget = asyncio.timeout(MODEL_REQUEST_TIMEOUT_SECONDS)
    try:
        async with budget:
            transient_retries = 0
            while True:
                if _budget_exhausted(budget):
                    raise CompactionLimitError(_TIME_LIMIT_MESSAGE)
                try:
                    scope = (
                        request_input_budget(model_id, recovery_budget)
                        if recovery_budget is not None
                        else nullcontext()
                    )
                    with scope:
                        response = await _stream_attempt(
                            adapter,
                            messages,
                            request_options,
                            budget=budget,
                            model=model,
                            usage_recorder=usage_recorder,
                            calibration=(
                                (model_reference, input_estimate)
                                if model_reference is not None and input_estimate is not None
                                else None
                            ),
                            session_address=session_address,
                            run_id=run_id,
                            owner_name=owner_name,
                            group_id=group_id,
                        )
                    if _budget_exhausted(budget):
                        raise CompactionLimitError(_TIME_LIMIT_MESSAGE)
                    if omitted_exchanges:
                        response["_compaction_omitted_exchanges"] = omitted_exchanges
                    return response
                except ProviderContextOverflowError:

                    def reduce_request(
                        characters: int, omit_old_exchanges: bool
                    ) -> tuple[OverflowProjection, _ModelRequestInput, bool]:
                        def estimate(request: Sequence[dict[str, Any]]) -> int:
                            return estimate_wire_request_input_tokens(
                                adapter,
                                request,
                                model_id=model_id,
                                tools=request_options.get("tools"),
                            )

                        initial = (
                            original_estimate
                            if original_estimate is not None
                            else estimate(original_messages)
                        )
                        projected = smaller_compaction_request(
                            original_messages,
                            tool_characters=characters,
                            target_tokens=round(initial * OVERFLOW_TARGET_RATIO),
                            omit_old_exchanges=omit_old_exchanges,
                            estimate=estimate,
                        )
                        counted = _model_request_input_budget(
                            adapter,
                            model_id,
                            projected.messages,
                            request_options.get("tools"),
                            factor=estimate_factor,
                        )
                        return (
                            projected,
                            counted,
                            _fits_reported_capacity(
                                adapter, model_id, request_options, counted.budget
                            ),
                        )

                    while reduction < len(OVERFLOW_TOOL_CHARACTERS):
                        characters = OVERFLOW_TOOL_CHARACTERS[reduction]
                        reduction += 1
                        projected, counted, fits = await COMPACTION_WORKERS.run(
                            reduce_request,
                            characters,
                            reduction == len(OVERFLOW_TOOL_CHARACTERS),
                        )
                        previous_estimate = input_estimate
                        if previous_estimate is None:
                            previous_estimate = await COMPACTION_WORKERS.run(
                                estimate_wire_request_input_tokens,
                                adapter,
                                messages,
                                model_id=model_id,
                                tools=request_options.get("tools"),
                            )
                        if (
                            fits
                            and projected.messages != messages
                            and counted.estimated < previous_estimate
                        ):
                            messages = projected.messages
                            input_estimate = counted.estimated
                            recovery_budget = counted.budget
                            omitted_exchanges = projected.omitted_exchanges
                            break
                    else:
                        raise
                except Exception as error:
                    if transient_retries >= MODEL_REQUEST_ATTEMPTS - 1 or not getattr(
                        error, "retryable", False
                    ):
                        raise
                    transient_retries += 1
                    hint = getattr(error, "retry_after", None)
                    delay, _ = compute_retry_delay(
                        transient_retries - 1,
                        retry_after=hint if isinstance(hint, (int, float)) else None,
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
        raise CompactionLimitError(_TIME_LIMIT_MESSAGE) from error


def _fits_reported_capacity(
    adapter: Any, model_id: str, options: dict[str, Any], request_input: RequestInput
) -> bool:
    """Skip a rescue view that a known Provider limit already makes impossible.

    Called only after a confirmed remote overflow. The Adapter remains the
    authority for its output allowance; this admission check does not invent a
    window when none was reported, or enable recovery for a local-only failure.
    """
    input_limit = getattr(adapter, "input_token_limit", None)
    limit = input_limit(model_id) if callable(input_limit) else None
    if isinstance(limit, int) and limit > 0 and request_input.tokens > limit:
        return False
    reported = getattr(adapter, "reported_context_window", None)
    window = reported(model_id) if callable(reported) else None
    if not isinstance(window, int) or window <= 0:
        return True
    minimum = getattr(adapter, "minimum_output_tokens", None)
    reserve = (
        minimum(model_id, options.get("thinking_effort"), explicit_limit=options.get("max_tokens"))
        if callable(minimum)
        else 0
    )
    return request_input.tokens + max(1, reserve) <= window


async def _stream_attempt(
    adapter: Any,
    messages: list[dict[str, Any]],
    request_options: dict[str, Any],
    *,
    budget: asyncio.Timeout,
    model: str,
    usage_recorder: UsageRecorder | None,
    calibration: tuple[str, int] | None,
    session_address: SessionAddress | None,
    run_id: str | None,
    owner_name: str | None,
    group_id: str | None,
) -> dict[str, Any]:
    """Run one stream attempt and record its Usage, whatever its outcome.

    *calibration* is the request's Model reference and uncorrected input
    estimate; a measured input teaches it to the input estimate calibration.
    """
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
        chunk_timeout_seconds = stream_stall_timeout(adapter)
        stream_failed = False
        try:
            summary_characters = 0
            async for delta in iter_with_chunk_timeout(
                source,
                timeout_seconds=chunk_timeout_seconds,
                progress_timeout_seconds=(
                    STREAM_PROGRESS_TIMEOUT_SECONDS if chunk_timeout_seconds is not None else None
                ),
            ):
                if _budget_exhausted(budget):
                    raise CompactionLimitError(_TIME_LIMIT_MESSAGE)
                if delta.get("type") != "heartbeat":
                    if delta.get("type") == "content_delta" and isinstance(delta.get("text"), str):
                        summary_characters += len(delta["text"])
                        if summary_characters > MAX_SUMMARY_CHARACTERS:
                            raise CompactionLimitError(
                                f"Compaction exceeded its {MAX_SUMMARY_CHARACTERS:,}-character "
                                "summary limit. No checkpoint was saved."
                            )
                    accumulator.add_delta(delta)
                if chunk_timeout_seconds is None:
                    # A local source can yield buffered deltas without any I/O.
                    # Account for this frame before a Stop interrupts processing.
                    await asyncio.sleep(0)
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
            raise CompactionLimitError(_TIME_LIMIT_MESSAGE)
        if accumulator.finish_reason != TERMINAL_OUTCOME_STOP:
            raise CompactionIncompleteError(
                "Summary stream did not complete successfully "
                f"(outcome={accumulator.finish_reason or 'missing'})"
            )
        if accumulator.has_partial_tool_call:
            raise CompactionIncompleteError(
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
                # The expired time budget cancels a request still in progress.
                failure="limit" if budget.expired() else exc,
            )
            await _learn_input_estimate(usage_recorder, calibration, accumulator.usage)
        raise
    if usage_recorder is not None and call_id is not None:
        response["usage"] = await usage_recorder.finish(call_id, accumulator.usage)
        await _learn_input_estimate(usage_recorder, calibration, accumulator.usage)
    return response


async def _learn_input_estimate(
    usage_recorder: UsageRecorder,
    calibration: tuple[str, int] | None,
    usage: Mapping[str, Any] | None,
) -> None:
    """Teach the calibration one attempt's measured input; estimated input never teaches.

    The recorder ignores unusable samples (``is_usable_sample``).
    """
    if calibration is None or usage is None or usage_token_is_estimated(usage, "input_tokens"):
        return
    measured = usage.get("input_tokens")
    if not isinstance(measured, int) or isinstance(measured, bool):
        return
    model_reference, estimated = calibration
    await COMPACTION_WORKERS.run(
        usage_recorder.record_input_estimate,
        model_reference,
        measured=measured,
        estimated=estimated,
    )


def _budget_exhausted(budget: asyncio.Timeout) -> bool:
    """A buffered stream can reach the deadline without yielding to its timer."""
    deadline = budget.when()
    return budget.expired() or (
        deadline is not None and asyncio.get_running_loop().time() >= deadline
    )
