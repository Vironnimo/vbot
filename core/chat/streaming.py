"""Provider-agnostic helpers for chat streaming accumulation."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from core.providers.errors import ProviderStreamingUnsupportedError
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
)
from core.utils.errors import ProviderError, VBotError

JsonObject = dict[str, Any]

STREAM_CHUNK_TIMEOUT_SECONDS = 180.0
MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS = 1200


class StreamingError(VBotError):
    """Base error for provider-agnostic streaming helpers."""


class StreamingDeltaError(StreamingError):
    """Raised when an adapter yields an invalid normalized streaming delta."""


class StreamingChunkTimeoutError(StreamingError):
    """Raised when a provider stream stalls between chunks."""


class StreamRecoveryAction(Enum):
    """What the chat loop should do when a streaming attempt breaks.

    The single, provider-agnostic vocabulary for stream-break recovery: deciding
    which action applies is :func:`decide_stream_recovery` (here); executing it —
    restarting, falling back to non-streaming, finalizing the partial answer,
    leaving an interruption note, or re-raising — stays in the chat loop.
    """

    RESTART = "restart"
    FALLBACK = "fallback"
    PRESERVE_PARTIAL = "preserve_partial"
    DISCARD_WITH_NOTE = "discard_with_note"
    FAIL = "fail"


def decide_stream_recovery(
    error: Exception,
    *,
    emitted_visible_delta: bool,
    can_restart: bool,
    has_partial_content: bool,
) -> StreamRecoveryAction:
    """Decide how to recover from a broken streaming attempt.

    Provider-agnostic: it reads only normalized vBot errors plus the attempt's
    state, so the same matrix holds for every adapter. The discriminators mirror
    the converged design of the reference harnesses — "did visible output
    escape?" gates whether a replay could duplicate, and the accumulated content
    decides between preserving the partial answer and merely leaving a note.

    Before any visible delta a not-yet-visible drop can be replayed cleanly: a
    streaming-unsupported error falls back to a non-streaming request, a
    restartable transient (transport/timeout drop or chunk stall) replays the
    whole stream while restarts remain, anything else fails. Once visible output
    has escaped, the stream is never replayed: accumulated content is preserved
    as an interrupted assistant turn, a reasoning-only interruption leaves a
    partial-thinking note, and the error otherwise propagates.
    """
    if not emitted_visible_delta:
        if _is_streaming_fallback_error(error):
            return StreamRecoveryAction.FALLBACK
        if can_restart and _is_stream_restartable_error(error):
            return StreamRecoveryAction.RESTART
        return StreamRecoveryAction.FAIL
    if has_partial_content:
        return StreamRecoveryAction.PRESERVE_PARTIAL
    return StreamRecoveryAction.DISCARD_WITH_NOTE


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
    matched by type). The chat loop restarts from scratch only when *nothing
    visible* has been emitted yet, so the replay cannot duplicate output the user
    already saw — this is the streaming analogue of the streaming→non-streaming
    fallback.
    """
    if isinstance(error, StreamingChunkTimeoutError):
        return True
    return bool(getattr(error, "retryable", False))


def _is_model_fallback_trigger(error: Exception) -> bool:
    """Whether a propagated error should switch the agent to its fallback model.

    Only a retryable ``ProviderError`` (provider-specific and transient). A
    ``NetworkError`` is deliberately excluded — it is not provider-specific, so
    switching models would not help.
    """
    return isinstance(error, ProviderError) and error.retryable


def is_local_provider_base_url(base_url: str | None) -> bool:
    """Whether a provider base URL points at a loopback or private-network host.

    Local inference servers (Ollama, llama.cpp, vLLM) can stay silent for
    minutes during prompt prefill, so the per-chunk stall timeout must not abort
    them; remote providers keep the timeout. Centralized here so the chunk-stall
    policy has one source of truth. Matches ``localhost`` and ``*.localhost`` /
    ``*.local`` names plus loopback, RFC1918 private, and link-local IP literals.
    """
    if not base_url:
        return False
    host = urlparse(base_url).hostname
    if not host:
        return False
    host = host.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


@dataclass(frozen=True)
class StreamingVisibleDelta:
    """A public Run/SSE delta event ready for ChatLoop emission."""

    event_type: str
    payload: JsonObject


