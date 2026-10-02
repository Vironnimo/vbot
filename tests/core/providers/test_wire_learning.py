"""Wire facts learned from live traffic, on every Adapter request path.

Each case runs one request path (Adapter, wire, send or stream) against a mocked
endpoint. A rejection the request can be blamed for is learned, retried once in
the learned shape and remembered by later requests; every other error passes
through unchanged and teaches nothing. A reply that carries reasoning records
that the Model returned reasoning.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest
import respx

from core.providers.errors import ProviderAuthError, ProviderError

from . import anthropic_test_support as anthropic
from . import github_copilot_test_support as copilot
from . import openai_compatible_test_support as compatible
from . import openai_test_support as openai
from . import opencode_go_test_support as go
from . import opencode_zen_test_support as zen
from . import openrouter_test_support as openrouter

ABSENT = object()

MESSAGES = [{"role": "user", "content": "Hello"}]


@dataclass(frozen=True)
class _Path:
    """One Adapter request path: the Adapter, the endpoint it posts to and a completed reply."""

    adapter: Callable[[], Any]
    url: str
    model_id: str
    success: Callable[[], httpx.Response]
    stream: bool = False

    def streaming(self, success: Callable[[], httpx.Response], url: str | None = None) -> _Path:
        return replace(self, success=success, url=url or self.url, stream=True)


def _chat_sse(*chunks: dict[str, Any]) -> httpx.Response:
    finish = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
    return compatible.sse_response(compatible.sse(*chunks, finish))


def _responses_stream(response: dict[str, Any]) -> httpx.Response:
    return openai.codex_sse_response(response)


_GEMINI_DONE = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}

_CHAT = _Path(
    lambda: compatible.make_adapter(
        model=compatible.catalog_model(levels=("low", "medium", "high"))
    ),
    compatible.OPENAI_URL,
    compatible.MODEL_ID,
    lambda: httpx.Response(200, json=compatible.SUCCESS_RESPONSE),
)
_MESSAGES = _Path(
    anthropic.make_adapter,
    anthropic.ANTHROPIC_URL,
    anthropic.MODEL_ID,
    lambda: httpx.Response(200, json=anthropic.SUCCESS_RESPONSE),
)
_PLATFORM = _Path(
    lambda: openai.platform_adapter(model_lookup=openai.bundled_model_lookup()),
    openai.PLATFORM_RESPONSES_URL,
    "gpt-6-sol",
    lambda: httpx.Response(200, json=openai.COMPLETED_RESPONSE),
)
_CODEX = _Path(
    lambda: openai.codex_adapter(model_lookup=openai.bundled_model_lookup()),
    openai.OPENAI_SUBSCRIPTION_URL,
    "gpt-6-sol",
    lambda: _responses_stream(openai.COMPLETED_RESPONSE),
)
_OPENROUTER = _Path(
    openrouter.openrouter_adapter,
    openrouter.RESPONSES_URL,
    openrouter.RESPONSES_MODEL,
    lambda: httpx.Response(200, json=openai.COMPLETED_RESPONSE),
)
_COPILOT_RESPONSES = _Path(
    copilot.make_adapter,
    copilot.RESPONSES_URL,
    "gpt-5-mini",
    lambda: httpx.Response(200, json={"output": []}),
)
_COPILOT_MESSAGES = _Path(
    lambda: copilot.make_adapter(
        metadata=copilot.copilot_metadata(
            "Anthropic", "claude-opus-4.8", ["/chat/completions", "/v1/messages"]
        )
    ),
    copilot.MESSAGES_URL,
    "claude-opus-4.8",
    lambda: httpx.Response(200, json={"content": []}),
)
_GO_RESPONSES = _Path(
    go.go_adapter,
    go.RESPONSES_URL,
    go.RESPONSES_MODEL,
    lambda: go.success_response("responses", streaming=False),
)
_GO_MESSAGES = _Path(
    go.go_adapter,
    go.MESSAGES_URL,
    go.MESSAGES_MODEL,
    lambda: go.success_response("messages", streaming=False),
)
_ZEN_GEMINI = _Path(
    zen.zen_adapter,
    zen.GEMINI_URL,
    zen.GEMINI_MODEL,
    lambda: httpx.Response(200, json=_GEMINI_DONE),
)


async def _request(adapter: Any, path: _Path, kwargs: dict[str, Any]) -> None:
    if path.stream:
        [_ async for _ in adapter.stream(MESSAGES, model_id=path.model_id, **kwargs)]
    else:
        await adapter.send(MESSAGES, model_id=path.model_id, **kwargs)


def _sent(request: httpx.Request, field: tuple[str, ...]) -> Any:
    value: Any = json.loads(request.content)
    for segment in field:
        if not isinstance(value, dict) or segment not in value:
            return ABSENT
        value = value[segment]
    return value


def _rejection(status: int, body: str | dict[str, Any]) -> httpx.Response:
    if isinstance(body, str):
        return httpx.Response(status, text=body)
    return httpx.Response(status, json=body)


def _error(message: str) -> dict[str, Any]:
    return {"error": {"type": "invalid_request_error", "message": message}}


_THINKING_OFF = {"type": "disabled"}


@pytest.mark.parametrize(
    ("path", "kwargs", "rejection", "field", "expected"),
    [
        pytest.param(
            _CHAT,
            {"thinking_effort": "high"},
            "invalid value for 'reasoning_effort': 'high'",
            ("reasoning_effort",),
            ["high", "medium", "medium"],
            id="chat-send-effort",
        ),
        pytest.param(
            _CHAT,
            {},
            "Unsupported parameter: 'temperature'",
            ("temperature",),
            [0.7, ABSENT, ABSENT],
            id="chat-send-parameter",
        ),
        pytest.param(
            _CHAT.streaming(_chat_sse),
            {},
            "Unsupported parameter: 'temperature'",
            ("temperature",),
            [0.7, ABSENT, ABSENT],
            id="chat-stream-parameter",
        ),
        pytest.param(
            _MESSAGES,
            {"temperature": 0.5, "thinking_effort": "none"},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.5, ABSENT, ABSENT],
            id="messages-send-parameter",
        ),
        pytest.param(
            _MESSAGES.streaming(lambda: anthropic.sse_response(anthropic.sse())),
            {"temperature": 0.5, "thinking_effort": "none"},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.5, ABSENT, ABSENT],
            id="messages-stream-parameter",
        ),
        pytest.param(
            _MESSAGES,
            {"thinking_effort": "none"},
            _error("thinking.type: 'disabled' is not supported for this model"),
            ("thinking",),
            [_THINKING_OFF, ABSENT, ABSENT],
            id="messages-send-off-switch",
        ),
        pytest.param(
            _PLATFORM,
            {"thinking_effort": "max"},
            {"error": {"message": "Invalid value for 'reasoning.effort': 'max'"}},
            ("reasoning", "effort"),
            ["max", "xhigh", "xhigh"],
            id="platform-send-effort",
        ),
        pytest.param(
            replace(_PLATFORM, model_id="gpt-5.5"),
            {"top_p": 0.9},
            {"error": {"message": "Unsupported parameter: 'top_p'"}},
            ("top_p",),
            [0.9, ABSENT, ABSENT],
            id="platform-send-parameter",
        ),
        pytest.param(
            _PLATFORM.streaming(lambda: _responses_stream(openai.COMPLETED_RESPONSE)),
            {"thinking_effort": "max"},
            {"error": {"message": "Invalid value for 'reasoning.effort': 'max'"}},
            ("reasoning", "effort"),
            ["max", "xhigh", "xhigh"],
            id="platform-stream-effort",
        ),
        pytest.param(
            _CODEX,
            {"thinking_effort": "max"},
            {"error": {"message": "Invalid value for 'reasoning.effort': 'max'"}},
            ("reasoning", "effort"),
            ["max", "xhigh", "xhigh"],
            id="codex-sse-send-effort",
        ),
        pytest.param(
            _OPENROUTER,
            {"thinking_effort": "high"},
            "Invalid value for 'reasoning.effort': 'high'",
            ("reasoning", "effort"),
            ["high", "medium", "medium"],
            id="openrouter-responses-send-effort",
        ),
        pytest.param(
            _OPENROUTER.streaming(lambda: _responses_stream(openai.COMPLETED_RESPONSE)),
            {"thinking_effort": "low"},
            "Invalid value for 'reasoning.effort': 'low'",
            ("reasoning", "effort"),
            ["low", "minimal", "minimal"],
            id="openrouter-responses-stream-effort",
        ),
        pytest.param(
            _COPILOT_RESPONSES,
            {"thinking_effort": "high"},
            _error("Invalid value for 'reasoning.effort': 'high'"),
            ("reasoning", "effort"),
            ["high", "medium", "medium"],
            id="copilot-responses-send-effort",
        ),
        pytest.param(
            _COPILOT_MESSAGES.streaming(
                lambda: copilot.sse_response(
                    copilot.sse_events(
                        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                        {"type": "message_stop"},
                    )
                )
            ),
            {"temperature": 0.4},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.4, ABSENT, ABSENT],
            id="copilot-messages-stream-parameter",
        ),
        pytest.param(
            # The bundled Model entry sends temperature; the retry still drops it,
            # but the explicit entry outranks the learned fact for later requests.
            replace(_COPILOT_MESSAGES, model_id="claude-sonnet-4.6"),
            {"temperature": 0.4},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.4, ABSENT, 0.4],
            id="model-entry-outranks-the-learned-parameter",
        ),
        pytest.param(
            _GO_RESPONSES.streaming(lambda: go.success_response("responses", streaming=True)),
            {"thinking_effort": "none"},
            "Invalid value for 'reasoning.effort': 'none'",
            ("reasoning",),
            [{"effort": "none", "summary": "auto"}, ABSENT, ABSENT],
            id="go-responses-stream-none-rung",
        ),
        pytest.param(
            _GO_MESSAGES,
            {"thinking_effort": "none"},
            "thinking type 'disabled' is not supported for minimax-m3",
            ("thinking",),
            [_THINKING_OFF, ABSENT, ABSENT],
            id="go-messages-send-off-switch",
        ),
        pytest.param(
            _ZEN_GEMINI,
            {"top_p": 0.9},
            "generationConfig.topP is not supported for this model",
            ("generationConfig", "topP"),
            [0.9, ABSENT, ABSENT],
            id="zen-gemini-send-parameter",
        ),
        pytest.param(
            _ZEN_GEMINI.streaming(
                lambda: httpx.Response(200, text=zen.gemini_sse(_GEMINI_DONE)),
                zen.GEMINI_STREAM_URL,
            ),
            {"thinking_effort": "low"},
            "thinking_level 'low' is not supported for this model",
            ("generationConfig", "thinkingConfig", "thinkingLevel"),
            ["low", "minimal", "minimal"],
            id="zen-gemini-stream-effort",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_rejection_is_learned_retried_and_remembered_for_later_requests(
    path: _Path,
    kwargs: dict[str, Any],
    rejection: str | dict[str, Any],
    field: tuple[str, ...],
    expected: list[Any],
) -> None:
    adapter = path.adapter()
    with respx.mock:
        route = respx.post(path.url).mock(
            side_effect=[_rejection(400, rejection), path.success(), path.success()]
        )
        for _ in range(2):
            await _request(adapter, path, kwargs)

    assert [_sent(call.request, field) for call in route.calls] == expected


@pytest.mark.parametrize(
    ("path", "kwargs", "status", "body", "error_type", "attempts"),
    [
        pytest.param(
            _CHAT,
            {},
            401,
            "Unsupported parameter: 'temperature'",
            ProviderAuthError,
            1,
            id="auth",
        ),
        pytest.param(
            _CHAT, {}, 503, "Unsupported parameter: 'temperature'", ProviderError, 4, id="retryable"
        ),
        pytest.param(
            _OPENROUTER,
            {"thinking_effort": "high"},
            400,
            {"error": {"code": 400, "message": "Invalid value for 'reasoning.effort': 'high'"}},
            ProviderError,
            4,
            id="lenient-retryable",
        ),
        pytest.param(
            _CHAT,
            {},
            400,
            "Unsupported parameter: 'top_k'",
            ProviderError,
            1,
            id="parameter-not-sent",
        ),
        pytest.param(
            _CHAT,
            {},
            400,
            "temperature must be at most 1.0",
            ProviderError,
            1,
            id="no-rejection-marker",
        ),
        pytest.param(
            _CHAT,
            {"thinking_effort": "high"},
            400,
            "invalid value for 'reasoning_effort'",
            ProviderError,
            1,
            id="effort-value-not-named",
        ),
        pytest.param(
            _MESSAGES,
            {"thinking_effort": "none"},
            400,
            _error("thinking is not supported for this model"),
            ProviderError,
            1,
            id="off-value-not-named",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_unattributable_rejection_passes_through_unchanged_and_teaches_nothing(
    path: _Path,
    kwargs: dict[str, Any],
    status: int,
    body: str | dict[str, Any],
    error_type: type[ProviderError],
    attempts: int,
) -> None:
    adapter = path.adapter()
    with respx.mock:
        route = respx.post(path.url).mock(
            side_effect=[*(_rejection(status, body) for _ in range(attempts)), path.success()]
        )
        with pytest.raises(ProviderError) as caught:
            await _request(adapter, path, kwargs)
        assert route.call_count == attempts
        await _request(adapter, path, kwargs)

    assert type(caught.value) is error_type
    detail = body if isinstance(body, str) else body["error"]["message"]
    assert detail in str(caught.value)
    first, *_, later = route.calls
    assert json.loads(later.request.content) == json.loads(first.request.content)


def _gemini_reply(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"candidates": [{"content": {"parts": list(parts)}, "finishReason": "STOP"}]}


_REASONING_ITEM = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [{"type": "summary_text", "text": "Checking."}],
    "encrypted_content": "opaque",
}


@pytest.mark.parametrize(
    ("path", "reply", "returned"),
    [
        pytest.param(
            _CHAT,
            lambda: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "ok",
                                "reasoning_content": "Checking.",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            ),
            True,
            id="chat-send",
        ),
        pytest.param(
            _CHAT,
            lambda: httpx.Response(200, json=compatible.SUCCESS_RESPONSE),
            False,
            id="chat-send-without-reasoning",
        ),
        pytest.param(
            _CHAT.streaming(_chat_sse),
            lambda: _chat_sse({"choices": [{"delta": {"reasoning_content": "Checking."}}]}),
            True,
            id="chat-stream",
        ),
        pytest.param(
            _MESSAGES,
            lambda: httpx.Response(
                200,
                json={
                    **anthropic.SUCCESS_RESPONSE,
                    "content": [anthropic.THINKING_BLOCK, {"type": "text", "text": "ok"}],
                },
            ),
            True,
            id="messages-send",
        ),
        pytest.param(
            _PLATFORM,
            lambda: httpx.Response(
                200, json={**openai.COMPLETED_RESPONSE, "output": [_REASONING_ITEM]}
            ),
            True,
            id="responses-send",
        ),
        pytest.param(
            _PLATFORM.streaming(lambda: _responses_stream(openai.COMPLETED_RESPONSE)),
            lambda: _responses_stream(openai.COMPLETED_RESPONSE),
            False,
            id="responses-stream-without-reasoning",
        ),
        pytest.param(
            _ZEN_GEMINI,
            lambda: httpx.Response(
                200,
                json=_gemini_reply({"text": "Checking.", "thought": True}, {"text": "ok"}),
            ),
            True,
            id="gemini-send",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_reply_with_reasoning_records_that_the_model_returned_reasoning(
    path: _Path, reply: Callable[[], httpx.Response], returned: bool
) -> None:
    adapter = path.adapter()
    with respx.mock:
        respx.post(path.url).mock(return_value=reply())
        await _request(adapter, path, {})

    wire = adapter.wire
    facts = wire.observations.facts_for(wire.provider_id, wire.connection_id, path.model_id)
    assert facts.reasoning_returned is returned
