"""Openrouter: errors behavior."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.errors import ProviderError, ProviderRateLimitError
from core.providers.openrouter import (
    OpenRouterAdapter,
)
from tests.core.providers.openrouter_helpers import (
    OPENROUTER_RESPONSES_URL,
    OPENROUTER_URL,
    SAMPLE_MESSAGES,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_adapter as openrouter_adapter,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_config as openrouter_config,
)


# In-band and HTTP error classification (router leniency)
def _error_sse_body(error_payload: dict[str, Any]) -> str:
    return (
        'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
        f"data: {json.dumps({'error': error_payload})}\n\n"
        "data: [DONE]\n\n"
    )


@respx.mock
@pytest.mark.asyncio
async def test_stream_unknown_in_band_error_is_retryable(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """An unclassified in-band error becomes retryable so Chat can re-route."""

    # Arrange
    route = respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(
            200,
            text=_error_sse_body(
                {
                    "code": 400,
                    "message": "assistant messages require content, reasoning, "
                    "reasoning_meta, or tool_calls",
                }
            ),
            headers={"content-type": "text/event-stream"},
        )
    )

    # Act / Assert
    with pytest.raises(ProviderError) as exc_info:
        async for _ in openrouter_adapter.stream(
            SAMPLE_MESSAGES, model_id="anthropic/claude-haiku"
        ):
            pass
    assert exc_info.value.retryable is True
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_stream_fatal_in_band_error_stays_non_retryable(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """Known-fatal in-band classes stay fatal despite router leniency."""

    # Arrange
    respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(
            200,
            text=_error_sse_body(
                {"code": 400, "error_type": "context_length_exceeded", "message": "too long"}
            ),
            headers={"content-type": "text/event-stream"},
        )
    )

    # Act / Assert
    with pytest.raises(ProviderError) as exc_info:
        async for _ in openrouter_adapter.stream(
            SAMPLE_MESSAGES, model_id="anthropic/claude-haiku"
        ):
            pass
    assert exc_info.value.retryable is False


@respx.mock
@pytest.mark.asyncio
async def test_stream_rate_limit_in_band_error_maps_to_rate_limit_error(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """A structured rate-limit error chunk maps into ProviderRateLimitError."""

    # Arrange
    respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(
            200,
            text=_error_sse_body(
                {
                    "code": 429,
                    "message": "Rate limit exceeded",
                    "metadata": {"error_type": "rate_limit_exceeded"},
                }
            ),
            headers={"content-type": "text/event-stream"},
        )
    )

    # Act / Assert
    with pytest.raises(ProviderRateLimitError):
        async for _ in openrouter_adapter.stream(
            SAMPLE_MESSAGES, model_id="anthropic/claude-haiku"
        ):
            pass


@respx.mock
@pytest.mark.asyncio
async def test_send_structured_400_body_becomes_retryable_leniently(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """A structured OpenRouter error body on HTTP 400 classifies leniently."""

    # Arrange
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "Reasoning is mandatory for this endpoint and cannot be disabled.",
                "metadata": {"provider_name": None},
            }
        }
    )
    respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(400, text=body))

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ProviderError) as exc_info,
    ):
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="stealth/ox-alpha")
    assert exc_info.value.retryable is True


@respx.mock
@pytest.mark.asyncio
async def test_send_unstructured_400_body_keeps_shared_policy(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """Without a parseable structured body, the shared status policy applies."""

    # Arrange
    respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(400, text="plain bad request"))

    # Act / Assert
    with pytest.raises(ProviderError) as exc_info:
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="stealth/ox-alpha")
    assert exc_info.value.retryable is False


@respx.mock
@pytest.mark.asyncio
async def test_send_429_delegates_to_shared_policy_with_retry_after(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """HTTP 429 keeps the shared classification including its Retry-After hint."""

    # Arrange
    respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(429, text="Rate limited", headers={"Retry-After": "7"})
    )

    # Act / Assert
    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ProviderRateLimitError) as exc_info,
    ):
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="stealth/ox-alpha")
    assert exc_info.value.retry_after == 7


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "error,retryable",
    [
        ({"code": 400, "message": "test upstream failure"}, True),
        ({"code": "context_length_exceeded", "message": "test limit"}, False),
    ],
)
async def test_responses_http_uses_router_error_policy(
    openrouter_adapter, streaming, error, retryable
):
    route = respx.post(OPENROUTER_RESPONSES_URL).mock(
        return_value=httpx.Response(400, json={"error": error})
    )
    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ProviderError) as caught,
    ):
        if streaming:
            async for _ in openrouter_adapter.stream(
                SAMPLE_MESSAGES, model_id="openai/gpt-5.6-sol"
            ):
                pass
        else:
            await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.6-sol")
    assert caught.value.retryable is retryable
    assert (route.call_count > 1) is retryable


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["error", "response.error", "response.failed"])
@pytest.mark.parametrize(
    "error,retryable,retry_after",
    [
        ({"error": {"code": "unrecognized", "message": "test unknown"}}, True, None),
        (
            {"error": {"code": "context_length_exceeded"}, "availability": {"retryable": True}},
            False,
            None,
        ),
        (
            {
                "error": {"code": "unrecognized"},
                "availability": {"retryable": True, "retry_after": 7},
            },
            True,
            7,
        ),
        (
            {"error_type": "provider_unavailable", "error": {"code": "context_length_exceeded"}},
            True,
            None,
        ),
        ({"error_type": "permission_denied", "error": {"code": "server_error"}}, False, None),
    ],
)
async def test_responses_stream_uses_router_error_policy(
    openrouter_adapter, event_type, error, retryable, retry_after
):
    event = {
        "type": event_type,
        **({"response": error} if event_type == "response.failed" else error),
    }
    respx.post(OPENROUTER_RESPONSES_URL).mock(
        return_value=httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")
    )
    with pytest.raises(ProviderError) as caught:
        async for _ in openrouter_adapter.stream(SAMPLE_MESSAGES, model_id="openai/gpt-5.6-sol"):
            pass
    assert caught.value.retryable is retryable
    assert getattr(caught.value, "retry_after", None) == retry_after
    assert error["error"]["code"] in str(caught.value)
