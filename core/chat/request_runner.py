"""Wire-request and stream execution for one Model step of a ChatLoop."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from core.chat.chat import (
    _CHAT_TRANSFORM_WORKERS,
    ChatLoopDependencies,
    _AssistantStep,
    _with_assistant_output_files,
)
from core.chat.continuation import normalize_interruption_cause
from core.chat.events import _emit_assistant_events, _emit_streaming_assistant_events
from core.chat.messages import JsonObject, _assistant_message_from_response
from core.chat.model_resolution import resolve_request_temperature, resolve_request_top_p
from core.chat.streaming import (
    STREAM_CHUNK_TIMEOUT_SECONDS,
    STREAM_PROGRESS_TIMEOUT_SECONDS,
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    StreamingDeltaBatcher,
    StreamingProgressTimeoutError,
    StreamingVisibleDelta,
    StreamRecoveryAction,
    decide_stream_recovery,
    is_local_provider_base_url,
    iter_with_chunk_timeout,
)
from core.providers.accounts import ConnectionRef
from core.providers.adapter import terminal_outcome_from_response
from core.providers.errors import NetworkError, ProviderError
from core.runs import (
    PROVIDER_HEARTBEAT_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    RunInterruptedError,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.continuation import ContinuationCause, ContinuationTracker
    from core.runs import Run

_LOGGER = get_logger("chat")

# How often a streaming attempt may be restarted from scratch after a transient
# drop that occurred before answer text or a Tool Call fragment. Readable
# Reasoning alone is replay-safe. Each restart re-issues the whole request (the
# adapter's own connect-level retry still applies per attempt), so this bounds
# only the post-connect mid-stream replays.
MAX_STREAM_RESTARTS = 2


def _normalize_non_streaming_step(
    adapter: Any,
    response: JsonObject,
    *,
    model_id: str,
    response_model: str,
    agent_model: str,
) -> _AssistantStep:
    """Normalize one Provider response and build its canonical Assistant step."""
    normalized = adapter.normalize_response(response, model_id=model_id)
    return _AssistantStep(
        message=_assistant_message_from_response(
            agent_model,
            normalized,
            reasoning_scope=response_model,
        ),
        terminal_outcome=terminal_outcome_from_response(normalized),
    )


def _resolve_request_context_kwargs(
    adapter: Any,
    run: Run,
    prompt_cache_affinity_id: str,
) -> dict[str, Any]:
    """Resolve per-request conversation-context kwargs for one request build.

    Mirrors ``_resolve_reasoning_replay_policy``: adapters and test doubles that
    do not expose the hook contribute nothing, so the provider call is unchanged
    for every adapter that has no use for the conversation identity.
    """
    if hasattr(adapter, "request_context_kwargs"):
        return dict(
            adapter.request_context_kwargs(
                agent_id=run.agent_id,
                session_id=run.session_id,
                project_id=run.project_id,
                prompt_cache_affinity_id=prompt_cache_affinity_id,
            )
        )
    return {}


def _connection_local_id(connection: ConnectionRef) -> str | None:
    """Extract the provider-local connection id from a connection reference.

    Returns ``None`` when the reference's compositional id does not carry the
    expected provider prefix, so callers fall back to the provider-level base URL.
    """
    prefix = f"{connection.provider_id}:"
    if not connection.connection_id.startswith(prefix):
        return None
    remainder = connection.connection_id[len(prefix) :]
    return remainder.split(":", 1)[0] or None


class _StreamRestartNeeded(Exception):  # noqa: N818 — control-flow signal, not an error
    """Internal signal: a stream dropped before answer text or a Tool Call.

    Raised by ``_consume_stream_attempt`` and caught by
    ``_send_streaming_assistant_request`` to replay the stream. It never escapes
    the chat loop — the final attempt cannot restart and re-raises the real
    error instead.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _StreamingRunDeltaEmitter:
    """Schedule bounded Run-event batches without delaying a quiet stream."""

    def __init__(self, run: Run) -> None:
        self._run = run
        self._batcher = StreamingDeltaBatcher()
        self._flush_handle: asyncio.TimerHandle | None = None

    def add(self, delta: StreamingVisibleDelta) -> None:
        ready = self._batcher.add(delta)
        if ready:
            self._cancel_flush()
            self._emit(ready)
        if self._batcher.has_pending and self._flush_handle is None:
            delay = self._batcher.seconds_until_flush()
            assert delay is not None
            self._flush_handle = asyncio.get_running_loop().call_later(delay, self._flush_due)

    def flush(self) -> None:
        self._cancel_flush()
        self._emit(self._batcher.flush())

    def close(self) -> None:
        self._cancel_flush()

    def _flush_due(self) -> None:
        self._flush_handle = None
        self._emit(self._batcher.flush(now=time.monotonic()))

    def _cancel_flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None

    def _emit(self, deltas: list[StreamingVisibleDelta]) -> None:
        for delta in deltas:
            self._run.emit(delta.event_type, delta.payload)


