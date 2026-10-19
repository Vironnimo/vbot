"""Consume one Provider stream and finalize visible output and incomplete Tool turns."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.chat._run_state import (
    _AssistantStep,
)
from core.chat._step_outcomes import (
    OUTPUT_INTEGRITY_RECOVERY_NOTE,
    TOOL_CALLS_STREAM_RECOVERY_NOTE,
    _with_assistant_output_files,
)
from core.chat._stream_events import _StreamingRunDeltaEmitter
from core.chat.events import (
    _emit_streaming_assistant_events,
    _exception_to_error_kind,
)
from core.chat.messages import InterruptionCause, JsonObject
from core.chat.recovery import (
    IncompleteResponseError,
    normalize_interruption_cause,
)
from core.chat.streaming import (
    STREAM_PROGRESS_TIMEOUT_SECONDS,
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    StreamingProgressTimeoutError,
    StreamRecoveryAction,
    decide_stream_recovery,
    iter_with_chunk_timeout,
)
from core.chat.wire_shaping import (
    _assistant_message_from_response,
)
from core.performance import record_span, session_track
from core.providers.adapter import (
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    TERMINAL_OUTCOME_STOP,
    TERMINAL_OUTCOME_TOOL_CALLS,
    TerminalOutcome,
)
from core.providers.errors import (
    NetworkError,
    ProviderError,
)
from core.runs import (
    PROVIDER_HEARTBEAT_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    RunInterruptedError,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._stream_draft import StreamDraft
    from core.runs import Run


_LOGGER = get_logger("chat")

ToolCallSink = Callable[[list[JsonObject]], None]
"""Receives complete normalized Tool Calls while the Model streams."""


def _record_first_token(run: Run, started: float) -> None:
    """Measure one request attempt until its first Model output (``provider.first_token``)."""
    record_span(
        "provider.first_token",
        started,
        track=session_track(run.agent_id, run.session_id, run.project_id),
        name="first token",
        args={"run_id": run.id},
    )


def _needs_visible_answer_recovery(
    terminal_outcome: TerminalOutcome | None,
    *,
    content: object,
    reasoning: str | None,
    tool_calls: object,
    ended_in_reasoning: bool,
) -> bool:
    """Detect a successful-looking step that ended before a visible answer."""
    if tool_calls:
        return False
    if terminal_outcome == TERMINAL_OUTCOME_OUTPUT_TRUNCATED:
        return True
    if terminal_outcome != TERMINAL_OUTCOME_STOP or not reasoning:
        return False
    has_visible_content = bool(content.strip()) if isinstance(content, str) else bool(content)
    return not has_visible_content or ended_in_reasoning


def _check_empty_response(response: JsonObject, outcome: TerminalOutcome | None) -> None:
    if outcome not in {TERMINAL_OUTCOME_STOP, TERMINAL_OUTCOME_OUTPUT_TRUNCATED}:
        return
    content = response.get("content")
    reasoning = response.get("reasoning")
    if (
        not (content.strip() if isinstance(content, str) else content)
        and not (reasoning.strip() if isinstance(reasoning, str) else reasoning)
        and not response.get("tool_calls")
    ):
        raise IncompleteResponseError("The Model returned no answer or Tool Calls")


class _StreamRestartNeeded(Exception):  # noqa: N818 — control-flow signal, not an error
    """Request another budgeted attempt before answer text has escaped."""

    def __init__(self, cause: Exception, *, non_streaming: bool = False) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.non_streaming = non_streaming


async def consume_stream_response(
    agent: Any,
    adapter: Any,
    model_id: str,
    response_model: str,
    messages: list[JsonObject],
    tools: list[JsonObject],
    run: Run,
    accumulator: StreamingAccumulator,
    *,
    public_model: str,
    can_restart: bool,
    output_cwd: Path | None,
    chunk_timeout_seconds: float | None,
    request_context: dict[str, Any] | None,
    stream_draft: StreamDraft | None,
    temperature: float | None,
    top_p: float | None,
    fallback_follows: bool,
    recovery_deadline: float | None,
    message_id: str | None,
    on_tool_calls: ToolCallSink | None,
) -> _AssistantStep:
    delta_emitter = _StreamingRunDeltaEmitter(run)
    attempt_started = time.perf_counter()
    awaiting_first_delta = True
    stream = adapter.stream(
        messages,
        model_id=model_id,
        temperature=temperature,
        top_p=top_p,
        tools=tools,
        **(request_context or {}),
    )

    last_model_delta_at = time.monotonic()
    preserve_complete_after_cancel = False
    try:
        async for delta in iter_with_chunk_timeout(
            stream,
            timeout_seconds=chunk_timeout_seconds,
            deadline=recovery_deadline,
            progress_timeout_seconds=(
                STREAM_PROGRESS_TIMEOUT_SECONDS if chunk_timeout_seconds is not None else None
            ),
        ):
            run.raise_if_cancelled()
            if delta.get("type") == "heartbeat":
                delta_emitter.flush()
                run.emit(
                    PROVIDER_HEARTBEAT_EVENT,
                    {
                        "idle_seconds": round(time.monotonic() - last_model_delta_at, 1),
                        "state": "waiting_for_model_delta",
                    },
                )
                continue
            if awaiting_first_delta:
                awaiting_first_delta = False
                _record_first_token(run, attempt_started)
            last_model_delta_at = time.monotonic()
            visible_deltas = accumulator.add_delta(delta)
            for visible_delta in visible_deltas:
                if stream_draft is not None:
                    stream_draft.record(
                        model=public_model,
                        reasoning=str(visible_delta.payload.get("reasoning_delta", "")),
                        content=str(visible_delta.payload.get("content_delta", "")),
                    )
                delta_emitter.add(visible_delta)
            if on_tool_calls is not None and accumulator.finish_reason is None:
                # After the finish, Chat decides on the remaining calls
                # with the terminal outcome in hand.
                completed = accumulator.take_completed_tool_calls()
                if completed:
                    # Every fragment of these calls reaches the Run first.
                    delta_emitter.flush()
                    on_tool_calls(completed)
            run.raise_if_cancelled()
        delta_emitter.flush()
        if accumulator.finish_reason is None:
            raise NetworkError("Provider stream ended without finish delta")
        assistant_fields = accumulator.finalize_assistant_fields()
        _check_empty_response(assistant_fields.to_response_dict(), assistant_fields.finish_reason)
    except (
        ProviderError,
        NetworkError,
        StreamingChunkTimeoutError,
        StreamingProgressTimeoutError,
    ) as exc:
        delta_emitter.flush()
        tools_started = accumulator.has_taken_tool_calls
        # One provider-agnostic owner decides what a stream break means; the
        # action stays here (the chat loop owns side effects, not the policy).
        action = decide_stream_recovery(
            exc,
            can_restart=can_restart,
            has_partial_content=bool((accumulator.partial_content or "").strip()),
            finish_received=(
                accumulator.finish_reason is not None
                and not isinstance(exc, IncompleteResponseError)
            ),
            fallback_follows=fallback_follows,
            tools_started=tools_started,
        )
        if action is StreamRecoveryAction.ACCEPT_COMPLETE:
            # The Run keeps the completed response; its terminal line reports the outcome.
            _LOGGER.debug(
                "Provider stream transport failed after a finish delta; accepted the "
                "completed response (run=%s model=%s error_type=%s error=%s)",
                run.id,
                model_id,
                type(exc).__name__,
                exc,
            )
            assistant_fields = accumulator.finalize_assistant_fields()
        elif action is StreamRecoveryAction.FALLBACK:
            if accumulator.partial_reasoning is not None or accumulator.has_partial_tool_call:
                run.emit(STREAM_ATTEMPT_RESTARTED_EVENT)
            raise _StreamRestartNeeded(exc, non_streaming=True) from exc
        elif action is StreamRecoveryAction.RESTART:
            run.emit(STREAM_ATTEMPT_RESTARTED_EVENT)
            run.emit(
                PROVIDER_REQUEST_STATUS_EVENT,
                {
                    "state": "retrying",
                    "model": response_model,
                    "error_kind": _exception_to_error_kind(exc),
                },
            )
            raise _StreamRestartNeeded(exc) from exc
        elif tools_started and action in {
            StreamRecoveryAction.PRESERVE_PARTIAL,
            StreamRecoveryAction.FAIL,
        }:
            _LOGGER.debug(
                "Provider stream broke after Tool Calls started; keeping them "
                "(run=%s model=%s error_type=%s error=%s)",
                run.id,
                model_id,
                type(exc).__name__,
                exc,
            )
            step = _finalize_after_started_tools(
                public_model,
                response_model,
                accumulator,
                run,
                message_id=message_id,
                output_cwd=output_cwd,
            )
            if action is StreamRecoveryAction.FAIL:
                return replace(step, failure=exc)
            return replace(
                step,
                recovery="continue",
                recovery_note=TOOL_CALLS_STREAM_RECOVERY_NOTE,
                recovery_error=exc,
            )
        elif action is StreamRecoveryAction.PRESERVE_PARTIAL:
            interruption_cause = normalize_interruption_cause(exc)
            run.stream_recovery_count += 1
            _LOGGER.debug(
                "Provider stream interrupted after visible output; preserving partial "
                "(run=%s model=%s cause=%s error=%s)",
                run.id,
                model_id,
                interruption_cause,
                exc,
            )
            return _finalize_interrupted_partial(
                public_model,
                response_model,
                accumulator,
                run,
                interruption_cause=interruption_cause,
                recovery="continue",
                recovery_error=exc,
                output_cwd=output_cwd,
                message_id=message_id,
            )
        elif action is StreamRecoveryAction.INTERRUPT:
            interruption_cause = normalize_interruption_cause(exc)
            # A Model fallback or the Run's terminal line reports the outcome.
            _LOGGER.debug(
                "Provider stream recovery exhausted before answer text "
                "(run=%s model=%s cause=%s error=%s)",
                run.id,
                model_id,
                interruption_cause,
                exc,
            )
            if accumulator.partial_reasoning is not None:
                return _finalize_interrupted_partial(
                    public_model,
                    response_model,
                    accumulator,
                    run,
                    interruption_cause=interruption_cause,
                    recovery="interrupt",
                    output_cwd=output_cwd,
                    message_id=message_id,
                )
            raise RunInterruptedError(interruption_cause) from exc
        else:
            if not isinstance(exc, IncompleteResponseError) and (
                accumulator.partial_content is not None or accumulator.partial_reasoning is not None
            ):
                return replace(
                    _finalize_interrupted_partial(
                        public_model,
                        response_model,
                        accumulator,
                        run,
                        interruption_cause=normalize_interruption_cause(exc),
                        output_cwd=output_cwd,
                        message_id=message_id,
                    ),
                    failure=exc,
                )
            raise
    except asyncio.CancelledError:
        delta_emitter.close()
        if (
            run.cancel_requested
            and accumulator.finish_reason == TERMINAL_OUTCOME_STOP
            and not accumulator.has_partial_tool_call
            and bool((accumulator.partial_content or "").strip())
        ):
            # Logical completion precedes iterator/transport cleanup. Keep
            # that answer through the ordinary integrity checks below;
            # cancellation still prevents any subsequent Model/Tool work.
            assistant_fields = accumulator.finalize_assistant_fields()
            preserve_complete_after_cancel = True
        elif run.cancel_requested and (
            accumulator.partial_content is not None
            or accumulator.partial_reasoning is not None
            or accumulator.has_taken_tool_calls
        ):
            # Preserve readable evidence of a genuinely unfinished step,
            # including the Tool Calls that already started.
            return _finalize_interrupted_partial(
                public_model,
                response_model,
                accumulator,
                run,
                interruption_cause=("user" if run.cancel_reason == "user" else "internal"),
                output_cwd=output_cwd,
                message_id=message_id,
            )
        else:
            raise
    except Exception as exc:
        if not accumulator.has_taken_tool_calls:
            delta_emitter.close()
            raise
        # Any other failure after Tool Calls started keeps them like a
        # fatal stream break: their turn and Results persist, then the
        # step fails.
        delta_emitter.flush()
        # The Run's terminal line reports the failure itself.
        _LOGGER.debug(
            "Model step failed after Tool Calls started; keeping them "
            "(run=%s model=%s error_type=%s)",
            run.id,
            model_id,
            type(exc).__name__,
        )
        step = _finalize_after_started_tools(
            public_model,
            response_model,
            accumulator,
            run,
            message_id=message_id,
            output_cwd=output_cwd,
        )
        return replace(step, failure=exc)
    except BaseException:
        delta_emitter.close()
        raise

    _check_empty_response(assistant_fields.to_response_dict(), assistant_fields.finish_reason)
    assistant_message = _assistant_message_from_response(
        public_model,
        assistant_fields.to_response_dict(),
        reasoning_scope=response_model,
        reasoning_timing=assistant_fields.reasoning_timing,
        message_id=message_id,
    )
    if _needs_visible_answer_recovery(
        assistant_fields.finish_reason,
        content=assistant_message.content,
        reasoning=assistant_message.reasoning,
        tool_calls=assistant_message.tool_calls,
        ended_in_reasoning=accumulator.ends_with_reasoning,
    ):
        return _finalize_interrupted_partial(
            public_model,
            response_model,
            accumulator,
            run,
            interruption_cause=(
                ("user" if run.cancel_reason == "user" else "internal")
                if run.cancel_requested
                else "provider"
            ),
            recovery="none" if run.cancel_requested else "continue",
            recovery_note=OUTPUT_INTEGRITY_RECOVERY_NOTE,
            output_cwd=output_cwd,
            message_id=message_id,
        )
    assistant_message = _with_assistant_output_files(assistant_message, cwd=output_cwd)
    _emit_streaming_assistant_events(
        run, assistant_message, allow_after_cancel=preserve_complete_after_cancel
    )
    return _AssistantStep(
        message=assistant_message,
        terminal_outcome=assistant_fields.finish_reason,
    )


def _finalize_after_started_tools(
    public_model: str,
    response_model: str,
    accumulator: StreamingAccumulator,
    run: Run,
    *,
    message_id: str | None,
    output_cwd: Path | None,
) -> _AssistantStep:
    """Keep a stream that broke after some of its Tool Calls started.

    Replaying would run those calls again, so the turn keeps the output that
    arrived with exactly the started calls, as a complete Tool-call turn
    whose results the Model continues from. A call still being written was
    never started and is dropped.
    """
    run.stream_recovery_count += 1
    fields = accumulator.finalize_partial_fields()
    assistant_message = _assistant_message_from_response(
        public_model,
        fields.to_response_dict(),
        reasoning_scope=response_model,
        reasoning_timing=fields.reasoning_timing,
        message_id=message_id,
    )
    assistant_message = _with_assistant_output_files(assistant_message, cwd=output_cwd)
    _emit_streaming_assistant_events(run, assistant_message)
    return _AssistantStep(
        message=assistant_message,
        terminal_outcome=TERMINAL_OUTCOME_TOOL_CALLS,
    )


def _finalize_interrupted_partial(
    public_model: str,
    response_model: str,
    accumulator: StreamingAccumulator,
    run: Run,
    *,
    interruption_cause: InterruptionCause,
    output_cwd: Path | None,
    recovery: Literal["none", "continue", "interrupt"] = "none",
    recovery_note: str | None = None,
    recovery_error: Exception | None = None,
    message_id: str | None = None,
) -> _AssistantStep:
    """Preserve a stream broken after visible output as an interrupted turn.

    The visible answer streamed so far is finalized into an assistant message
    flagged ``interrupted`` (no finish reason; any in-flight tool call is
    dropped — it was never executed, so dropping it is side-effect-free).
    Tool Calls that already started stay with the turn (only a cancelled
    Run ends with them; any other break keeps them through
    :meth:`_finalize_after_started_tools`).
    A provider break after answer text asks the progression loop for a fresh
    continuation request in the same Run. Exhausted replay may instead ask
    the loop to terminate after preserving readable Reasoning. Both paths
    drop any in-flight Tool Call because it was never complete or executed.

    Also the finalize path for a user cancel after visible output: there the
    run ends as *cancelled*, and ``allow_after_cancel`` lets the settled
    assistant-output event through the cancel suppression — it re-publishes
    text the user already saw streaming, not a late result.
    """
    partial_fields = accumulator.finalize_partial_fields()
    assistant_message = _assistant_message_from_response(
        public_model,
        partial_fields.to_response_dict(),
        reasoning_scope=response_model,
        reasoning_timing=partial_fields.reasoning_timing,
        interrupted=True,
        interruption_cause=interruption_cause,
        message_id=message_id,
    )
    assistant_message = _with_assistant_output_files(assistant_message, cwd=output_cwd)
    _emit_streaming_assistant_events(run, assistant_message, allow_after_cancel=True)
    return _AssistantStep(
        message=assistant_message,
        terminal_outcome=None,
        recovery=recovery,
        recovery_note=recovery_note,
        recovery_error=recovery_error,
    )
