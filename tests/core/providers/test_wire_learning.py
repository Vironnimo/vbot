"""Wire facts learned from live traffic, on every Adapter request path.

Each case runs one request path (Adapter, wire, send or stream) against a mocked
endpoint. A rejection the request can be blamed for is learned, retried once in
the learned shape and remembered by later requests; every other error passes
through unchanged and teaches nothing. A reply that carries reasoning records
that the Model returned reasoning, and that it ignores reasoning off when the
request turned reasoning off.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, override

import httpx
import pytest
import respx

from core.providers.errors import (
    ProviderAuthError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderImageLimitError,
)
from core.providers.providers import REQUEST_ESTIMATE_MIN_MARGIN_TOKENS
from core.utils.retry import caller_owns_retries

from . import anthropic_test_support as anthropic
from . import openai_compatible_test_support as compatible
from . import openai_test_support as openai
from . import opencode_go_test_support as go
from . import openrouter_test_support as openrouter
from . import wire_paths_test_support as paths

ABSENT = object()


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


class _Within:
    """Equals an output allowance clamped into a ``window``, its reserve included."""

    def __init__(self, window: int) -> None:
        self.window = window

    @override
    def __eq__(self, value: object) -> bool:
        return (
            isinstance(value, int)
            and self.window // 2 < value <= self.window - REQUEST_ESTIMATE_MIN_MARGIN_TOKENS
        )

    @override
    def __repr__(self) -> str:
        return f"<output within {self.window}>"


_WITHIN_4096 = _Within(4096)


@pytest.mark.parametrize(
    ("path", "rejection", "tool_result"),
    [
        (paths.CHAT, "a request may include at most 2 images", False),
        (paths.CHAT.streaming(paths.chat_sse), "Too many images in request: 3 > 2", False),
        (
            paths.MESSAGES,
            "request contains 3 images, exceeding the maximum of 2 allowed per request",
            True,
        ),
        (paths.GO_RESPONSES, "Too many images: max 2 images per request, got 3", False),
        (paths.ZEN_GEMINI_STREAM, "Exceeded maximum number of images (2)", False),
        pytest.param(
            paths.PLATFORM,
            "Too many images in request: 3, maximum allowed: 2.",
            False,
            id="azure-comma-maximum",
        ),
        pytest.param(
            paths.CHAT,
            "Too many images in request. Max is 2.",
            False,
            id="azure-older-maximum",
        ),
        pytest.param(
            paths.CHAT,
            "At most 2 image(s) may be provided in one request.",
            False,
            id="vllm-request",
        ),
        pytest.param(
            paths.CHAT,
            "At most 2 image(s) may be provided in one prompt. "
            "Set `--limit-mm-per-prompt` to increase this limit.",
            False,
            id="vllm-prompt",
        ),
        pytest.param(
            paths.CHAT,
            "Too many images provided. This model supports up to 2 images",
            False,
            id="groq",
        ),
        pytest.param(
            paths.CHAT,
            "The model returned the following errors: Too many input images: 3, max allowed: 2",
            False,
            id="bedrock-llama-via-compatible-route",
        ),
        pytest.param(
            paths.ZEN_GEMINI_STREAM,
            "too many images: maximum allowed for model gemini-3.1-pro-preview is 2, got 3",
            False,
            id="gemini-model-maximum",
        ),
        pytest.param(
            paths.MESSAGES.streaming(lambda: anthropic.sse_response(anthropic.sse())),
            "Too much media: 0 document pages + 3 images > 2",
            True,
            id="anthropic-zero-pages",
        ),
        pytest.param(
            paths.COPILOT_MESSAGES,
            "too many images and documents: 3 + 0 > 2",
            True,
            id="anthropic-zero-documents-via-copilot",
        ),
        pytest.param(
            paths.OPENROUTER,
            {
                "error": {
                    "code": 400,
                    "message": "Provider returned error",
                    "metadata": {
                        "raw": json.dumps(_error("Exceeded maximum number of images (2)")),
                    },
                }
            },
            False,
            id="openrouter-nested-upstream-error",
        ),
        pytest.param(
            paths.OPENROUTER.streaming(lambda: paths.responses_sse()),
            {
                "error": {
                    "code": 400,
                    "message": "Provider returned error",
                    "metadata": {"raw": "Too many images in request: 3, maximum allowed: 2."},
                }
            },
            False,
            id="openrouter-in-band-upstream-error",
        ),
        (paths.OLLAMA, "a request may include at most 2 images", False),
        (
            paths.OLLAMA.streaming(paths.OLLAMA.success),
            "a request may include at most 2 images",
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_image_count_rejections_are_learned_and_handed_to_chat_without_replay(
    path: paths.RequestPath,
    rejection: str | dict[str, Any],
    tool_result: bool,
) -> None:
    from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD

    images = [{"type": "media", "media_type": "image/png", "base64": "aW1hZ2U="} for _ in range(3)]
    messages = [{"role": "user", "content": images}]
    if tool_result:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "read-images", "name": "read", "arguments": {}}],
            },
            {
                "role": "tool",
                "tool_call_id": "read-images",
                "content": "images",
                TOOL_RESULT_CONTENT_BLOCKS_FIELD: images,
            },
        ]
    adapter = path.adapter()
    before = adapter.request_image_limit(path.model_id)
    with respx.mock, caller_owns_retries():
        body = _error(rejection) if isinstance(rejection, str) else rejection
        response = (
            httpx.Response(
                200,
                text=": heartbeat\n\n"
                + paths.chat_sse(
                    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 0}}, body
                ).text,
            )
            if path.stream and path.url == paths.CHAT.url
            else httpx.Response(200, text=json.dumps({"error": rejection}) + "\n")
            if path.stream and path.url == paths.OLLAMA.url
            else httpx.Response(200, text="data: " + json.dumps({"type": "error", **body}) + "\n\n")
            if path.stream and path.url in {paths.MESSAGES.url, paths.OPENROUTER.url}
            else _rejection(422 if "one prompt" in str(rejection) else 400, body)
        )
        route = respx.post(path.url).mock(return_value=response)
        with pytest.raises(ProviderImageLimitError) as failure:
            await paths.request(adapter, path, {}, messages)
        assert len(route.calls) == 1
    assert failure.value.max_images == 2
    assert failure.value.retryable is False
    assert before != 2
    assert adapter.request_image_limit(path.model_id) == 2
    assert adapter.request_image_limit("gpt-6-astra") != 2
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "detail", "status"),
    [
        ("within-limit", "a request may include at most 2 images", 400),
        ("image-size", "each image may include at most 2 megabytes", 400),
        ("auth", "a request may include at most 2 images", 401),
        ("after-output", "a request may include at most 2 images", 400),
        ("after-reasoning", "a request may include at most 2 images", 400),
        ("after-tool-call", "a request may include at most 2 images", 400),
        ("rate-limit", "Too many images provided. This model supports up to 2 images", 429),
        ("server-error", "Too many images in request: 3, maximum allowed: 2.", 503),
        ("in-band-server", "Too many images in request: 3, maximum allowed: 2.", 503),
        ("in-band-rate", "Too many images in request: 3, maximum allowed: 2.", 429),
        ("in-band-auth", "Too many images in request: 3, maximum allowed: 2.", 401),
        ("in-band-payment", "Too many images in request: 3, maximum allowed: 2.", 402),
        ("document-pages", "Too much media: 1 document pages + 3 images > 2", 400),
        ("documents", "too many images and documents: 3 + 1 > 2", 400),
        ("vague", "Too many images", 400),
        ("ambiguous", "Request rejected (too many or oversized images)", 400),
        ("per-message", "At most 2 image(s) may be provided in one message.", 400),
        (
            "per-minute",
            "Too many images provided. This model supports up to 2 images per minute",
            400,
        ),
        ("video-count", "At most 2 video(s) may be provided in one prompt.", 400),
        ("zero-limit", "At most 0 image(s) may be provided in one prompt.", 400),
        ("decimal-limit", "Too many images in request: 3 > 2.5", 400),
        ("grouped-number", "Too many images in request: 3 > 2,000", 400),
        ("pixels", "Image dimensions exceed the maximum allowed size: 3 > 2", 400),
        ("no-images", "Too many images in request: 3, maximum allowed: 2.", 400),
    ],
)
async def test_image_limit_learning_requires_an_exceeded_count_before_output(
    scenario: str, detail: str, status: int
) -> None:
    path = (
        paths.OPENROUTER.streaming(lambda: paths.responses_sse())
        if scenario.startswith("in-band-")
        else paths.CHAT
    )
    adapter = path.adapter()
    before = adapter.request_image_limit(path.model_id)
    image = {"type": "media", "media_type": "image/png", "base64": "aW1hZ2U="}
    messages = [{"role": "user", "content": [image] * (2 if scenario == "within-limit" else 3)}]
    if scenario == "no-images":
        messages = [{"role": "user", "content": json.dumps(messages)}]
    response = _rejection(status, _error(detail))
    if scenario.startswith("in-band-"):
        event = {"type": "error", "error": {"code": status, "message": detail}}
        response = httpx.Response(200, text="data: " + json.dumps(event) + "\n\n")
    elif scenario.startswith("after-"):
        path = path.streaming(paths.chat_sse)
        delta: dict[str, Any] = {"content": "Already answered"}
        if scenario == "after-reasoning":
            delta = {"reasoning_content": "Already thinking"}
        elif scenario == "after-tool-call":
            delta = {
                "tool_calls": [
                    {"index": 0, "id": "call-1", "function": {"name": "read", "arguments": "{}"}}
                ]
            }
        response = paths.chat_sse({"choices": [{"delta": delta}]}, _error(detail))
    with respx.mock, caller_owns_retries():
        route = respx.post(path.url).mock(return_value=response)
        with pytest.raises(ProviderError) as failure:
            await paths.request(adapter, path, {}, messages)
        assert route.call_count == 1
    assert not isinstance(failure.value, ProviderImageLimitError)
    assert adapter.request_image_limit(path.model_id) == before
    await adapter.aclose()


@pytest.mark.parametrize(
    ("path", "kwargs", "rejection", "field", "expected"),
    [
        pytest.param(
            paths.CHAT,
            {"thinking_effort": "high"},
            "invalid value for 'reasoning_effort': 'high'",
            ("reasoning_effort",),
            ["high", "medium", "medium"],
            id="chat-send-effort",
        ),
        pytest.param(
            paths.CHAT,
            {},
            "Unsupported parameter: 'temperature'",
            ("temperature",),
            [0.7, ABSENT, ABSENT],
            id="chat-send-parameter",
        ),
        pytest.param(
            paths.CHAT.streaming(paths.chat_sse),
            {},
            "Unsupported parameter: 'temperature'",
            ("temperature",),
            [0.7, ABSENT, ABSENT],
            id="chat-stream-parameter",
        ),
        pytest.param(
            # Of a combination the wire refuses, temperature stays and top_p goes.
            paths.CHAT,
            {"top_p": 0.9},
            "`temperature` and `top_p` cannot both be specified for this model.",
            ("top_p",),
            [0.9, ABSENT, ABSENT],
            id="chat-send-combination",
        ),
        pytest.param(
            paths.MESSAGES,
            {"temperature": 0.5, "thinking_effort": "none"},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.5, ABSENT, ABSENT],
            id="messages-send-parameter",
        ),
        pytest.param(
            paths.MESSAGES.streaming(lambda: anthropic.sse_response(anthropic.sse())),
            {"temperature": 0.5, "thinking_effort": "none"},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.5, ABSENT, ABSENT],
            id="messages-stream-parameter",
        ),
        pytest.param(
            # Anthropic's wording for Models that removed sampling (Claude Haiku 5.5).
            paths.MESSAGES,
            {"temperature": 0.5, "thinking_effort": "none"},
            _error("`temperature` is deprecated for this model."),
            ("temperature",),
            [0.5, ABSENT, ABSENT],
            id="messages-send-deprecated-parameter",
        ),
        pytest.param(
            paths.MESSAGES,
            {"thinking_effort": "none"},
            _error("thinking.type: 'disabled' is not supported for this model"),
            ("thinking",),
            [_THINKING_OFF, ABSENT, ABSENT],
            id="messages-send-off-switch",
        ),
        pytest.param(
            paths.PLATFORM,
            {"thinking_effort": "max"},
            {"error": {"message": "Invalid value for 'reasoning.effort': 'max'"}},
            ("reasoning", "effort"),
            ["max", "xhigh", "xhigh"],
            id="platform-send-effort",
        ),
        pytest.param(
            replace(paths.PLATFORM, model_id="gpt-5.5"),
            {"top_p": 0.9},
            {"error": {"message": "Unsupported parameter: 'top_p'"}},
            ("top_p",),
            [0.9, ABSENT, ABSENT],
            id="platform-send-parameter",
        ),
        pytest.param(
            paths.PLATFORM.streaming(lambda: paths.responses_sse()),
            {"thinking_effort": "max"},
            {"error": {"message": "Invalid value for 'reasoning.effort': 'max'"}},
            ("reasoning", "effort"),
            ["max", "xhigh", "xhigh"],
            id="platform-stream-effort",
        ),
        pytest.param(
            paths.CODEX,
            {"thinking_effort": "max"},
            {"error": {"message": "Invalid value for 'reasoning.effort': 'max'"}},
            ("reasoning", "effort"),
            ["max", "xhigh", "xhigh"],
            id="codex-sse-send-effort",
        ),
        pytest.param(
            paths.OPENROUTER,
            {"thinking_effort": "high"},
            "Invalid value for 'reasoning.effort': 'high'",
            ("reasoning", "effort"),
            ["high", "medium", "medium"],
            id="openrouter-responses-send-effort",
        ),
        pytest.param(
            paths.OPENROUTER.streaming(lambda: paths.responses_sse()),
            {"thinking_effort": "low"},
            "Invalid value for 'reasoning.effort': 'low'",
            ("reasoning", "effort"),
            ["low", "minimal", "minimal"],
            id="openrouter-responses-stream-effort",
        ),
        pytest.param(
            paths.COPILOT_RESPONSES,
            {"thinking_effort": "high"},
            _error("Invalid value for 'reasoning.effort': 'high'"),
            ("reasoning", "effort"),
            ["high", "medium", "medium"],
            id="copilot-responses-send-effort",
        ),
        pytest.param(
            paths.COPILOT_MESSAGES_STREAM,
            {"temperature": 0.4},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.4, ABSENT, ABSENT],
            id="copilot-messages-stream-parameter",
        ),
        pytest.param(
            # The bundled Model entry sends temperature; the retry still drops it,
            # but the explicit entry outranks the learned fact for later requests.
            replace(paths.COPILOT_MESSAGES, model_id="claude-sonnet-4.6"),
            {"temperature": 0.4},
            _error("temperature is not supported for this model"),
            ("temperature",),
            [0.4, ABSENT, 0.4],
            id="model-entry-outranks-the-learned-parameter",
        ),
        pytest.param(
            paths.GO_RESPONSES.streaming(lambda: go.success_response("responses", streaming=True)),
            {"thinking_effort": "none"},
            "Invalid value for 'reasoning.effort': 'none'",
            ("reasoning",),
            [{"effort": "none", "summary": "auto"}, ABSENT, ABSENT],
            id="go-responses-stream-none-rung",
        ),
        pytest.param(
            paths.GO_MESSAGES,
            {"thinking_effort": "none"},
            "thinking type 'disabled' is not supported for minimax-m3",
            ("thinking",),
            [_THINKING_OFF, ABSENT, ABSENT],
            id="go-messages-send-off-switch",
        ),
        pytest.param(
            paths.ZEN_GEMINI,
            {"top_p": 0.9},
            "generationConfig.topP is not supported for this model",
            ("generationConfig", "topP"),
            [0.9, ABSENT, ABSENT],
            id="zen-gemini-send-parameter",
        ),
        pytest.param(
            paths.ZEN_GEMINI_STREAM,
            {"thinking_effort": "low"},
            "thinking_level 'low' is not supported for this model",
            ("generationConfig", "thinkingConfig", "thinkingLevel"),
            ["low", "minimal", "minimal"],
            id="zen-gemini-stream-effort",
        ),
        pytest.param(
            paths.CHAT,
            {"max_tokens": 9000},
            "max_tokens is too large: 9000. This model supports at most 4000 completion tokens.",
            ("max_tokens",),
            [9000, 4000, 4000],
            id="chat-send-output-maximum",
        ),
        pytest.param(
            paths.CHAT,
            {"max_tokens": 9000},
            "Invalid max_tokens value, the valid range of max_tokens is [1, 4000]",
            ("max_tokens",),
            [9000, 4000, 4000],
            id="chat-send-output-range",
        ),
        pytest.param(
            paths.MESSAGES,
            {"max_tokens": 9000, "thinking_effort": "none"},
            _error(
                "max_tokens: 9000 > 4000, which is the maximum allowed number of output "
                "tokens for claude-test"
            ),
            ("max_tokens",),
            [9000, 4000, 4000],
            id="messages-send-output-maximum",
        ),
        pytest.param(
            paths.CHAT.streaming(paths.chat_sse),
            {"max_tokens": 9000},
            "This model's maximum context length is 4096 tokens. However, you requested 9010 "
            "tokens (10 in the messages, 9000 in the completion).",
            ("max_tokens",),
            [9000, _WITHIN_4096, _WITHIN_4096],
            id="chat-stream-context-window",
        ),
        pytest.param(
            paths.CHAT,
            {"max_tokens": 9000},
            "Requested token count exceeds the model's maximum context length of 4096 tokens.",
            ("max_tokens",),
            [9000, _WITHIN_4096, _WITHIN_4096],
            id="chat-send-sglang-context-window",
        ),
        pytest.param(
            paths.CHAT,
            {"max_tokens": 9000},
            {
                "error": {
                    "code": 400,
                    "message": "the request exceeds the available context size, try increasing it",
                    "n_ctx": 4096,
                    "n_prompt_tokens": 9010,
                    "type": "exceed_context_size_error",
                }
            },
            ("max_tokens",),
            [9000, _WITHIN_4096, _WITHIN_4096],
            id="chat-send-llama-cpp-context-window",
        ),
        pytest.param(
            paths.MESSAGES,
            {"max_tokens": 9000, "thinking_effort": "none"},
            _error(
                "input length and `max_tokens` exceed context limit: 10 + 9000 > 4096, "
                "decrease input length or `max_tokens` and try again"
            ),
            ("max_tokens",),
            [9000, _WITHIN_4096, _WITHIN_4096],
            id="messages-send-context-window",
        ),
        pytest.param(
            # OpenRouter reports the limit of the endpoint it routed to as a
            # retryable error; the stated number is learned all the same.
            replace(
                paths.OPENROUTER,
                url=openrouter.CHAT_URL,
                model_id="meta-llama/llama-4-scout",
                success=lambda: httpx.Response(200, json=openrouter.CHAT_SUCCESS),
            ),
            {"max_tokens": 9000},
            {
                "error": {
                    "code": 400,
                    "message": "This endpoint's maximum context length is 4096 tokens. However, "
                    "you requested about 9010 tokens (10 of text input, 9000 in the output).",
                }
            },
            ("max_tokens",),
            [9000, _WITHIN_4096, _WITHIN_4096],
            id="openrouter-chat-send-context-window",
        ),
        pytest.param(
            paths.OPENROUTER,
            {"max_tokens": 9000},
            "max_tokens is too large: 9000. This model supports at most 4000 completion tokens.",
            ("max_output_tokens",),
            [9000, 4000, 4000],
            id="openrouter-responses-send-output-maximum",
        ),
        pytest.param(
            paths.OPENROUTER.streaming(lambda: paths.responses_sse()),
            {"max_tokens": 9000},
            "This model's maximum context length is 4096 tokens. However, you requested 9010 "
            "tokens (10 in the messages, 9000 in the completion).",
            ("max_output_tokens",),
            [9000, _WITHIN_4096, _WITHIN_4096],
            id="openrouter-responses-stream-context-window",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_rejection_is_learned_retried_and_remembered_for_later_requests(
    path: paths.RequestPath,
    kwargs: dict[str, Any],
    rejection: str | dict[str, Any],
    field: tuple[str, ...],
    expected: list[Any],
) -> None:
    adapter = path.adapter()
    # Like Chat, the caller owns retries: a retryable rejection reaches the learner at once.
    with respx.mock, caller_owns_retries():
        route = respx.post(path.url).mock(
            side_effect=[_rejection(400, rejection), path.success(), path.success()]
        )
        for _ in range(2):
            await paths.request(adapter, path, kwargs)

    assert [_sent(call.request, field) for call in route.calls] == expected
    # A window a rejection stated is the one the Provider reports; Chat's Context
    # capacity budgets against it too.
    stated_window = next((value.window for value in expected if isinstance(value, _Within)), None)
    assert adapter.reported_context_window(path.model_id) == stated_window


@pytest.mark.parametrize(
    ("path", "kwargs", "status", "body", "error_type", "attempts"),
    [
        pytest.param(
            paths.CHAT,
            {},
            401,
            "Unsupported parameter: 'temperature'",
            ProviderAuthError,
            1,
            id="auth",
        ),
        pytest.param(
            paths.CHAT,
            {},
            503,
            "Unsupported parameter: 'temperature'",
            ProviderError,
            4,
            id="retryable",
        ),
        pytest.param(
            paths.OPENROUTER,
            {"thinking_effort": "high"},
            400,
            {"error": {"code": 400, "message": "Invalid value for 'reasoning.effort': 'high'"}},
            ProviderError,
            4,
            id="lenient-retryable",
        ),
        pytest.param(
            paths.CHAT,
            {},
            400,
            "Unsupported parameter: 'top_k'",
            ProviderError,
            1,
            id="parameter-not-sent",
        ),
        pytest.param(
            paths.CHAT,
            {},
            400,
            "temperature must be at most 1.0",
            ProviderError,
            1,
            id="no-rejection-marker",
        ),
        pytest.param(
            paths.CHAT,
            {"thinking_effort": "high"},
            400,
            "invalid value for 'reasoning_effort'",
            ProviderError,
            1,
            id="effort-value-not-named",
        ),
        pytest.param(
            paths.MESSAGES,
            {"thinking_effort": "none"},
            400,
            _error("thinking is not supported for this model"),
            ProviderError,
            1,
            id="off-value-not-named",
        ),
        pytest.param(
            paths.CHAT,
            {"max_tokens": 3000},
            400,
            "max_tokens is too large: 3000. This model supports at most 4000 completion tokens.",
            ProviderError,
            1,
            id="output-maximum-not-exceeded",
        ),
        pytest.param(
            paths.CHAT,
            {"max_tokens": 9000},
            400,
            "Your input exceeds the context window of this model. Please adjust your input "
            "and try again.",
            ProviderContextOverflowError,
            1,
            id="context-window-not-stated",
        ),
        pytest.param(
            paths.CHAT,
            {},
            400,
            "a request may include at most 2 images",
            ProviderError,
            1,
            id="image-limit-without-images",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_unattributable_rejection_passes_through_unchanged_and_teaches_nothing(
    path: paths.RequestPath,
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
            await paths.request(adapter, path, kwargs)
        assert route.call_count == attempts
        await paths.request(adapter, path, kwargs)

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
            paths.CHAT,
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
            paths.CHAT,
            lambda: httpx.Response(200, json=compatible.SUCCESS_RESPONSE),
            False,
            id="chat-send-without-reasoning",
        ),
        pytest.param(
            paths.CHAT.streaming(paths.chat_sse),
            lambda: paths.chat_sse({"choices": [{"delta": {"reasoning_content": "Checking."}}]}),
            True,
            id="chat-stream",
        ),
        pytest.param(
            paths.MESSAGES,
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
            paths.PLATFORM,
            lambda: httpx.Response(
                200, json={**openai.COMPLETED_RESPONSE, "output": [_REASONING_ITEM]}
            ),
            True,
            id="responses-send",
        ),
        pytest.param(
            paths.PLATFORM.streaming(lambda: paths.responses_sse()),
            lambda: paths.responses_sse(),
            False,
            id="responses-stream-without-reasoning",
        ),
        pytest.param(
            paths.ZEN_GEMINI,
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
    path: paths.RequestPath, reply: Callable[[], httpx.Response], returned: bool
) -> None:
    adapter = path.adapter()
    with respx.mock:
        respx.post(path.url).mock(return_value=reply())
        await paths.request(adapter, path, {})

    wire = adapter.wire
    facts = wire.observations.facts_for(wire.provider_id, wire.connection_id, path.model_id)
    assert facts.reasoning_returned is returned


_OFF_RUNG_CHAT = replace(
    paths.CHAT,
    adapter=lambda: compatible.make_adapter(
        model=compatible.catalog_model(levels=("none", "low", "high"))
    ),
).streaming(paths.chat_sse)


@pytest.mark.parametrize(
    ("effort", "chunk", "ignored"),
    [
        pytest.param(
            "none",
            {"choices": [{"delta": {"reasoning_content": "Checking."}}]},
            True,
            id="reasoning-field",
        ),
        pytest.param(
            "none",
            {"choices": [{"delta": {"content": "Checking.</think>"}}]},
            True,
            id="inline-after-template-opened-block",
        ),
        pytest.param(
            "none",
            {"choices": [{"delta": {"content": "Wrap it in <think>tags</think>."}}]},
            False,
            id="tag-literal-in-answer",
        ),
        pytest.param(
            "none",
            {"choices": [{"delta": {"content": "The delimiter is `</think>`."}}]},
            False,
            id="closing-tag-in-code-example",
        ),
        pytest.param(
            "none", {"choices": [{"delta": {"content": "Plain."}}]}, False, id="no-reasoning"
        ),
        pytest.param(
            "low",
            {"choices": [{"delta": {"reasoning_content": "Checking."}}]},
            False,
            id="reasoning-requested",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reasoning_despite_off_teaches_effort_none_to_send_the_lowest_level(
    effort: str, chunk: dict[str, Any], ignored: bool
) -> None:
    adapter = _OFF_RUNG_CHAT.adapter()
    with respx.mock:
        route = respx.post(_OFF_RUNG_CHAT.url).mock(
            side_effect=[paths.chat_sse(chunk), paths.chat_sse()]
        )
        for _ in range(2):
            await paths.request(adapter, _OFF_RUNG_CHAT, {"thinking_effort": effort})

    wire = adapter.wire
    facts = wire.observations.facts_for(wire.provider_id, wire.connection_id, paths.CHAT.model_id)
    assert facts.off_ignored is ignored
    assert [_sent(call.request, ("reasoning_effort",)) for call in route.calls] == [
        effort,
        "low" if ignored else effort,
    ]
