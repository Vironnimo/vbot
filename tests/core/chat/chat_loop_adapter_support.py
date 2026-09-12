"""Provider adapter doubles for Chat tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, cast

from core.providers.reasoning import (
    ReasoningReplayPolicy,
)
from core.utils.tokens import estimate_request_input_tokens

JsonObject = dict[str, Any]


class StubAdapter:
    def __init__(
        self,
        responses: list[Any],
        *,
        stream_responses: list[Any] | None = None,
        wire_media_types: frozenset[str] = frozenset(),
    ) -> None:
        self._responses = responses
        self._stream_responses = stream_responses or []
        self._wire_media_types = wire_media_types
        self.requests: list[JsonObject] = []
        self.stream_requests: list[JsonObject] = []

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if not self._responses:
            raise AssertionError("unexpected adapter request")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast(JsonObject, response)

    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        return response

    def wire_media_support(self, _model_id: str) -> frozenset[str]:
        return self._wire_media_types

    def estimate_request_input_tokens(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        tools: list[JsonObject] | None = None,
    ) -> int:
        del model_id
        estimated, _ = estimate_request_input_tokens(messages, tools)
        return estimated

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if not self._stream_responses:
            raise AssertionError("unexpected adapter stream request")
        response = self._stream_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        for delta in response:
            if isinstance(delta, Exception):
                raise delta
            yield delta


class ClosingStubAdapter(StubAdapter):
    def __init__(self, responses: list[JsonObject]) -> None:
        super().__init__(responses)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class BlockingStubAdapter(StubAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.request_started.set()
        await self.release.wait()
        return {"content": "Late", "tool_calls": None}


class BlockingStreamingStubAdapter(ClosingStubAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.stream_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "before"}
        self.stream_started.set()
        await self.release.wait()
        yield {"type": "content_delta", "text": "late"}


class BlockingReasoningStreamingStubAdapter(ClosingStubAdapter):
    """Stream readable and opaque reasoning, then block until cancellation."""

    def __init__(self) -> None:
        super().__init__([])
        self.stream_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {
            "type": "reasoning_meta",
            "reasoning_meta": {"signature": "interrupted-signed-state"},
        }
        yield {"type": "reasoning_delta", "text": "Thinking hard."}
        self.stream_started.set()
        await self.release.wait()
        yield {"type": "content_delta", "text": "late"}


class SilentBlockingStreamingStubAdapter(ClosingStubAdapter):
    """Block before emitting any visible stream output."""

    def __init__(self) -> None:
        super().__init__([])
        self.stream_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.stream_started.set()
        await self.release.wait()
        yield {"type": "content_delta", "text": "late"}


class TenToolsThenBlockingReasoningAdapter(ClosingStubAdapter):
    """Completes ten tools, then exposes readable work until cancellation."""

    def __init__(self) -> None:
        super().__init__([])
        self.second_step_started = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if len(self.stream_requests) == 1:
            yield {"type": "reasoning_delta", "text": "Plan the batch. "}
            yield {"type": "reasoning_delta", "text": "Inspect every result."}
            for index in range(10):
                yield {
                    "type": "tool_call_delta",
                    "id": f"call-{index}",
                    "name_delta": "get_weather",
                    "arguments_delta": '{"city":"Berlin"}',
                }
            yield {"type": "finish", "reason": "tool_calls"}
            return
        if len(self.stream_requests) == 2:
            yield {"type": "reasoning_delta", "text": "Review the completed batch. "}
            yield {"type": "reasoning_delta", "text": "Prepare the final answer."}
            self.second_step_started.set()
            await asyncio.Event().wait()
        raise AssertionError("unexpected adapter stream request")


class StalledStreamingStubAdapter(StubAdapter):
    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "partial"}
        await asyncio.sleep(1)
        yield {"type": "content_delta", "text": "late"}


class SlowStreamingStubAdapter(StubAdapter):
    """Streams visible content, pauses, then completes — to probe the stall guard.

    With a short chunk timeout the pause trips a chunk stall for remote providers;
    for local providers (where the guard is disabled) the same pause completes.
    """

    def __init__(self, *, delay: float) -> None:
        super().__init__([])
        self._delay = delay

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "partial"}
        await asyncio.sleep(self._delay)
        yield {"type": "content_delta", "text": " done"}
        yield {"type": "finish", "reason": "stop"}


class MidStreamCancelledStubAdapter(StubAdapter):
    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "reasoning_delta", "text": "Need network."}
        raise asyncio.CancelledError


class PolicyStubAdapter(StubAdapter):
    """Stub adapter declaring an explicit reasoning replay policy."""

    def __init__(self, responses: list[Any], *, policy: ReasoningReplayPolicy) -> None:
        super().__init__(responses)
        self._policy = policy
        self.policy_queries: list[str] = []

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        self.policy_queries.append(model_id)
        return self._policy