@dataclass(frozen=True)
class StreamingAssistantFields:
    """Final canonical assistant fields assembled from normalized deltas."""

    content: str | None
    reasoning: str | None
    reasoning_meta: JsonObject | None
    tool_calls: list[JsonObject] | None
    finish_reason: str | None
    usage: JsonObject | None = None

    def to_response_dict(self) -> JsonObject:
        """Return fields in the same shape as adapter response normalization."""
        result: JsonObject = {
            "content": self.content,
            "reasoning": self.reasoning,
            "reasoning_meta": self.reasoning_meta,
            "tool_calls": self.tool_calls,
        }
        if self.usage is not None:
            result["usage"] = self.usage
        return result


@dataclass
class _ToolCallFragments:
    tool_call_id: str
    name_text: str = ""
    arguments_text: str = ""

    def append(self, *, name_delta: str, arguments_delta: str) -> tuple[str, str]:
        self.name_text, normalized_name_delta = _merge_stream_fragment(self.name_text, name_delta)
        self.arguments_text, normalized_arguments_delta = _merge_stream_fragment(
            self.arguments_text,
            arguments_delta,
        )
        return normalized_name_delta, normalized_arguments_delta

    def to_tool_call(self) -> JsonObject:
        arguments = _parse_tool_arguments(self.arguments_text)
        if arguments is None:
            raise StreamingDeltaError(
                f"streamed tool call {self.tool_call_id!r} has malformed or incomplete "
                "arguments JSON fragment "
                f"({len(self.arguments_text)} chars): "
                f"{_preview_malformed_tool_arguments(self.arguments_text)}"
            )
        return {
            "id": self.tool_call_id,
            "name": self.name_text,
            "arguments": arguments,
        }


class StreamingAccumulator:
    """Accumulate normalized provider deltas into final assistant fields."""

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._reasoning_meta: JsonObject | None = None
        self._tool_calls: OrderedDict[str, _ToolCallFragments] = OrderedDict()
        self._visible_deltas: list[StreamingVisibleDelta] = []
        self._finish_reason: str | None = None
        self._usage: JsonObject | None = None

    @property
    def visible_deltas(self) -> list[StreamingVisibleDelta]:
        """Return visible deltas in the exact order they were accepted."""
        return list(self._visible_deltas)

    @property
    def finish_reason(self) -> str | None:
        """Return the normalized finish reason, if the stream provided one."""
        return self._finish_reason

    @property
    def partial_reasoning(self) -> str | None:
        """Return accumulated reasoning text so far, or None if empty."""
        return _joined_or_none(self._reasoning_parts)

    @property
    def partial_content(self) -> str | None:
        """Return accumulated visible content so far, or None if empty."""
        return _joined_or_none(self._content_parts)

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
        self._visible_deltas.append(visible_delta)
        return [visible_delta]

    def finalize_assistant_fields(self) -> StreamingAssistantFields:
        """Build final canonical assistant fields from accumulated deltas."""
        tool_calls: list[JsonObject] = []
        for fragments in self._tool_calls.values():
            tool_calls.append(fragments.to_tool_call())
        return StreamingAssistantFields(
            content=_joined_or_none(self._content_parts),
            reasoning=_joined_or_none(self._reasoning_parts),
            reasoning_meta=dict(self._reasoning_meta) if self._reasoning_meta is not None else None,
            tool_calls=tool_calls or None,
            finish_reason=self._finish_reason,
            usage=dict(self._usage) if self._usage is not None else None,
        )

    def finalize_partial_fields(self) -> StreamingAssistantFields:
        """Build assistant fields from a stream interrupted after visible output.

        Unlike :meth:`finalize_assistant_fields`, this never parses tool-call
        arguments and never raises on a malformed fragment: a tool call cut off
        mid-stream was never executed, so its in-flight fragment is dropped
        rather than parsed (side-effect-free). No ``finish_reason`` is set — the
        turn did not finish — so the result reads as an interrupted assistant
        turn the next request can continue.
        """
        return StreamingAssistantFields(
            content=_joined_or_none(self._content_parts),
            reasoning=_joined_or_none(self._reasoning_parts),
            reasoning_meta=dict(self._reasoning_meta) if self._reasoning_meta is not None else None,
            tool_calls=None,
            finish_reason=None,
            usage=dict(self._usage) if self._usage is not None else None,
        )

    def _add_content_delta(self, delta: JsonObject) -> StreamingVisibleDelta | None:
        text = _optional_delta_string(delta, "text")
        if not text:
            return None
        self._content_parts.append(text)
        return StreamingVisibleDelta(
            event_type=ASSISTANT_OUTPUT_DELTA_EVENT,
            payload={"content_delta": text},
        )

    def _add_reasoning_delta(self, delta: JsonObject) -> StreamingVisibleDelta | None:
        text = _optional_delta_string(delta, "text")
        if not text:
            return None
        self._reasoning_parts.append(text)
        return StreamingVisibleDelta(
            event_type=REASONING_DELTA_EVENT,
            payload={"reasoning_delta": text},
        )

    def _add_tool_call_delta(self, delta: JsonObject) -> StreamingVisibleDelta | None:
        tool_call_id = _require_delta_string(delta, "id")
        name_delta = _optional_delta_string(delta, "name_delta")
        arguments_delta = _optional_delta_string(delta, "arguments_delta")
        if not name_delta and not arguments_delta:
            return None

        fragments = self._tool_calls.setdefault(
            tool_call_id,
            _ToolCallFragments(tool_call_id=tool_call_id),
        )
        name_delta, arguments_delta = fragments.append(
            name_delta=name_delta,
            arguments_delta=arguments_delta,
        )
        if not name_delta and not arguments_delta:
            return None

        payload: JsonObject = {"tool_call_id": tool_call_id}
        if name_delta:
            payload["name_delta"] = name_delta
        if arguments_delta:
            payload["arguments_delta"] = arguments_delta
        return StreamingVisibleDelta(event_type=TOOL_CALL_DELTA_EVENT, payload=payload)

    def _add_reasoning_meta(self, delta: JsonObject) -> None:
        reasoning_meta = delta.get("reasoning_meta")
        if not isinstance(reasoning_meta, dict):
            raise StreamingDeltaError("reasoning_meta delta must include an object")
        if self._reasoning_meta is None:
            self._reasoning_meta = dict(reasoning_meta)
            return
        self._reasoning_meta.update(reasoning_meta)

    def _add_usage(self, delta: JsonObject) -> None:
        input_tokens = delta.get("input_tokens")
        output_tokens = delta.get("output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise StreamingDeltaError(
                "usage delta must include integer input_tokens and output_tokens"
            )
        usage: JsonObject = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        for cache_key in ("cache_read_tokens", "cache_write_tokens"):
            cache_tokens = delta.get(cache_key)
            if isinstance(cache_tokens, int):
                usage[cache_key] = cache_tokens
        self._usage = usage

    def _add_finish(self, delta: JsonObject) -> None:
        reason = delta.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise StreamingDeltaError("finish reason must be a string")
        self._finish_reason = reason


