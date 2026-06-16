"""Run-event projection and chat failure persistence."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.chat.errors import ChatError, ToolIterationLimitError
from core.chat.messages import (
    ERROR_KIND_AUTH,
    ERROR_KIND_CONFIG,
    ERROR_KIND_NETWORK,
    ERROR_KIND_PROVIDER_ERROR,
    ERROR_KIND_PROVIDER_FATAL,
    ERROR_KIND_PROVIDER_OVERLOAD,
    ERROR_KIND_RATE_LIMIT,
    ERROR_KIND_TIMEOUT,
    ERROR_KIND_TOOL_ITERATIONS,
    ChatMessage,
    JsonObject,
    _format_timestamp,
)
from core.chat.streaming import (
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    StreamingDeltaError,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    REASONING_EVENT,
    Run,
)
from core.sessions import PARTIAL_THINKING_NOTE_PREFIX, ChatSession
from core.utils.errors import ConfigError, ProviderError, VBotError


async def _close_adapter(adapter: Any) -> None:
    close_method = getattr(adapter, "aclose", None)
    if not callable(close_method):
        return
    result = close_method()
    if inspect.isawaitable(result):
        await result


def _emit_assistant_events(run: Run, message: ChatMessage) -> None:
    if message.reasoning:
        run.emit(REASONING_EVENT, {"message": _visible_message_payload(message)})
    if message.content:
        _emit_message_event(run, ASSISTANT_OUTPUT_EVENT, message)


def _emit_streaming_assistant_events(run: Run, message: ChatMessage) -> None:
    if message.reasoning:
        run.emit(REASONING_EVENT, {"message": _visible_message_payload(message)})
    _emit_message_event(run, ASSISTANT_OUTPUT_EVENT, message)


def _emit_message_event(run: Run, event_type: str, message: ChatMessage) -> None:
    run.emit(event_type, {"message": _visible_message_payload(message)})


PARTIAL_THINKING_CAP = 2000
"""Max chars of partial reasoning kept in the interruption note (head retained)."""


def _maybe_persist_partial_thinking(
    accumulator: StreamingAccumulator,
    note_hook: Callable[[str], None] | None,
) -> None:
    if note_hook is None:
        return
    partial = accumulator.partial_reasoning
    if partial:
        note_hook(_partial_thinking_note_content(partial))


def _partial_thinking_note_content(partial: str) -> str:
    """Prefix-tag and size-cap the interruption note.

    The ``[partial-thinking]`` prefix lets ``_assemble_request_history`` embed
    the note one-shot (only until the next run produces an assistant turn); the
    cap stops a long aborted thinking stream from bloating every later request.
    """
    if len(partial) > PARTIAL_THINKING_CAP:
        partial = f"{partial[:PARTIAL_THINKING_CAP]}\n[… partial thinking truncated]"
    return f"{PARTIAL_THINKING_NOTE_PREFIX}Partial thinking before interruption:\n{partial}"


def _visible_message_payload(message: ChatMessage) -> JsonObject:
    data = message.to_dict()
    data.pop("reasoning_meta", None)
    return data


def _emit_tool_context_event(run: Run, event_type: str, payload: JsonObject) -> None:
    run.emit(event_type, payload)


def _exception_to_error_kind(exc: Exception) -> str:
    if isinstance(exc, ProviderRateLimitError):
        return ERROR_KIND_RATE_LIMIT
    if isinstance(exc, ProviderTimeoutError):
        return ERROR_KIND_TIMEOUT
    if isinstance(exc, StreamingChunkTimeoutError):
        return ERROR_KIND_TIMEOUT
    if isinstance(exc, StreamingDeltaError):
        return ERROR_KIND_PROVIDER_ERROR
    if isinstance(exc, NetworkError):
        return ERROR_KIND_NETWORK
    if isinstance(exc, ProviderAuthError):
        return ERROR_KIND_AUTH
    if isinstance(exc, ProviderError):
        if exc.retryable:
            return ERROR_KIND_PROVIDER_OVERLOAD
        return ERROR_KIND_PROVIDER_FATAL
    if isinstance(exc, ToolIterationLimitError):
        return ERROR_KIND_TOOL_ITERATIONS
    if isinstance(exc, (ChatError, ConfigError, VBotError)):
        return ERROR_KIND_CONFIG
    return ERROR_KIND_PROVIDER_ERROR


def _persist_run_error(run: Run, session: ChatSession, exc: Exception) -> None:
    # Persists the user-visible error message only. The failure itself is logged
    # centrally by Run.mark_failed once the re-raised exception reaches the run
    # executor, so logging here would duplicate that entry.
    kind = _exception_to_error_kind(exc)
    error_message = ChatMessage.error(error_kind=kind, content=str(exc))
    session.append(error_message)
    _emit_message_event(run, ERROR_MESSAGE_PERSISTED_EVENT, error_message)


def _timing_payload(started_at: datetime, started_perf: float) -> JsonObject:
    completed_at = datetime.now(UTC)
    duration_ms = max(0, round((time.perf_counter() - started_perf) * 1000))
    return {
        "started_at": _format_timestamp(started_at),
        "completed_at": _format_timestamp(completed_at),
        "duration_ms": duration_ms,
    }
