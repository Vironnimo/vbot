"""Provider-agnostic helpers for chat streaming accumulation."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from core.chat.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
)
from core.utils.errors import VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

_LOGGER = get_logger("chat.streaming")

STREAM_CHUNK_TIMEOUT_SECONDS = 180.0

CONTENT_DELTA_TYPE = "content_delta"
REASONING_DELTA_TYPE = "reasoning_delta"
TOOL_CALL_DELTA_TYPE = "tool_call_delta"
REASONING_META_TYPE = "reasoning_meta"
USAGE_TYPE = "usage"
FINISH_TYPE = "finish"


class StreamingError(VBotError):
    """Base error for provider-agnostic streaming helpers."""


class StreamingDeltaError(StreamingError):
    """Raised when an adapter yields an invalid normalized streaming delta."""


class StreamingChunkTimeoutError(StreamingError):
    """Raised when a provider stream stalls between chunks."""


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
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)

    def append(self, *, name_delta: str, arguments_delta: str) -> None:
        if name_delta:
            self.name_parts.append(name_delta)
        if arguments_delta:
            self.argument_parts.append(arguments_delta)

    def to_tool_call(self) -> JsonObject:
        return {
            "id": self.tool_call_id,
            "name": "".join(self.name_parts),
            "arguments": _parse_tool_arguments("".join(self.argument_parts)),
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
        tool_calls = [fragments.to_tool_call() for fragments in self._tool_calls.values()]
        return StreamingAssistantFields(
            content=_joined_or_none(self._content_parts),
            reasoning=_joined_or_none(self._reasoning_parts),
            reasoning_meta=dict(self._reasoning_meta) if self._reasoning_meta is not None else None,
            tool_calls=tool_calls or None,
            finish_reason=self._finish_reason,
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
        fragments.append(name_delta=name_delta, arguments_delta=arguments_delta)

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
        self._usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}

    def _add_finish(self, delta: JsonObject) -> None:
        reason = delta.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise StreamingDeltaError("finish reason must be a string")
        self._finish_reason = reason


async def iter_with_chunk_timeout(
    source: AsyncIterator[JsonObject],
    *,
    timeout_seconds: float = STREAM_CHUNK_TIMEOUT_SECONDS,
) -> AsyncIterator[JsonObject]:
    """Yield stream chunks with a timeout that resets before each chunk."""
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


def _parse_tool_arguments(arguments_text: str) -> JsonObject:
    if not arguments_text:
        return {}
    try:
        value = json.loads(arguments_text)
    except json.JSONDecodeError:
        _LOGGER.warning(
            "tool call arguments JSON parse failed - fragment: %r",
            arguments_text,
        )
        return {}
    if not isinstance(value, dict):
        return {}
    return value


async def _close_async_iterator(iterator: AsyncIterator[JsonObject]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    result = close()
    if result is not None:
        await result