async def iter_with_chunk_timeout(
    source: AsyncIterator[JsonObject],
    *,
    timeout_seconds: float | None = STREAM_CHUNK_TIMEOUT_SECONDS,
) -> AsyncIterator[JsonObject]:
    """Yield stream chunks with a timeout that resets before each chunk.

    A ``timeout_seconds`` of ``None`` disables the stall guard entirely — used
    for local/loopback providers whose prefill can be silent for minutes (see
    :func:`is_local_provider_base_url`).
    """
    if timeout_seconds is None:
        async for chunk in source:
            yield chunk
        return
    iterator = source.__aiter__()
    while True:
        try:
            yield await asyncio.wait_for(iterator.__anext__(), timeout=timeout_seconds)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            await _close_async_iterator(iterator)
            raise StreamingChunkTimeoutError(
                f"provider stream stalled for {timeout_seconds:g} seconds"
            ) from exc


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


def _joined_or_none(parts: list[str]) -> str | None:
    if not parts:
        return None
    return "".join(parts)


def _merge_stream_fragment(existing: str, delta: str) -> tuple[str, str]:
    if not delta:
        return existing, ""
    if not existing:
        return delta, delta
    if delta.startswith(existing):
        suffix = delta[len(existing) :]
        return delta, suffix
    return existing + delta, delta


def _parse_tool_arguments(arguments_text: str) -> JsonObject | None:
    if not arguments_text:
        return {}
    try:
        value = json.loads(arguments_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _preview_malformed_tool_arguments(arguments_text: str) -> str:
    if len(arguments_text) <= MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS:
        return repr(arguments_text)

    edge_length = MALFORMED_TOOL_ARGUMENT_PREVIEW_CHARS // 2
    head = arguments_text[:edge_length]
    tail = arguments_text[-edge_length:]
    omitted_count = len(arguments_text) - (edge_length * 2)
    return f"{head!r} ... <{omitted_count} chars omitted> ... {tail!r}"


async def _close_async_iterator(iterator: AsyncIterator[JsonObject]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    result = close()
    if result is not None:
        await result
