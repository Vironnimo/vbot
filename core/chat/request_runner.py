"""Wire-request and stream execution for one Model step of a ChatLoop."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from core.chat._boundaries import _finish_visible_boundary
from core.chat._run_state import (
    ChatLoopDependencies,
    _AssistantStep,
)
from core.chat._step_outcomes import (
    OUTPUT_INTEGRITY_RECOVERY_NOTE,
    _with_assistant_output_files,
)
from core.chat._stream_response import ToolCallSink as ToolCallSink
from core.chat._stream_response import (
    _check_empty_response,
    _needs_visible_answer_recovery,
    _record_first_token,
    _StreamRestartNeeded,
    consume_stream_response,
)
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.events import (
    _emit_assistant_events,
    _exception_to_error_kind,
)
from core.chat.messages import JsonObject
from core.chat.output_files import OutputFileScope
from core.chat.recovery import (
    IncompleteResponseError,
    RecoveryBudget,
)
from core.chat.streaming import (
    STREAM_CHUNK_TIMEOUT_SECONDS,
    StreamingAccumulator,
    finalize_model_response,
)
from core.chat.wire_shaping import (
    _assistant_message_from_response,
    model_facing_request,
    route_effort,
)
from core.debug import timing
from core.providers.adapter import (
    TERMINAL_OUTCOME_OUTPUT_TRUNCATED,
    terminal_outcome_from_response,
)
from core.providers.errors import (
    NetworkError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.runs import (
    PROVIDER_REQUEST_STATUS_EVENT,
)
from core.utils.logging import get_logger
from core.utils.retry import RetryNotice, caller_owns_retries

if TYPE_CHECKING:
    from core.chat._effort_epoch import EffortState
    from core.chat._stream_draft import StreamDraft
    from core.runs import Run


_LOGGER = get_logger("chat")


def _normalize_non_streaming_step(
    normalized: JsonObject,
    *,
    model_id: str,
    response_model: str,
    public_model: str,
    message_id: str | None = None,
) -> _AssistantStep:
    """Normalize one Provider response and build its canonical Assistant step."""
    normalized = finalize_model_response(normalized)
    terminal_outcome = terminal_outcome_from_response(normalized)
    _check_empty_response(normalized, terminal_outcome)
    message = _assistant_message_from_response(
        public_model,
        normalized,
        reasoning_scope=response_model,
        message_id=message_id,
    )
    if _needs_visible_answer_recovery(
        terminal_outcome,
        content=message.content,
        reasoning=message.reasoning,
        tool_calls=message.tool_calls,
        ended_in_reasoning=False,
    ):
        _LOGGER.debug(
            "Provider ended a non-streaming response without a complete answer; "
            "requesting a continuation (model=%s)",
            model_id,
        )
        return _AssistantStep(
            message=replace(
                message,
                interrupted=True,
                interruption_cause="provider",
            ),
            terminal_outcome=None,
            recovery="continue",
            recovery_note=OUTPUT_INTEGRITY_RECOVERY_NOTE,
        )
    return _AssistantStep(
        message=message,
        terminal_outcome=terminal_outcome,
    )


def _resolve_request_context_kwargs(
    adapter: Any,
    run: Run,
    prompt_cache_affinity_id: str,
) -> dict[str, Any]:
    """Resolve per-request conversation-context kwargs for one request build."""
    return dict(
        adapter.request_context_kwargs(
            agent_id=run.agent_id,
            session_id=run.session_id,
            project_id=run.project_id,
            prompt_cache_affinity_id=prompt_cache_affinity_id,
        )
    )


class WireRequestRunner:
    """Sends one ChatLoop Model step over the wire and recovers its stream.

    Every request streams. Only a Provider that declares a request cannot
    stream (``ProviderStreamingUnsupportedError`` before any output) is asked
    for a completed response instead; no other path sends without streaming.

    Stateless besides its injected dependencies: the resolved target, tools, and
    Run travel through the arguments, and the loop's collaborators are reached
    through the injected dependencies object.
    """

    def __init__(
        self,
        *,
        dependencies: ChatLoopDependencies,
    ) -> None:
        self._dependencies = dependencies

    async def send_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        response_model: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        prompt_cache_affinity_id: str,
        output_scope: OutputFileScope,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        stream_draft: StreamDraft | None = None,
        *,
        public_model: str,
        provider_id: str = "",
        recovery: RecoveryBudget | None = None,
        message_id: str | None = None,
        on_tool_calls: ToolCallSink | None = None,
        effort: EffortState | None = None,
    ) -> _AssistantStep:
        """Send one Model step; its Assistant messages name ``public_model``.

        ``public_model`` is the user-facing Model string of the answering route:
        the Agent's primary Model, or the fallback candidate serving this Run.
        ``message_id`` names the Assistant turn this step builds. ``on_tool_calls``
        receives each Tool Call as soon as the stream has moved past it, in
        order; calls that arrive only with the end of the response are not
        passed. Once a call was passed, the stream is never replayed: a break
        keeps the output so far with exactly the passed calls (see
        :data:`TOOL_CALLS_STREAM_RECOVERY_NOTE`). ``effort`` is the Session's
        thinking effort state; without it the request uses the Agent's effort.
        """
        messages, effort_kwargs = route_effort(
            effort, messages, adapter=adapter, model_id=model_id, fallback=agent.thinking_effort
        )
        # The projection reads every request text, so it stays off the Event Loop.
        messages, tools = await _CHAT_TRANSFORM_WORKERS.run(
            model_facing_request, messages, tools, self._dependencies.secret_masker
        )
        request_context = {
            **_resolve_request_context_kwargs(adapter, run, prompt_cache_affinity_id),
            **effort_kwargs,
        }
        # Sampling reaches the wire only when configured; otherwise the
        # Provider's own default applies.
        temperature = agent.temperature
        top_p = agent.top_p

        def retry_notice(notice: RetryNotice) -> None:
            run.emit(
                PROVIDER_REQUEST_STATUS_EVENT,
                {
                    "state": "retrying" if notice.waiting else "waiting",
                    "model": response_model,
                    "attempt": notice.attempt,
                    "max_attempts": notice.max_attempts,
                    "delay_seconds": notice.delay_seconds,
                    "error_kind": _exception_to_error_kind(notice.error),
                },
            )
            if notice.waiting:
                # Each attempt is DEBUG; the Run's terminal line reports the count.
                run.retry_count += 1
                _LOGGER.debug(
                    "Provider request retry scheduled (run=%s model=%s attempt=%d/%d cause=%s)",
                    run.id,
                    response_model,
                    notice.attempt,
                    notice.max_attempts,
                    type(notice.error).__name__,
                )

        run.emit(PROVIDER_REQUEST_STATUS_EVENT, {"state": "waiting", "model": response_model})
        budget = recovery if recovery is not None else RecoveryBudget()
        use_streaming = True
        try:
            while True:
                run.raise_if_cancelled()
                await budget.begin(response_model, retry_notice)
                try:
                    # One Chat budget covers establishment and open-stream
                    # recovery. Standalone Adapter consumers keep their retries.
                    with caller_owns_retries():
                        if use_streaming:
                            step = await self._consume_stream_attempt(
                                agent,
                                adapter,
                                model_id,
                                response_model,
                                messages,
                                tools,
                                run,
                                public_model=public_model,
                                can_restart=budget.available(response_model),
                                chunk_timeout_seconds=chunk_timeout_seconds,
                                request_context=request_context,
                                stream_draft=stream_draft,
                                output_scope=output_scope,
                                temperature=temperature,
                                top_p=top_p,
                                fallback_follows=budget.patience(response_model).fallback_follows,
                                recovery_deadline=budget.deadline(response_model),
                                message_id=message_id,
                                on_tool_calls=on_tool_calls,
                            )
                        else:
                            deadline = budget.deadline(response_model)
                            remaining = (
                                max(0.0, deadline - budget.clock())
                                if deadline is not None
                                else None
                            )
                            try:
                                async with asyncio.timeout(remaining):
                                    step = await self._send_non_streaming_assistant_request(
                                        agent,
                                        adapter,
                                        model_id,
                                        response_model,
                                        messages,
                                        tools,
                                        run,
                                        public_model=public_model,
                                        request_context=request_context,
                                        temperature=temperature,
                                        top_p=top_p,
                                        message_id=message_id,
                                    )
                            except TimeoutError as exc:
                                raise ProviderTimeoutError(
                                    "Model recovery time budget exhausted"
                                ) from exc
                            step = replace(
                                step,
                                message=_with_assistant_output_files(
                                    step.message,
                                    scope=output_scope,
                                ),
                            )
                            _emit_assistant_events(run, step.message)
                except _StreamRestartNeeded as restart:
                    if restart.non_streaming:
                        use_streaming = False
                    else:
                        budget.failed(restart.cause, response_model)
                    continue
                except (ProviderError, NetworkError) as exc:
                    if getattr(exc, "retryable", False):
                        budget.failed(exc, response_model)
                    if (
                        (not use_streaming or isinstance(exc, IncompleteResponseError))
                        and getattr(exc, "retryable", False)
                        and not (
                            isinstance(exc, ProviderRateLimitError)
                            and budget.patience(response_model).fallback_follows
                        )
                        and budget.available(response_model)
                    ):
                        continue
                    if isinstance(exc, NetworkError):
                        raise budget.exhausted() from exc
                    raise
                if (
                    step.recovery == "continue"
                    or step.terminal_outcome == TERMINAL_OUTCOME_OUTPUT_TRUNCATED
                ):
                    # A stream break carries its real error (including Retry-After)
                    # separately from a fatal error to raise after persistence.
                    budget.failed(step.recovery_error or IncompleteResponseError(), response_model)
                elif not step.message.interrupted:
                    # A complete response ends this Model step. The next request
                    # starts a new step without its attempts, deadline or backoff.
                    budget.complete(response_model)
                return step
        finally:
            run.emit(PROVIDER_REQUEST_STATUS_EVENT, {"state": "finished"})

    async def _send_non_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        response_model: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        *,
        public_model: str,
        request_context: dict[str, Any],
        temperature: float | None,
        top_p: float | None,
        message_id: str | None,
    ) -> _AssistantStep:
        """Ask for a completed response: only after the Provider refused to stream."""
        send_started = time.perf_counter()
        recorder = self._dependencies.usage_recorder
        call_id = await self._start_usage_call(public_model, run)
        normalized: JsonObject | None = None
        try:
            with timing.request_scope(
                run=run.id, iteration=run.iteration_count + 1, model=public_model
            ):
                response = await adapter.send(
                    messages,
                    model_id=model_id,
                    temperature=temperature,
                    top_p=top_p,
                    tools=tools,
                    **request_context,
                )
                # Without streaming, the first output arrives with the whole response.
                _record_first_token(run, send_started)
                normalized = await _CHAT_TRANSFORM_WORKERS.run(
                    adapter.normalize_response, response, model_id=model_id
                )
                step = await _CHAT_TRANSFORM_WORKERS.run(
                    _normalize_non_streaming_step,
                    normalized,
                    model_id=model_id,
                    response_model=response_model,
                    public_model=public_model,
                    message_id=message_id,
                )
                for index, tool_call in enumerate(step.message.tool_calls or []):
                    timing.mark(
                        "call_finalized",
                        call=tool_call.id,
                        index=index,
                        mode="non_streaming",
                        terminal_outcome=step.terminal_outcome,
                    )
        except BaseException as exc:
            if recorder is not None and call_id is not None:
                await recorder.finish(
                    call_id,
                    normalized.get("usage") if normalized is not None else None,
                    status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    failure=exc,
                )
            raise
        if recorder is not None and call_id is not None:
            usage = await recorder.finish(
                call_id,
                step.message.usage,
                status="failed" if step.message.interrupted else "completed",
                failure="incomplete",
            )
            step = replace(step, message=replace(step.message, usage=usage))
        return step

    async def _start_usage_call(self, public_model: str, run: Run) -> str | None:
        recorder = self._dependencies.usage_recorder
        if recorder is None:
            return None
        return await recorder.start(
            model=public_model,
            kind="chat",
            agent_id=run.agent_id,
            session_id=run.session_id,
            project_id=run.project_id,
            run_id=run.id,
            owner_name=run.execution_owner.extension if run.execution_owner is not None else None,
            group_id=run.execution_owner.group_id if run.execution_owner is not None else None,
        )

    async def _consume_stream_attempt(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        response_model: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        *,
        public_model: str,
        can_restart: bool,
        output_scope: OutputFileScope,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        request_context: dict[str, Any] | None = None,
        stream_draft: StreamDraft | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        fallback_follows: bool = False,
        recovery_deadline: float | None = None,
        message_id: str | None = None,
        on_tool_calls: ToolCallSink | None = None,
    ) -> _AssistantStep:
        accumulator = StreamingAccumulator()
        recorder = self._dependencies.usage_recorder
        call_id = await self._start_usage_call(public_model, run)
        try:
            with timing.request_scope(
                run=run.id, iteration=run.iteration_count + 1, model=public_model
            ):
                step = await consume_stream_response(
                    agent,
                    adapter,
                    model_id,
                    response_model,
                    messages,
                    tools,
                    run,
                    accumulator,
                    public_model=public_model,
                    can_restart=can_restart,
                    output_scope=output_scope,
                    chunk_timeout_seconds=chunk_timeout_seconds,
                    request_context=request_context,
                    stream_draft=stream_draft,
                    temperature=temperature,
                    top_p=top_p,
                    fallback_follows=fallback_follows,
                    recovery_deadline=recovery_deadline,
                    message_id=message_id,
                    on_tool_calls=on_tool_calls,
                )
        except BaseException as exc:
            if recorder is not None and call_id is not None:
                await recorder.finish(
                    call_id,
                    accumulator.usage,
                    status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    failure=exc,
                )
            if stream_draft is not None and isinstance(exc, Exception):
                # The attempt left no Assistant entry: a restart or fallback
                # replaces its output, and a failure ends the Run without it.
                await stream_draft.discard()
            raise
        if recorder is not None and call_id is not None:
            status = (
                "cancelled"
                if run.cancel_requested
                else "failed"
                if step.message.interrupted
                or step.failure is not None
                or step.recovery_error is not None
                else "completed"
            )
            usage = await _finish_visible_boundary(
                recorder.finish(
                    call_id,
                    step.message.usage,
                    status=status,
                    # A stream break without an error left the step unfinished.
                    failure=step.failure or step.recovery_error or "incomplete",
                ),
                run,
                step.message.interrupted
                # Started Tool Calls may already have effects; their turn
                # must reach the Session.
                or accumulator.has_taken_tool_calls
                or (
                    not step.message.tool_calls
                    and bool(step.message.content or step.message.reasoning)
                ),
            )
            step = replace(step, message=replace(step.message, usage=usage))
        return step
