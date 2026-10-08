"""Provider-agnostic helpers for chat streaming accumulation."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.providers.adapter import (
    TERMINAL_OUTCOME_UNKNOWN,
    TerminalOutcome,
    normalize_tool_call_candidates,
    terminal_outcome_from_response,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderStreamingUnsupportedError,
)
from core.providers.reasoning import merge_reasoning_meta
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
    RunInterruptedError,
)
from core.tools import registry_tool_name
from core.utils.errors import ProviderError, VBotError
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

JsonObject = dict[str, Any]

_LOGGER = get_logger("chat")

STREAM_CHUNK_TIMEOUT_SECONDS = 180.0
STREAM_PROGRESS_TIMEOUT_SECONDS = 900.0
STREAM_EVENT_EMIT_INTERVAL_SECONDS = 0.04


class StreamingError(VBotError):
    """Base error for provider-agnostic streaming helpers."""


class StreamingDeltaError(StreamingError):
    """Raised when an adapter yields an invalid normalized streaming delta."""


class StreamingChunkTimeoutError(StreamingError):
    """Raised when a provider stream stalls between chunks."""


class StreamingProgressTimeoutError(StreamingError):
    """Raised when heartbeats continue but the Model produces no delta."""


class StreamBrokenAfterToolCallsError(StreamingError):
    """A stream broke after some of its Tool Calls were handed out to run.

    ``response`` holds the output that arrived, with exactly the handed-out
    Tool Calls; a replay would run them twice, so the caller continues from it.
    """

    def __init__(self, cause: Exception, response: JsonObject) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.response = response


class StreamRecoveryAction(Enum):
    """What the chat loop should do when a streaming attempt breaks.

    The single, provider-agnostic vocabulary for stream-break recovery: deciding
    which action applies is :func:`decide_stream_recovery` (here); executing it —
    restarting, falling back to non-streaming, finalizing the partial answer,
    interrupting after bounded recovery, or re-raising — stays in the chat loop.
    """

    ACCEPT_COMPLETE = "accept_complete"
    RESTART = "restart"
    FALLBACK = "fallback"
    PRESERVE_PARTIAL = "preserve_partial"
    INTERRUPT = "interrupt"
    FAIL = "fail"


def decide_stream_recovery(
    error: Exception,
    *,
    can_restart: bool,
    has_partial_content: bool,
    finish_received: bool = False,
    has_fallback_chain: bool = False,
    tools_started: bool = False,
) -> StreamRecoveryAction:
    """Decide how to recover from a broken streaming attempt.

    Provider-agnostic: it reads only normalized vBot errors plus the attempt's
    state, so the same matrix holds for every adapter. Answer text blocks replay
    because a second attempt could duplicate user-visible output. Readable
    Reasoning and an in-flight Tool Call do not: neither has a Tool side effect,
    and both can be discarded before the attempt is retried.

    A normalized finish delta is the provider's logical completion boundary. A
    later transport error therefore cannot turn the completed response back into
    a partial one; the accumulated response is accepted as complete. Before
    answer text a drop can otherwise be replayed cleanly: a
    streaming-unsupported error falls back to a non-streaming request, a
    restartable transient (transport/timeout drop or chunk stall) replays the
    whole stream while restarts remain and interrupts when that budget is
    exhausted; anything else fails. Once answer text has escaped, the stream is
    never replayed and accumulated content is preserved as an interrupted
    Assistant boundary. Transient failures permit same-Run continuation; fatal
    failures are raised after preserving that boundary. Started Tool Calls
    (``tools_started``) block replay the same way: a second attempt would run
    them again.

    A rate-limit failure with a configured model-fallback chain fails
    immediately (advancing the chain) instead of burning same-model restarts:
    quota pressure rarely clears within seconds. Without a chain the shared
    Chat budget and Retry-After backoff apply.
    """
    if finish_received:
        return StreamRecoveryAction.ACCEPT_COMPLETE
    if not has_partial_content and not tools_started:
        if _is_streaming_fallback_error(error):
            return StreamRecoveryAction.FALLBACK
        if _is_stream_restartable_error(error):
            if has_fallback_chain and _is_rate_limit_error(error):
                # A retryable Provider failure still belongs to the Run-local
                # Model fallback policy; rate limits skip straight to it.
                return StreamRecoveryAction.FAIL
            if can_restart:
                return StreamRecoveryAction.RESTART
            # A retryable Provider failure still belongs to the Run-local Model
            # fallback policy once same-Model recovery is exhausted. Transport
            # and timeout failures become interruptions, which can also advance
            # the configured chain before ending the Run.
            if _is_model_fallback_trigger(error):
                return StreamRecoveryAction.FAIL
            return StreamRecoveryAction.INTERRUPT
        return StreamRecoveryAction.FAIL
    if _is_stream_restartable_error(error) or _is_streaming_fallback_error(error):
        return StreamRecoveryAction.PRESERVE_PARTIAL
    return StreamRecoveryAction.FAIL


def _is_streaming_fallback_error(error: Exception) -> bool:
    """Whether a streaming failure should fall back to a non-streaming request.

    Only ``ProviderStreamingUnsupportedError`` qualifies (a provider/model that
    cannot serve this request as a stream at all); the chat loop applies it only
    before any visible delta has been emitted.
    """
    return isinstance(error, ProviderStreamingUnsupportedError)


def _is_stream_restartable_error(error: Exception) -> bool:
    """Whether a streaming failure may be replayed as a fresh stream.

    True for retryable transport/timeout failures (``NetworkError``,
    ``ProviderTimeoutError``, retryable ``ProviderError``) and for a mid-stream
    chunk stall (``StreamingChunkTimeoutError``) — the provider went silent after
    the connect succeeded, which is exactly the transient "not yet visible" case
    the restart was built for (it carries no ``retryable`` attribute, so it is
    matched by type). The chat loop restarts from scratch only before answer
    text has been emitted, so the replay cannot duplicate an answer the user
    already saw — this is the streaming analogue of the
    streaming-to-non-streaming fallback.
    """
    if isinstance(error, (StreamingChunkTimeoutError, StreamingProgressTimeoutError)):
        return True
    return bool(getattr(error, "retryable", False))


def _is_model_fallback_trigger(error: Exception) -> bool:
    """Whether a propagated error should switch the agent to its fallback model.

    Retryable Provider errors propagate directly. Exhausted transport failures
    instead become RunInterruptedError, preserving their terminal cause when no
    configured fallback is available.
    """
    return isinstance(error, ProviderError) and error.retryable


def _is_rate_limit_error(error: Exception) -> bool:
    """Whether the failure is a provider rate limit (HTTP 429 family)."""
    return isinstance(error, ProviderRateLimitError)


# Message markers of a fatal error that is clearly model-scoped: the requested
# model is unknown, deactivated, or not served by this account. Deliberately
# narrow — billing, permission, content-policy, and context-overflow fatals
# must never match, because switching models cannot fix them.
_MODEL_SCOPED_FATAL_MARKERS = (
    "model not found",
    "model_not_found",
    "unknown model",
    "no such model",
    "invalid model",
    "not a valid model",
    "does not exist",
)


def should_advance_model_fallback_chain(error: ProviderError | RunInterruptedError) -> bool:
    """Whether a failed attempt should advance to the next fallback-chain candidate.

    Advances on retryable ``ProviderError`` failures (transient and
    provider-specific), exhausted provider/network/timeout interruptions, and
    fatal errors that are clearly
    model-scoped (404 or unknown-model wording): the binding itself is dead, so
    the next candidate is the only sensible recovery.

    Raw ``NetworkError`` must first exhaust same-Model recovery. A configured
    route may use a different host, so its normalized interruption can advance.
    Never advances on auth failures (account-wide), streaming-unsupported,
    or any other fatal class (billing, permission, context/token limits,
    content policy) where an identical-shaped retry on another model either
    fails again or silently changes the outcome's meaning.
    """
    if isinstance(error, RunInterruptedError):
        return error.cause in {"provider", "network", "timeout"}
    if isinstance(error, (ProviderAuthError, ProviderStreamingUnsupportedError)):
        return False
    if error.retryable:
        return True
    if getattr(error, "status_code", None) == 404:
        return True
    message = str(error).lower()
    return any(marker in message for marker in _MODEL_SCOPED_FATAL_MARKERS)


def stream_stall_timeout(adapter: Any) -> float | None:
    """Return the per-chunk stall timeout for requests through ``adapter``.

    Local inference servers (Ollama, llama.cpp, vLLM) can stay silent for
    minutes during prompt prefill, so the stall guards are off when the
    Adapter's endpoint is local (``ProviderAdapter.local_endpoint``); every
    remote Provider keeps the default timeout. Pass the result as
    ``timeout_seconds`` to :func:`iter_with_chunk_timeout`; ``None`` also turns
    off its Model-progress window.
    """
    if getattr(adapter, "local_endpoint", False) is True:
        return None
    return STREAM_CHUNK_TIMEOUT_SECONDS


@dataclass(frozen=True)
class StreamingVisibleDelta:
    """A public Run/SSE delta event ready for ChatLoop emission."""

    event_type: str
    payload: JsonObject


class StreamingDeltaBatcher:
    """Bound the Run-event rate while preserving visible delta order.

    Provider streams may yield hundreds of tiny fragments per second. Keeping
    every fragment as its own replayable Run event needlessly pressures the
    browser transport and its sequence reducer. The first fragment remains
    immediate; later adjacent fragments are merged and released at the bounded
    cadence or at the next stable stream boundary.
    """

    def __init__(self, interval_seconds: float = STREAM_EVENT_EMIT_INTERVAL_SECONDS) -> None:
        if interval_seconds <= 0:
            raise ValueError("stream event emit interval must be positive")
        self._interval_seconds = interval_seconds
        self._next_emit_at = 0.0
        self._pending: list[StreamingVisibleDelta] = []

    def add(
        self,
        delta: StreamingVisibleDelta,
        *,
        now: float | None = None,
    ) -> list[StreamingVisibleDelta]:
        """Accept one delta and return a batch when the cadence is due."""
        current_time = time.monotonic() if now is None else now
        if self._next_emit_at == 0.0:
            self._next_emit_at = current_time + self._interval_seconds
            return [delta]

        self._append_pending(delta)
        if current_time < self._next_emit_at:
            return []

        self._next_emit_at = current_time + self._interval_seconds
        return self.flush()

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def seconds_until_flush(self, *, now: float | None = None) -> float | None:
        if not self._pending:
            return None
        current_time = time.monotonic() if now is None else now
        return max(0.0, self._next_emit_at - current_time)

    def flush(self, *, now: float | None = None) -> list[StreamingVisibleDelta]:
        """Return and clear every pending delta in its original order."""
        pending = self._pending
        self._pending = []
        if pending and now is not None:
            self._next_emit_at = now + self._interval_seconds
        return pending

    def _append_pending(self, delta: StreamingVisibleDelta) -> None:
        if self._pending:
            merged = _merge_adjacent_visible_deltas(self._pending[-1], delta)
            if merged is not None:
                self._pending[-1] = merged
                return
        self._pending.append(delta)


def _merge_adjacent_visible_deltas(
    existing: StreamingVisibleDelta,
    incoming: StreamingVisibleDelta,
) -> StreamingVisibleDelta | None:
    if existing.event_type != incoming.event_type:
        return None

    if existing.event_type == ASSISTANT_OUTPUT_DELTA_EVENT:
        return _merge_text_visible_delta(existing, incoming, "content_delta")
    if existing.event_type == REASONING_DELTA_EVENT:
        if existing.payload.get("summary_index") != incoming.payload.get("summary_index"):
            return None
        merged = _merge_text_visible_delta(existing, incoming, "reasoning_delta")
        if "summary_index" in existing.payload:
            merged.payload["summary_index"] = existing.payload["summary_index"]
            merged.payload["summary_text"] = (
                existing.payload["summary_text"] + incoming.payload["summary_text"]
            )
        return merged
    if existing.event_type != TOOL_CALL_DELTA_EVENT:
        return None

    existing_id = existing.payload.get("tool_call_id")
    if not isinstance(existing_id, str) or incoming.payload.get("tool_call_id") != existing_id:
        return None
    payload: JsonObject = {"tool_call_id": existing_id}
    for key in ("name_delta", "arguments_delta"):
        combined = f"{existing.payload.get(key, '')}{incoming.payload.get(key, '')}"
        if combined:
            payload[key] = combined
    return StreamingVisibleDelta(event_type=existing.event_type, payload=payload)


def _merge_text_visible_delta(
    existing: StreamingVisibleDelta,
    incoming: StreamingVisibleDelta,
    payload_key: str,
) -> StreamingVisibleDelta:
    return StreamingVisibleDelta(
        event_type=existing.event_type,
        payload={
            payload_key: f"{existing.payload.get(payload_key, '')}"
            f"{incoming.payload.get(payload_key, '')}",
        },
    )


@dataclass(frozen=True)
class StreamingAssistantFields:
    """Final canonical assistant fields assembled from normalized deltas."""

    content: str | None
    reasoning: str | None
    reasoning_meta: JsonObject | None
    tool_calls: list[JsonObject] | None
    finish_reason: TerminalOutcome | None
    usage: JsonObject | None = None
    reasoning_timing: JsonObject | None = None
    reasoning_summary: list[str] | None = None

    def to_response_dict(self) -> JsonObject:
        """Return fields in the same shape as adapter response normalization."""
        result: JsonObject = {
            "content": self.content,
            "reasoning": self.reasoning,
            "reasoning_meta": self.reasoning_meta,
            "tool_calls": self.tool_calls,
        }
        if self.reasoning_summary is not None:
            result["reasoning_summary"] = self.reasoning_summary
        if self.finish_reason is not None:
            result["terminal_outcome"] = self.finish_reason
        if self.usage is not None:
            result["usage"] = self.usage
        return result


@dataclass
class _ToolCallFragments:
    stream_slot: str
    synthetic_id_suffix: str
    provider_id: str | None = None
    name_text: str = ""
    arguments_text: str = ""

    @property
    def tool_call_id(self) -> str:
        """Return the Provider id, or a finalization-safe deterministic fallback."""
        return self.provider_id or f"tool_call_{self.synthetic_id_suffix}"

    def accept_provider_id(self, provider_id: str | None) -> None:
        """Adopt a real id whenever its stable stream slot supplies one."""
        if provider_id is not None:
            self.provider_id = provider_id

    def append(self, *, name_delta: str, arguments_delta: str) -> None:
        """Append true deltas verbatim.

        Adapters own wire snapshots and emit only unseen suffixes. Repeated or
        prefix-overlapping bytes are therefore meaningful content, including
        a second identical top-level argument value that finalization expands
        into a sibling Call.
        """
        self.name_text += name_delta
        self.arguments_text += arguments_delta

    def arguments_closed(self) -> bool:
        """Whether the call has a name and its arguments form one complete JSON value."""
        text = self.arguments_text.strip()
        if not self.name_text or not text:
            return False
        try:
            json.loads(text)
        except ValueError:
            return False
        return True

    def to_tool_calls(self) -> list[JsonObject]:
        return normalize_tool_call_candidates(
            tool_call_id=self.provider_id,
            name=self.name_text,
            arguments=self.arguments_text,
            fallback_id=f"tool_call_{self.synthetic_id_suffix}",
        )


class StreamingAccumulator:
    """Accumulate normalized provider deltas into final assistant fields.

    A Tool Call is complete once the stream has moved past it (a later Tool
    Call, answer text or Reasoning began) and its arguments form one complete
    JSON value, or once the stream finished. Providers stream one Tool Call
    after another, so this needs no Provider-specific end marker; a call whose
    arguments are still open when the stream moves on (interleaved fragments)
    waits for the finish. :meth:`take_completed_tool_calls` hands complete
    calls to a caller that runs them while the Model is still writing; a taken
    call is final, and the finished Assistant fields carry exactly the calls
    that were taken.
    """

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._reasoning_summary: list[str] = []
        self._last_text_delta_type: str | None = None
        self._reasoning_meta: JsonObject | None = None
        self._reasoning_started_perf: float | None = None
        self._reasoning_ended_perf: float | None = None
        self._reasoning_started_at: str | None = None
        self._reasoning_completed_at: str | None = None
        self._tool_calls: OrderedDict[str, _ToolCallFragments] = OrderedDict()
        # The slot whose fragments are still arriving; every earlier slot is complete.
        self._open_tool_slot: str | None = None
        # Calls already handed out, frozen per slot in the order they were taken.
        self._taken_tool_calls: OrderedDict[str, list[JsonObject]] = OrderedDict()
        self._finish_reason: TerminalOutcome | None = None
        self._usage: JsonObject | None = None

    @property
    def finish_reason(self) -> TerminalOutcome | None:
        """Return the normalized finish reason, if the stream provided one."""
        return self._finish_reason

    @property
    def usage(self) -> JsonObject | None:
        """Return reported counters even when the attempt has no usable output."""
        return dict(self._usage) if self._usage is not None else None

    @property
    def partial_reasoning(self) -> str | None:
        """Return accumulated reasoning text so far, or None if empty."""
        return _joined_or_none(self._reasoning_parts)

    @property
    def partial_content(self) -> str | None:
        """Return accumulated visible content so far, or None if empty."""
        return _joined_or_none(self._content_parts)

    @property
    def ends_with_reasoning(self) -> bool:
        """Whether readable Reasoning was the stream's final text phase."""
        return self._last_text_delta_type == "reasoning_delta"

    @property
    def reasoning_timing(self) -> JsonObject | None:
        """Return the measured first-to-last reasoning delta span, or None.

        The duration uses a monotonic clock; the display timestamps are UTC ISO
        strings captured at the first and last accepted reasoning delta, matching
        the canonical ``timing`` payload shape used by Tool Calls and Runs.
        """
        if self._reasoning_started_perf is None or self._reasoning_ended_perf is None:
            return None
        return {
            "started_at": self._reasoning_started_at,
            "completed_at": self._reasoning_completed_at,
            "duration_ms": max(
                0,
                round((self._reasoning_ended_perf - self._reasoning_started_perf) * 1000),
            ),
        }

    @property
    def has_partial_tool_call(self) -> bool:
        """Whether any Tool Call fragment arrived during this attempt."""
        return bool(self._tool_calls)

    @property
    def has_taken_tool_calls(self) -> bool:
        """Whether a caller took complete Tool Calls during this attempt."""
        return bool(self._taken_tool_calls)

    def take_completed_tool_calls(self) -> list[JsonObject]:
        """Return the Tool Calls completed since the last take, in stream order.

        Only a leading run of complete slots is handed out, so taken calls are
        always a prefix of the final Tool Calls and keep their final positions.
        A slot can expand into several calls (consecutive JSON argument values).
        """
        taken: list[JsonObject] = []
        for slot, fragments in self._tool_calls.items():
            if slot in self._taken_tool_calls:
                continue
            if self._finish_reason is None and (
                slot == self._open_tool_slot or not fragments.arguments_closed()
            ):
                break
            calls = fragments.to_tool_calls()
            self._taken_tool_calls[slot] = calls
            taken.extend(calls)
        return taken

    def add_delta(self, delta: JsonObject) -> list[StreamingVisibleDelta]:
        """Accept one normalized provider delta and return public deltas to emit."""
        delta_type = _require_delta_type(delta)
        match delta_type:
            case "content_delta":
                visible_delta = self._add_content_delta(delta)
            case "reasoning_delta":
                visible_delta = self._add_reasoning_delta(delta)
            case "tool_call_delta":
                visible_delta = self._add_tool_call_delta(delta)
            case "reasoning_meta":
                self._add_reasoning_meta(delta)
                return []
            case "usage":
                self._add_usage(delta)
                return []
            case "finish":
                self._add_finish(delta)
                return []
            case _:
                raise StreamingDeltaError(f"unsupported streaming delta type: {delta_type}")

        if visible_delta is None:
            return []
        return [visible_delta]

    def finalize_assistant_fields(self) -> StreamingAssistantFields:
        """Build final fields, preserving malformed Tool Calls as rejected calls."""
        tool_calls: list[JsonObject] = []
        for slot, fragments in self._tool_calls.items():
            taken = self._taken_tool_calls.get(slot)
            tool_calls.extend(taken if taken is not None else fragments.to_tool_calls())
        content, reasoning = split_inline_reasoning(
            _joined_or_none(self._content_parts), _joined_or_none(self._reasoning_parts)
        )
        return StreamingAssistantFields(
            content=content,
            reasoning=reasoning,
            reasoning_meta=dict(self._reasoning_meta) if self._reasoning_meta is not None else None,
            tool_calls=tool_calls or None,
            finish_reason=self._finish_reason,
            usage=dict(self._usage) if self._usage is not None else None,
            reasoning_timing=self.reasoning_timing,
            reasoning_summary=list(self._reasoning_summary) or None,
        )

    def finalize_partial_fields(self) -> StreamingAssistantFields:
        """Build assistant fields from a stream that broke before it finished.

        Taken Tool Calls stay, because the caller already runs them. Any other
        Tool Call fragment was never handed out or executed and is dropped. No
        ``finish_reason`` is set, so the result reads as an unfinished
        Assistant turn the next request can continue.
        """
        taken = [call for calls in self._taken_tool_calls.values() for call in calls]
        content, reasoning = split_inline_reasoning(
            _joined_or_none(self._content_parts), _joined_or_none(self._reasoning_parts)
        )
        return StreamingAssistantFields(
            content=content,
            reasoning=reasoning,
            reasoning_meta=dict(self._reasoning_meta) if self._reasoning_meta is not None else None,
            tool_calls=taken or None,
            finish_reason=None,
            usage=dict(self._usage) if self._usage is not None else None,
            reasoning_timing=self.reasoning_timing,
            reasoning_summary=list(self._reasoning_summary) or None,
        )

    def _add_content_delta(self, delta: JsonObject) -> StreamingVisibleDelta | None:
        text = _optional_delta_string(delta, "text")
        if not text:
            return None
        self._open_tool_slot = None
        self._last_text_delta_type = "content_delta"
        self._content_parts.append(text)
        return StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": text},
        )

    def _add_reasoning_delta(self, delta: JsonObject) -> StreamingVisibleDelta | None:
        text = _optional_delta_string(delta, "text")
        if not text:
            return None
        self._open_tool_slot = None
        self._last_text_delta_type = "reasoning_delta"
        self._record_reasoning_activity()
        self._reasoning_parts.append(text)
        payload: JsonObject = {"reasoning_delta": text}
        index = delta.get("summary_index")
        summary_text = delta.get("summary_text")
        if isinstance(index, int) and not isinstance(index, bool) and isinstance(summary_text, str):
            if index < 0 or index > len(self._reasoning_summary):
                raise StreamingDeltaError("reasoning summary sections must arrive in order")
            if index == len(self._reasoning_summary):
                self._reasoning_summary.append("")
            self._reasoning_summary[index] += summary_text
            payload.update(summary_index=index, summary_text=summary_text)
        return StreamingVisibleDelta(event_type=REASONING_DELTA_EVENT, payload=payload)

    def _record_reasoning_activity(self) -> None:
        now_perf = time.monotonic()
        if self._reasoning_started_perf is None:
            self._reasoning_started_perf = now_perf
            self._reasoning_started_at = utc_now_timestamp()
        self._reasoning_ended_perf = now_perf
        self._reasoning_completed_at = utc_now_timestamp()

    def _add_tool_call_delta(self, delta: JsonObject) -> StreamingVisibleDelta | None:
        stream_slot, synthetic_id_suffix = _tool_call_stream_slot(delta)
        provider_id = _optional_tool_call_provider_id(delta)
        name_delta = _optional_delta_string(delta, "name_delta")
        arguments_delta = _optional_delta_string(delta, "arguments_delta")
        if stream_slot in self._taken_tool_calls:
            # The call already runs as it was when the stream moved past it.
            # A Provider that returns to it breaks stream order; keep what ran.
            if name_delta or arguments_delta:
                _LOGGER.warning(
                    "Provider stream returned to a Tool Call that already started; "
                    "ignoring the late fragment"
                )
            return None
        self._open_tool_slot = stream_slot

        fragments = self._tool_calls.setdefault(
            stream_slot,
            _ToolCallFragments(
                stream_slot=stream_slot,
                synthetic_id_suffix=synthetic_id_suffix,
            ),
        )
        fragments.accept_provider_id(provider_id)
        if not name_delta and not arguments_delta:
            return None
        fragments.append(name_delta=name_delta, arguments_delta=arguments_delta)

        # Tool-call deltas are transient. Before an index-based wire supplies its
        # real id, this field is only a stable display correlation handle; the
        # persisted Tool Call is synthesized only at finalization if no id ever
        # arrived. This keeps public Run-event order unchanged while allowing a
        # late Provider id to become canonical.
        payload: JsonObject = {"tool_call_id": fragments.tool_call_id}
        if name_delta:
            # Displays use registry names; a name split across deltas is
            # corrected when the finished Tool Call replaces the transient row.
            payload["name_delta"] = registry_tool_name(name_delta)
        if arguments_delta:
            payload["arguments_delta"] = arguments_delta
        return StreamingVisibleDelta(event_type=TOOL_CALL_DELTA_EVENT, payload=payload)

    def _add_reasoning_meta(self, delta: JsonObject) -> None:
        reasoning_meta = delta.get("reasoning_meta")
        if not isinstance(reasoning_meta, dict):
            raise StreamingDeltaError("reasoning_meta delta must include an object")
        self._reasoning_meta = merge_reasoning_meta(self._reasoning_meta, reasoning_meta)

    def _add_usage(self, delta: JsonObject) -> None:
        from core.models.pricing import nonnegative_amount

        usage = dict(self._usage or {})
        primary_count = 0
        for token_key in ("input_tokens", "output_tokens"):
            if token_key not in delta:
                continue
            token_count = delta[token_key]
            if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
                raise StreamingDeltaError(f"usage delta {token_key} must be a non-negative integer")
            usage[token_key] = token_count
            primary_count += 1
        if primary_count == 0:
            raise StreamingDeltaError("usage delta must include input_tokens or output_tokens")
        for detail_key in ("cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
            detail_tokens = delta.get(detail_key)
            if (
                isinstance(detail_tokens, int)
                and not isinstance(detail_tokens, bool)
                and detail_tokens >= 0
            ):
                usage[detail_key] = detail_tokens
        reported_cost = nonnegative_amount(delta.get("reported_cost_usd"))
        if reported_cost is not None:
            usage["reported_cost_usd"] = reported_cost
        self._usage = usage

    def _add_finish(self, delta: JsonObject) -> None:
        reason = delta.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise StreamingDeltaError("finish reason must be a string")
        if reason is None:
            self._finish_reason = TERMINAL_OUTCOME_UNKNOWN
            return
        self._finish_reason = terminal_outcome_from_response({"terminal_outcome": reason})


async def stream_model_response(
    adapter: Any,
    messages: list[JsonObject],
    *,
    model_id: str,
    on_tool_calls: Callable[[list[JsonObject]], None] | None = None,
    on_usage: Callable[[JsonObject], None] | None = None,
    **request_options: Any,
) -> JsonObject:
    """Stream one Model request and return its completed, normalized response.

    The result has the shape ``normalize_response`` returns (``content``,
    ``reasoning``, ``reasoning_meta``, ``tool_calls``, ``terminal_outcome``,
    ``usage``). Every kernel Model request outside Chat goes through here, so
    stall timeouts apply and a Provider that rejects large completed responses
    still answers. ``on_tool_calls`` receives each Tool Call once the stream
    has moved past it, while the Model is still writing. A stream that breaks
    after that raises :class:`StreamBrokenAfterToolCallsError` with the output
    so far instead of a replayable error. ``on_usage`` receives the Usage
    accumulated so far after each usage delta, so a caller can record what a
    stream that later fails or is cancelled consumed; it must be cheap and
    synchronous, and an exception it raises is logged without ending the
    stream. Stall guards follow
    :func:`stream_stall_timeout`, so a local Provider's long prefill is not cut off.

    Only a Provider that declares this request cannot stream
    (``ProviderStreamingUnsupportedError``, before any output) is asked for a
    completed response instead.
    """
    accumulator = StreamingAccumulator()
    chunk_timeout_seconds = stream_stall_timeout(adapter)
    try:
        async for delta in iter_with_chunk_timeout(
            adapter.stream(messages, model_id=model_id, **request_options),
            timeout_seconds=chunk_timeout_seconds,
            progress_timeout_seconds=(
                STREAM_PROGRESS_TIMEOUT_SECONDS if chunk_timeout_seconds is not None else None
            ),
        ):
            if delta.get("type") == "heartbeat":
                continue
            accumulator.add_delta(delta)
            if on_usage is not None and delta.get("type") == "usage":
                _report_usage(on_usage, accumulator.usage or {})
            if on_tool_calls is not None and (completed := accumulator.take_completed_tool_calls()):
                on_tool_calls(completed)
        if accumulator.finish_reason is None:
            raise NetworkError("Provider stream ended without finish delta")
        if on_tool_calls is not None and (completed := accumulator.take_completed_tool_calls()):
            on_tool_calls(completed)
    except ProviderStreamingUnsupportedError as exc:
        if accumulator.has_taken_tool_calls:
            raise StreamBrokenAfterToolCallsError(
                exc, accumulator.finalize_partial_fields().to_response_dict()
            ) from exc
        if accumulator.has_partial_tool_call or accumulator.partial_content is not None:
            raise
        if accumulator.partial_reasoning is not None:
            raise
        response = await adapter.send(messages, model_id=model_id, **request_options)
        normalized: JsonObject = adapter.normalize_response(response, model_id=model_id)
        content = normalized.get("content")
        reasoning = normalized.get("reasoning")
        if isinstance(content, str) and (reasoning is None or isinstance(reasoning, str)):
            normalized["content"], normalized["reasoning"] = split_inline_reasoning(
                content, reasoning
            )
        if on_tool_calls is not None and normalized.get("tool_calls"):
            on_tool_calls(list(normalized["tool_calls"]))
        return normalized
    except Exception as exc:
        if accumulator.has_taken_tool_calls:
            raise StreamBrokenAfterToolCallsError(
                exc, accumulator.finalize_partial_fields().to_response_dict()
            ) from exc
        raise
    return accumulator.finalize_assistant_fields().to_response_dict()


def _report_usage(on_usage: Callable[[JsonObject], None], usage: JsonObject) -> None:
    """Hand *usage* to the caller's observer; its failure never ends the stream."""
    try:
        on_usage(usage)
    except Exception:
        _LOGGER.warning("Usage observer failed during a Model stream", exc_info=True)


async def iter_with_chunk_timeout(
    source: AsyncIterator[JsonObject],
    *,
    timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
    progress_timeout_seconds: float | None = STREAM_PROGRESS_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> AsyncIterator[JsonObject]:
    """Yield stream chunks with separate transport and Model-progress timeouts.

    Model requests disable both stall windows for local Providers whose prefill
    can be silent for minutes (:func:`stream_stall_timeout`). Provider
    heartbeats reset the transport window but not ``progress_timeout_seconds``:
    OpenAI-compatible gateways may buffer a complete Tool Call while sending SSE
    comments, so those comments prove the request is alive without pretending
    the Model produced a delta.
    An optional monotonic deadline bounds recovery regardless of fresh deltas.
    """
    if timeout_seconds is None and progress_timeout_seconds is None and deadline is None:
        async for chunk in source:
            yield chunk
        return
    iterator = source.__aiter__()
    last_progress_at = time.monotonic()
    while True:
        progress_remaining = _remaining_progress_seconds(
            last_progress_at,
            progress_timeout_seconds,
        )
        deadline_remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        wait_timeout = _minimum_timeout(timeout_seconds, progress_remaining, deadline_remaining)
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=wait_timeout)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            await _close_async_iterator(iterator)
            # The bound that sized this wait decides the failure: ``wait_for`` can
            # resume marginally before the bound it was given, so comparing the bound
            # is race-free where a wall-clock comparison is not.
            if deadline_remaining is not None and wait_timeout == deadline_remaining:
                raise StreamingProgressTimeoutError("Model recovery time budget exhausted") from exc
            if progress_remaining is not None and wait_timeout == progress_remaining:
                raise StreamingProgressTimeoutError(
                    "provider connection stayed alive but produced no Model delta for "
                    f"{progress_timeout_seconds:g} seconds"
                ) from exc
            active_stall_seconds = timeout_seconds if timeout_seconds is not None else wait_timeout
            raise StreamingChunkTimeoutError(
                f"provider stream stalled for {active_stall_seconds or 0.0:g} seconds"
            ) from exc
        if chunk.get("type") != "heartbeat":
            last_progress_at = time.monotonic()
        yield chunk


def _remaining_progress_seconds(
    last_progress_at: float,
    progress_timeout_seconds: float | None,
) -> float | None:
    if progress_timeout_seconds is None:
        return None
    return max(0.0, progress_timeout_seconds - (time.monotonic() - last_progress_at))


def _minimum_timeout(*timeouts: float | None) -> float | None:
    values = [value for value in timeouts if value is not None]
    return min(values) if values else None


def _require_delta_type(delta: JsonObject) -> str:
    return _require_delta_string(delta, "type")


def _require_delta_string(delta: JsonObject, key: str) -> str:
    value = delta.get(key)
    if not isinstance(value, str) or not value:
        raise StreamingDeltaError(f"streaming delta {key} must be a non-empty string")
    return value


def _optional_delta_string(delta: JsonObject, key: str) -> str:
    value = delta.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StreamingDeltaError(f"streaming delta {key} must be a string")
    return value


def _tool_call_stream_slot(delta: JsonObject) -> tuple[str, str]:
    slot = delta.get("slot")
    if isinstance(slot, int) and not isinstance(slot, bool):
        return f"index:{slot}", str(slot)
    if isinstance(slot, str) and slot:
        return f"slot:{slot}", slot

    provider_id = _require_delta_string(delta, "id")
    return f"id:{provider_id}", provider_id


def _optional_tool_call_provider_id(delta: JsonObject) -> str | None:
    provider_id = delta.get("id")
    if provider_id is None:
        return None
    if not isinstance(provider_id, str) or not provider_id:
        raise StreamingDeltaError("streaming delta id must be a non-empty string")
    return provider_id


def _joined_or_none(parts: list[str]) -> str | None:
    if not parts:
        return None
    return "".join(parts)


# Inline reasoning tag names some Models emit in their content instead of using
# a dedicated reasoning field (observed behind Ollama).
_INLINE_THINKING_TAG_NAMES = ("think", "thinking", "reasoning")
# Request-only replay markup adapters inject into historical Assistant content.
# Models may echo it; strip on ingest and never promote it to ``reasoning``.
_DISCARD_LEADING_TAG_NAMES = ("reasoning_history",)


def split_inline_reasoning(
    content: str | None, reasoning: str | None
) -> tuple[str | None, str | None]:
    """Move inline reasoning markup out of a Model's answer content.

    Every finished Model response passes through here, so Chat, Compaction and
    every other kernel request see the same answer. Two shapes are reasoning:

    - Leading ``<think>`` / ``<thinking>`` / ``<reasoning>`` blocks. An
      unclosed leading block is reasoning up to the truncation point.
    - Text before a closing tag that has no opening tag in front of it: chat
      templates that open the thinking block in the prompt leave only
      ``...</think>answer`` in the content.

    Literal tag text inside a normal answer survives, since its opening tag
    precedes the closing one. Request-only ``<reasoning_history>`` wrappers are
    discarded (adapters inject those on replay, and Models sometimes echo
    them). Extracted reasoning is appended to *reasoning*. Returns
    ``(content, reasoning)``; an empty block with no history markup changes
    nothing.
    """

    if not content:
        return (content, reasoning)
    remaining = content
    thinking_parts: list[str] = []
    discarded_history = False
    orphan = _orphan_closing_thinking_tag(remaining)
    if orphan is not None:
        orphan_index, orphan_tag = orphan
        thinking_parts.append(remaining[:orphan_index])
        remaining = remaining[orphan_index + len(orphan_tag) + 3 :]
    while True:
        stripped = remaining.lstrip()
        tag = next(
            (
                name
                for name in (*_DISCARD_LEADING_TAG_NAMES, *_INLINE_THINKING_TAG_NAMES)
                if stripped.startswith(f"<{name}>")
            ),
            None,
        )
        if tag is None:
            break
        is_history_markup = tag in _DISCARD_LEADING_TAG_NAMES
        inner_start = len(remaining) - len(stripped) + len(tag) + 2
        close_index = remaining.find(f"</{tag}>", inner_start)
        if close_index == -1:
            if is_history_markup:
                discarded_history = True
            else:
                thinking_parts.append(remaining[inner_start:])
            remaining = ""
            break
        if is_history_markup:
            discarded_history = True
        else:
            thinking_parts.append(remaining[inner_start:close_index])
        remaining = remaining[close_index + len(tag) + 3 :]
    thinking = "\n".join(part for part in thinking_parts if part.strip())
    if not thinking.strip():
        if not discarded_history and orphan is None:
            return (content, reasoning)
        return (remaining.strip() or None, reasoning)
    merged = f"{reasoning}\n{thinking}" if reasoning else thinking
    return (remaining.strip() or None, merged)


def _orphan_closing_thinking_tag(content: str) -> tuple[int, str] | None:
    """Return the first closing thinking tag no opening tag precedes, if any."""
    first: tuple[int, str] | None = None
    for name in _INLINE_THINKING_TAG_NAMES:
        close_index = content.find(f"</{name}>")
        if close_index == -1 or f"<{name}>" in content[:close_index]:
            continue
        if first is None or close_index < first[0]:
            first = (close_index, name)
    return first


async def _close_async_iterator(iterator: AsyncIterator[JsonObject]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    result = close()
    if result is not None:
        await result
