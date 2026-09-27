"""OpenRouter error classification: router leniency on both Chat Completions and Responses."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.providers.errors import ProviderError, ProviderRateLimitError
from core.utils.retry import caller_owns_retries
from tests.core.providers.openrouter_test_support import (
    CHAT_URL,
    HELLO,
    RESPONSES_MODEL,
    RESPONSES_URL,
    chat_sse,
    openrouter_adapter,
)


@pytest.mark.parametrize(
    ("error", "error_class", "retryable"),
    [
        pytest.param(
            {
                "code": 400,
                "message": "assistant messages require content, reasoning, "
                "reasoning_meta, or tool_calls",
            },
            ProviderError,
            True,
            id="unclassified-becomes-retryable-for-re-routing",
        ),
        pytest.param(
            {"code": 400, "error_type": "context_length_exceeded", "message": "too long"},
            ProviderError,
            False,
            id="known-fatal-class-stays-fatal",
        ),
        pytest.param(
            {
                "code": 429,
                "message": "Rate limit exceeded",
                "metadata": {"error_type": "rate_limit_exceeded"},
            },
            ProviderRateLimitError,
            True,
            id="rate-limit",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_chat_stream_in_band_error_uses_router_leniency(
    error: dict[str, Any], error_class: type[ProviderError], retryable: bool
) -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=chat_sse(
            {"id": "chatcmpl-1", "choices": [{"delta": {"content": "Hi"}}]}, {"error": error}
        )
    )

    with pytest.raises(error_class) as caught:
        async for _ in openrouter_adapter().stream(HELLO, model_id="anthropic/claude-haiku"):
            pass

    assert caught.value.retryable is retryable
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("response", "error_class", "retryable", "retry_after"),
    [
        pytest.param(
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "message": "Reasoning is mandatory for this endpoint "
                        "and cannot be disabled.",
                        "metadata": {"provider_name": None},
                    }
                },
            ),
            ProviderError,
            True,
            None,
            id="structured-body-classifies-leniently",
        ),
        pytest.param(
            httpx.Response(400, text="plain bad request"),
            ProviderError,
            False,
            None,
            id="unstructured-body-keeps-shared-policy",
        ),
        pytest.param(
            httpx.Response(429, text="Rate limited", headers={"Retry-After": "7"}),
            ProviderRateLimitError,
            True,
            7,
            id="429-keeps-shared-policy-and-retry-after",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_chat_http_error_status_classification(
    response: httpx.Response,
    error_class: type[ProviderError],
    retryable: bool,
    retry_after: int | None,
) -> None:
    respx.post(CHAT_URL).mock(return_value=response)

    with caller_owns_retries(), pytest.raises(error_class) as caught:
        await openrouter_adapter().send(HELLO, model_id="stealth/ox-alpha")

    assert caught.value.retryable is retryable
    assert getattr(caught.value, "retry_after", None) == retry_after


@pytest.mark.parametrize(
    ("streaming", "error", "retryable"),
    [
        (False, {"code": 400, "message": "test upstream failure"}, True),
        (True, {"code": 400, "message": "test upstream failure"}, True),
        (True, {"code": "context_length_exceeded", "message": "test limit"}, False),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_responses_http_uses_router_error_policy(
    streaming: bool, error: dict[str, Any], retryable: bool
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(400, json={"error": error}))
    adapter = openrouter_adapter()

    with (
        pytest.raises(ProviderError) as caught,
    ):
        if streaming:
            async for _ in adapter.stream(HELLO, model_id=RESPONSES_MODEL):
                pass
        else:
            await adapter.send(HELLO, model_id=RESPONSES_MODEL)

    assert caught.value.retryable is retryable
    # A shared-policy 400 would be fatal; retries prove the router override ran.
    assert (route.call_count > 1) is retryable


@pytest.mark.parametrize(
    ("event_type", "error", "retryable", "retry_after"),
    [
        pytest.param(
            "error",
            {"error": {"code": "unrecognized", "message": "test unknown"}},
            True,
            None,
            id="unclassified-retryable",
        ),
        pytest.param(
            "response.failed",
            {"error": {"code": "context_length_exceeded"}, "availability": {"retryable": True}},
            False,
            None,
            id="fatal-class-beats-availability-hint",
        ),
        pytest.param(
            "response.failed",
            {
                "error": {"code": "unrecognized"},
                "availability": {"retryable": True, "retry_after": 7},
            },
            True,
            7,
            id="availability-retry-after-on-failed-envelope",
        ),
        pytest.param(
            "error",
            {"error_type": "provider_unavailable", "error": {"code": "context_length_exceeded"}},
            True,
            None,
            id="top-level-error-type-beats-nested-fatal-code",
        ),
        pytest.param(
            "response.error",
            {"error_type": "permission_denied", "error": {"code": "server_error"}},
            False,
            None,
            id="top-level-error-type-beats-nested-transient-code",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_responses_stream_uses_router_error_policy(
    event_type: str, error: dict[str, Any], retryable: bool, retry_after: int | None
) -> None:
    event = {
        "type": event_type,
        **({"response": error} if event_type == "response.failed" else error),
    }
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")
    )

    with pytest.raises(ProviderError) as caught:
        async for _ in openrouter_adapter().stream(HELLO, model_id=RESPONSES_MODEL):
            pass

    assert caught.value.retryable is retryable
    assert getattr(caught.value, "retry_after", None) == retry_after
    # The raw upstream payload stays visible in the error message.
    assert error["error"]["code"] in str(caught.value)