class WireRequestRunner:
    """Sends one ChatLoop Model step over the wire and recovers its stream.

    Stateless besides its two injected fields: the resolved target, tools, and
    Run travel through the arguments, and the loop's collaborators are reached
    through the injected dependencies object.
    """

    def __init__(
        self,
        *,
        dependencies: ChatLoopDependencies,
        streaming: bool,
    ) -> None:
        self._dependencies = dependencies
        self._streaming = streaming

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
        output_cwd: Path | None,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        continuation_tracker: ContinuationTracker | None = None,
        *,
        provider_id: str = "",
    ) -> _AssistantStep:
        request_context = _resolve_request_context_kwargs(
            adapter,
            run,
            prompt_cache_affinity_id,
        )
        temperature = resolve_request_temperature(
            agent.temperature,
            self._dependencies.models,
            provider_id,
            model_id,
        )
        top_p = resolve_request_top_p(
            self._dependencies.models,
            provider_id,
            model_id,
        )
        if self._streaming:
            return await self._send_streaming_assistant_request(
                agent,
                adapter,
                model_id,
                response_model,
                messages,
                tools,
                run,
                chunk_timeout_seconds=chunk_timeout_seconds,
                request_context=request_context,
                continuation_tracker=continuation_tracker,
                output_cwd=output_cwd,
                temperature=temperature,
                top_p=top_p,
            )

        return await self._send_non_streaming_assistant_request(
            agent,
            adapter,
            model_id,
            response_model,
            messages,
            tools,
            request_context=request_context,
            temperature=temperature,
            top_p=top_p,
        )

    async def _send_non_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        response_model: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        *,
        request_context: dict[str, Any],
        temperature: float | None,
        top_p: float | None,
    ) -> _AssistantStep:
        response = await adapter.send(
            messages,
            model_id=model_id,
            temperature=temperature,
            top_p=top_p,
            thinking_effort=agent.thinking_effort,
            tools=tools,
            **request_context,
        )
        return await _CHAT_TRANSFORM_WORKERS.run(
            _normalize_non_streaming_step,
            adapter,
            response,
            model_id=model_id,
            response_model=response_model,
            agent_model=agent.model,
        )

    async def _send_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        response_model: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        output_cwd: Path | None,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        request_context: dict[str, Any] | None = None,
        continuation_tracker: ContinuationTracker | None = None,
        *,
        temperature: float | None,
        top_p: float | None,
    ) -> _AssistantStep:
        # A transient drop before answer text is replayed as a full stream
        # restart. Readable Reasoning and any unexecuted Tool Call preview are
        # discarded before replay. Once answer text arrives, partial output is
        # persisted so the progression loop can continue it without duplication.
        for attempt in range(MAX_STREAM_RESTARTS + 1):
            try:
                return await self._consume_stream_attempt(
                    agent,
                    adapter,
                    model_id,
                    response_model,
                    messages,
                    tools,
                    run,
                    can_restart=attempt < MAX_STREAM_RESTARTS,
                    chunk_timeout_seconds=chunk_timeout_seconds,
                    request_context=request_context or {},
                    continuation_tracker=continuation_tracker,
                    output_cwd=output_cwd,
                    temperature=temperature,
                    top_p=top_p,
                )
            except _StreamRestartNeeded as restart:
                _LOGGER.warning(
                    "Streaming attempt %d/%d dropped before answer or Tool Call output "
                    "(%s: %s); restarting stream",
                    attempt + 1,
                    MAX_STREAM_RESTARTS + 1,
                    type(restart.cause).__name__,
                    restart.cause,
                )
        # Unreachable: the final attempt runs with can_restart=False, so it either
        # returns a message or re-raises the underlying error.
        raise AssertionError("stream restart loop exited without returning")

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
        can_restart: bool,
        output_cwd: Path | None,
        chunk_timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
        request_context: dict[str, Any] | None = None,
        continuation_tracker: ContinuationTracker | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> _AssistantStep:
        accumulator = StreamingAccumulator()
        delta_emitter = _StreamingRunDeltaEmitter(run)
        stream = adapter.stream(
            messages,
            model_id=model_id,
            temperature=temperature,
            top_p=top_p,
            thinking_effort=agent.thinking_effort,
            tools=tools,
            **(request_context or {}),
        )

        last_model_delta_at = time.monotonic()
        try:
            async for delta in iter_with_chunk_timeout(
                stream,
                timeout_seconds=chunk_timeout_seconds,
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
                last_model_delta_at = time.monotonic()
                visible_deltas = accumulator.add_delta(delta)
                for visible_delta in visible_deltas:
                    if continuation_tracker is not None:
                        continuation_tracker.record_stream_delta(
                            reasoning=str(visible_delta.payload.get("reasoning_delta", "")),
                            content=str(visible_delta.payload.get("content_delta", "")),
                        )
                    delta_emitter.add(visible_delta)
                run.raise_if_cancelled()
            delta_emitter.flush()
            if accumulator.finish_reason is None:
                raise NetworkError("Provider stream ended without finish delta")
            assistant_fields = accumulator.finalize_assistant_fields()
        except (
            ProviderError,
            NetworkError,
            StreamingChunkTimeoutError,
            StreamingProgressTimeoutError,
        ) as exc:
            delta_emitter.flush()
            # One provider-agnostic owner decides what a stream break means; the
            # action stays here (the chat loop owns side effects, not the policy).
            action = decide_stream_recovery(
                exc,
                can_restart=can_restart,
                has_partial_content=accumulator.partial_content is not None,
                finish_received=accumulator.finish_reason is not None,
            )
            if action is StreamRecoveryAction.ACCEPT_COMPLETE:
                _LOGGER.warning(
                    "Provider stream transport failed after a finish delta; "
                    "accepting the completed response (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
                assistant_fields = accumulator.finalize_assistant_fields()
            elif action is StreamRecoveryAction.FALLBACK:
                assistant_step = await self._send_non_streaming_assistant_request(
                    agent,
                    adapter,
                    model_id,
                    response_model,
                    messages,
                    tools,
                    request_context=request_context or {},
                    temperature=temperature,
                    top_p=top_p,
                )
                assistant_step = replace(
                    assistant_step,
                    message=_with_assistant_output_files(
                        assistant_step.message,
                        cwd=output_cwd,
                    ),
                )
                _emit_assistant_events(run, assistant_step.message)
                return assistant_step
            elif action is StreamRecoveryAction.RESTART:
                if continuation_tracker is not None:
                    await continuation_tracker.discard_stream_attempt()
                run.emit(STREAM_ATTEMPT_RESTARTED_EVENT)
                raise _StreamRestartNeeded(exc) from exc
            elif action is StreamRecoveryAction.PRESERVE_PARTIAL:
                interruption_cause = normalize_interruption_cause(exc)
                if continuation_tracker is not None:
                    continuation_tracker.mark_interruption_cause(interruption_cause)
                _LOGGER.warning(
                    "Provider stream interrupted after visible output; preserving partial "
                    "(run=%s model=%s cause=%s error=%s)",
                    run.id,
                    model_id,
                    interruption_cause,
                    exc,
                )
                return self._finalize_interrupted_partial(
                    agent,
                    response_model,
                    accumulator,
                    run,
                    interruption_cause=interruption_cause,
                    recovery="continue",
                    output_cwd=output_cwd,
                )
            elif action is StreamRecoveryAction.INTERRUPT:
                interruption_cause = normalize_interruption_cause(exc)
                if continuation_tracker is not None:
                    continuation_tracker.mark_interruption_cause(interruption_cause)
                _LOGGER.warning(
                    "Provider stream recovery exhausted before answer text "
                    "(run=%s model=%s cause=%s error=%s)",
                    run.id,
                    model_id,
                    interruption_cause,
                    exc,
                )
                if accumulator.partial_reasoning is not None:
                    return self._finalize_interrupted_partial(
                        agent,
                        response_model,
                        accumulator,
                        run,
                        interruption_cause=interruption_cause,
                        recovery="interrupt",
                        output_cwd=output_cwd,
                    )
                raise RunInterruptedError(interruption_cause) from exc
            else:
                raise
        except asyncio.CancelledError:
            delta_emitter.close()
            # User cancel mid-stream. Output the user already saw must not
            # vanish (GLOSSARY → Cancel), so accumulated visible content is
            # finalized like a stream break after visible output; the caller
            # persists it and the Run still ends as cancelled. This includes
            # readable reasoning even when answer text has not started yet;
            # the private Continuation Checkpoint separately preserves the
            # same working state for the next visible Run.
            if run.cancel_requested and (
                accumulator.partial_content is not None or accumulator.partial_reasoning is not None
            ):
                return self._finalize_interrupted_partial(
                    agent,
                    response_model,
                    accumulator,
                    run,
                    interruption_cause=("user" if run.cancel_reason == "user" else "internal"),
                    output_cwd=output_cwd,
                )
            raise
        except BaseException:
            delta_emitter.close()
            raise

        assistant_message = _assistant_message_from_response(
            agent.model,
            assistant_fields.to_response_dict(),
            reasoning_scope=response_model,
            reasoning_timing=assistant_fields.reasoning_timing,
        )
        assistant_message = _with_assistant_output_files(assistant_message, cwd=output_cwd)
        _emit_streaming_assistant_events(run, assistant_message)
        return _AssistantStep(
            message=assistant_message,
            terminal_outcome=assistant_fields.finish_reason,
        )

    def _finalize_interrupted_partial(
        self,
        agent: Any,
        response_model: str,
        accumulator: StreamingAccumulator,
        run: Run,
        *,
        interruption_cause: ContinuationCause,
        output_cwd: Path | None,
        recovery: Literal["none", "continue", "interrupt"] = "none",
    ) -> _AssistantStep:
        """Preserve a stream broken after visible output as an interrupted turn.

        The visible answer streamed so far is finalized into an assistant message
        flagged ``interrupted`` (no finish reason; any in-flight tool call is
        dropped — it was never executed, so dropping it is side-effect-free).
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
            agent.model,
            partial_fields.to_response_dict(),
            reasoning_scope=response_model,
            reasoning_timing=partial_fields.reasoning_timing,
            interrupted=True,
            interruption_cause=interruption_cause,
        )
        assistant_message = _with_assistant_output_files(assistant_message, cwd=output_cwd)
        _emit_streaming_assistant_events(run, assistant_message, allow_after_cancel=True)
        return _AssistantStep(
            message=assistant_message,
            terminal_outcome=None,
            recovery=recovery,
        )

    def resolve_chunk_timeout(self, connection: ConnectionRef) -> float | None:
        """Return the per-chunk stall timeout for this connection, or None locally.

        Local/loopback inference servers (Ollama, llama.cpp, vLLM) can stay
        silent for minutes during prompt prefill, so the stall guard is disabled
        for them; every remote provider keeps the default timeout. Detection is
        owned by :func:`is_local_provider_base_url` so the policy has one home.
        """
        base_url = self._resolve_connection_base_url(connection)
        if is_local_provider_base_url(base_url):
            return None
        return STREAM_CHUNK_TIMEOUT_SECONDS

    def _resolve_connection_base_url(self, connection: ConnectionRef) -> str | None:
        """Resolve the effective base URL for a provider connection, if known.

        Tolerant of a missing/partial provider registry and of connections
        without their own base URL: returns ``None`` when nothing is resolvable
        (treated as "not local", so the stall guard stays on).
        """
        try:
            provider_config = self._dependencies.providers.get(connection.provider_id)
        except (KeyError, AttributeError):
            return None
        local_id = _connection_local_id(connection)
        get_connection = getattr(provider_config, "get_connection", None)
        if local_id is not None and callable(get_connection):
            try:
                connection_config = get_connection(local_id)
            except KeyError:
                connection_config = None
            connection_base_url = (
                getattr(connection_config, "base_url", None) if connection_config else None
            )
            if isinstance(connection_base_url, str) and connection_base_url:
                return connection_base_url
        provider_base_url = getattr(provider_config, "base_url", None)
        return provider_base_url if isinstance(provider_base_url, str) else None
