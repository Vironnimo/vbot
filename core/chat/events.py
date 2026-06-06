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
from core.extensions import ExtensionRegistry
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderStreamingUnsupportedError,
    ProviderTimeoutError,
)
from core.runs import (
    ASSISTANT_OUTPUT_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    REASONING_EVENT,
    ChatRunManager,
    Run,
)
from core.sessions import ChatSession
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("chat")


def _runtime_run_manager(runtime: Any) -> ChatRunManager:
    run_manager = getattr(runtime, "chat_runs", None)
    if isinstance(run_manager, ChatRunManager):
        return run_manager
    run_manager = ChatRunManager()
    runtime.chat_runs = run_manager
    return run_manager


def _runtime_extensions(runtime: Any) -> ExtensionRegistry | None:
    return getattr(runtime, "extensions", None)


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


def _is_streaming_fallback_error(error: ProviderError) -> bool:
    return isinstance(error, ProviderStreamingUnsupportedError)


def _maybe_persist_partial_thinking(
    accumulator: StreamingAccumulator,
    note_hook: Callable[[str], None] | None,
) -> None:
    if note_hook is None:
        return
    partial = accumulator.partial_reasoning
    if partial:
        note_hook(f"Partial thinking before interruption:\n{partial}")


def _visible_message_payload(message: ChatMessage) -> JsonObject:
    data = message.to_dict()
    data.pop("reasoning_meta", None)
    return data


def _emit_tool_context_event(run: Run, event_type: str, payload: JsonObject) -> None:
    run.emit(event_type, payload)


def _is_model_fallback_trigger(exc: Exception) -> bool:
    return isinstance(exc, ProviderError) and exc.retryable


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
    kind = _exception_to_error_kind(exc)
    error_message = ChatMessage.error(error_kind=kind, content=str(exc))
    session.append(error_message)
    _emit_message_event(run, ERROR_MESSAGE_PERSISTED_EVENT, error_message)
    _LOGGER.error(
        "Persisted run error for agent=%s session=%s kind=%s: %s",
        run.agent_id,
        run.session_id,
        kind,
        exc,
    )


def _timing_payload(started_at: datetime, started_perf: float) -> JsonObject:
    completed_at = datetime.now(UTC)
    duration_ms = max(0, round((time.perf_counter() - started_perf) * 1000))
    return {
        "started_at": _format_timestamp(started_at),
        "completed_at": _format_timestamp(completed_at),
        "duration_ms": duration_ms,
    }
