"""One Adapter request path per wire, for the tests that hold on every path.

A path names the Adapter, the endpoint its request posts to, a Model the
Adapter's wire profile routes there and a completed reply; ``streaming`` turns
it into the stream variant of the same wire. Codex WebSocket requests are not
HTTP requests and have no path here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import httpx

from . import anthropic_test_support as anthropic
from . import github_copilot_test_support as copilot
from . import ollama_test_support as ollama
from . import openai_compatible_test_support as compatible
from . import openai_test_support as openai
from . import opencode_go_test_support as go
from . import opencode_zen_test_support as zen
from . import openrouter_test_support as openrouter

HELLO = [{"role": "user", "content": "Hello"}]


@dataclass(frozen=True)
class RequestPath:
    """One Adapter request path: the Adapter, the endpoint it posts to and a completed reply."""

    adapter: Callable[[], Any]
    url: str
    model_id: str
    success: Callable[[], httpx.Response]
    stream: bool = False

    def streaming(
        self, success: Callable[[], httpx.Response], url: str | None = None
    ) -> RequestPath:
        return replace(self, success=success, url=url or self.url, stream=True)


async def request(
    adapter: Any,
    path: RequestPath,
    kwargs: dict[str, Any],
    messages: list[dict[str, Any]] = HELLO,
) -> None:
    """Send or stream one request on ``path`` to completion."""

    if path.stream:
        [_ async for _ in adapter.stream(messages, model_id=path.model_id, **kwargs)]
    else:
        await adapter.send(messages, model_id=path.model_id, **kwargs)


def chat_sse(*chunks: dict[str, Any]) -> httpx.Response:
    """A Chat Completions stream of ``chunks`` that finishes with ``ok``."""

    finish = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
    return compatible.sse_response(compatible.sse(*chunks, finish))


def responses_sse(response: dict[str, Any] = openai.COMPLETED_RESPONSE) -> httpx.Response:
    """A Responses stream that completes with ``response``."""

    return openai.codex_sse_response(response)


GEMINI_DONE = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}

CHAT = RequestPath(
    lambda: compatible.make_adapter(
        model=compatible.catalog_model(levels=("low", "medium", "high"))
    ),
    compatible.OPENAI_URL,
    compatible.MODEL_ID,
    lambda: httpx.Response(200, json=compatible.SUCCESS_RESPONSE),
)
MESSAGES = RequestPath(
    anthropic.make_adapter,
    anthropic.ANTHROPIC_URL,
    anthropic.MODEL_ID,
    lambda: httpx.Response(200, json=anthropic.SUCCESS_RESPONSE),
)
PLATFORM = RequestPath(
    lambda: openai.platform_adapter(model_lookup=openai.bundled_model_lookup()),
    openai.PLATFORM_RESPONSES_URL,
    "gpt-6-sol",
    lambda: httpx.Response(200, json=openai.COMPLETED_RESPONSE),
)
CODEX = RequestPath(
    lambda: openai.codex_adapter(model_lookup=openai.bundled_model_lookup()),
    openai.OPENAI_SUBSCRIPTION_URL,
    "gpt-6-sol",
    responses_sse,
)
OPENROUTER = RequestPath(
    openrouter.openrouter_adapter,
    openrouter.RESPONSES_URL,
    openrouter.RESPONSES_MODEL,
    lambda: httpx.Response(200, json=openai.COMPLETED_RESPONSE),
)
COPILOT_RESPONSES = RequestPath(
    copilot.make_adapter,
    copilot.RESPONSES_URL,
    "gpt-5-mini",
    lambda: httpx.Response(200, json={"output": []}),
)
COPILOT_MESSAGES = RequestPath(
    lambda: copilot.make_adapter(
        metadata=copilot.copilot_metadata(
            "Anthropic", "claude-opus-4.8", ["/chat/completions", "/v1/messages"]
        )
    ),
    copilot.MESSAGES_URL,
    "claude-opus-4.8",
    lambda: httpx.Response(200, json={"content": []}),
)
COPILOT_MESSAGES_STREAM = COPILOT_MESSAGES.streaming(
    lambda: copilot.sse_response(
        copilot.sse_events(
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        )
    )
)
GO_RESPONSES = RequestPath(
    go.go_adapter,
    go.RESPONSES_URL,
    go.RESPONSES_MODEL,
    lambda: go.success_response("responses", streaming=False),
)
GO_MESSAGES = RequestPath(
    go.go_adapter,
    go.MESSAGES_URL,
    go.MESSAGES_MODEL,
    lambda: go.success_response("messages", streaming=False),
)
ZEN_RESPONSES = RequestPath(
    zen.zen_adapter,
    zen.RESPONSES_URL,
    zen.RESPONSES_MODEL,
    lambda: httpx.Response(200, json=openai.COMPLETED_RESPONSE),
)
ZEN_GEMINI = RequestPath(
    zen.zen_adapter,
    zen.GEMINI_URL,
    zen.GEMINI_MODEL,
    lambda: httpx.Response(200, json=GEMINI_DONE),
)
ZEN_GEMINI_STREAM = ZEN_GEMINI.streaming(
    lambda: httpx.Response(200, text=zen.gemini_sse(GEMINI_DONE)), zen.GEMINI_STREAM_URL
)
OLLAMA = RequestPath(
    ollama.local_adapter,
    ollama.LOCAL_CHAT_URL,
    "plain-model",
    lambda: httpx.Response(200, json=ollama.TEXT_RESPONSE),
)
