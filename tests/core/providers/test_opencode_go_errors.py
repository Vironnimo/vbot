"""OpenCode Go error policy: observed upstream failures, subscription limits and auth failures."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import Any, override
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OPENCODE_SESSION_HEADER, OpenCodeGoAdapter
from core.utils.retry import MAX_RETRIES, RetryNotice, caller_owns_retries, observe_retries

from .opencode_go_test_support import (
    API_KEY,
    CHAT_MODEL,
    CHAT_URL,
    GEMINI_STREAM_URL,
    HELLO,
    RESPONSES_MODEL,
    RESPONSES_URL,
    WIRES,
    go_adapter,
    go_config,
    go_request,
    success_response,
)

UPSTREAM_JSON_FAILURE = {
    "error": {
        "type": "server_error",
        "code": "server_error",
        "message": "Upstream request failed: [server_error] Upstream response was not valid JSON",
    }
}
UPSTREAM_THINKING_FAILURE = {
    "error": {
        "type": "server_error",
        "message": "Upstream request failed: [unsupported_request] "
        "The thinking request field is not enabled for this endpoint.",
    }
}
UPSTREAM_FAILURES = {
    "403-invalid-json": (403, UPSTREAM_JSON_FAILURE),
    "400-thinking-endpoint": (400, UPSTREAM_THINKING_FAILURE),
}
_WIRE_IDS = list(WIRES)


def _affinity(adapter: OpenCodeGoAdapter) -> dict[str, Any]:
    return adapter.request_context_kwargs(
        agent_id="test-agent", session_id="test-session", prompt_cache_affinity_id="retry-affinity"
    )


@pytest.mark.parametrize("failure", list(UPSTREAM_FAILURES))
@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.parametrize("wire", _WIRE_IDS)
@pytest.mark.asyncio
async def test_observed_upstream_failure_retries_the_identical_request(
    wire: str, streaming: bool, failure: str
) -> None:
    """An observed gateway failure keeps its status, body and Retry-After and is retried."""
    model_id, url = WIRES[wire]
    status, body = UPSTREAM_FAILURES[failure]
    adapter = go_adapter()
    notices: list[RetryNotice] = []

    with (
        respx.mock,
        observe_retries(notices.append),
        patch("core.utils.retry._sleep", new_callable=AsyncMock) as sleep,
    ):
        route = respx.post(GEMINI_STREAM_URL if wire == "gemini" and streaming else url).mock(
            side_effect=[
                httpx.Response(status, json=body, headers={"retry-after": "7"}),
                success_response(wire, streaming=streaming),
            ]
        )
        deltas = await go_request(adapter, model_id, streaming=streaming, **_affinity(adapter))

    assert not streaming or any(delta["type"] == "finish" for delta in deltas)
    assert route.call_count == 2
    assert route.calls[0].request.content == route.calls[1].request.content
    assert all(
        call.request.headers[OPENCODE_SESSION_HEADER] == "vbot-retry-affinity"
        for call in route.calls
    )
    sleep.assert_awaited_once()
    assert sleep.await_args is not None
    assert sleep.await_args.args[0] >= 7
    error = notices[0].error
    assert type(error) is ProviderError
    assert error.retryable is True
    assert error.status_code == status
    assert error.retry_after == 7
    assert json.loads(str(error).split(f"{status} ", 1)[1]) == body


@pytest.mark.parametrize(
    ("caller_owned", "attempts"),
    [
        pytest.param(True, 1, id="chat-owns-retries"),
        pytest.param(False, MAX_RETRIES + 1, id="standalone"),
    ],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.asyncio
async def test_responses_wire_leaves_upstream_json_retries_to_the_retry_owner(
    streaming: bool, caller_owned: bool, attempts: int
) -> None:
    """Go's own Responses route adds no retry loop beyond the caller-owned budget."""
    with (
        respx.mock,
        caller_owns_retries() if caller_owned else nullcontext(),
    ):
        route = respx.post(RESPONSES_URL).mock(
            return_value=httpx.Response(403, json=UPSTREAM_JSON_FAILURE)
        )
        with pytest.raises(ProviderError) as failure:
            await go_request(go_adapter(), RESPONSES_MODEL, streaming=streaming)

    assert type(failure.value) is ProviderError
    assert failure.value.retryable is True
    assert failure.value.attempts_made == attempts
    assert route.call_count == attempts


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.parametrize(
    ("wire", "code"),
    [
        ("chat", "authentication_error"),
        ("messages", "permission_denied"),
        ("responses", "authentication_error"),
        ("gemini", "authentication_error"),
    ],
)
@pytest.mark.asyncio
async def test_real_forbidden_response_is_a_fatal_auth_error_on_every_wire(
    wire: str, code: str, streaming: bool
) -> None:
    model_id, url = WIRES[wire]

    with respx.mock:
        route = respx.post(GEMINI_STREAM_URL if wire == "gemini" and streaming else url).mock(
            return_value=httpx.Response(
                403, json={"error": {"type": code, "code": code, "message": "Access denied"}}
            )
        )
        with pytest.raises(ProviderAuthError) as failure:
            await go_request(go_adapter(), model_id, streaming=streaming)

    assert failure.value.retryable is False
    assert route.call_count == 1


def _upstream_error(failure: dict[str, Any], **changes: str) -> str:
    return json.dumps({"error": {**failure["error"], **changes}})


_THINKING_MESSAGE = UPSTREAM_THINKING_FAILURE["error"]["message"]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        pytest.param(
            401, json.dumps(UPSTREAM_JSON_FAILURE), ProviderAuthError, id="401-with-the-403-body"
        ),
        pytest.param(403, "Forbidden", ProviderAuthError, id="plain-text"),
        pytest.param(403, "[]", ProviderAuthError, id="non-object-json"),
        pytest.param(403, '{"error":"server_error"}', ProviderAuthError, id="non-object-error"),
        pytest.param(
            403,
            _upstream_error(UPSTREAM_JSON_FAILURE, code="invalid_api_key"),
            ProviderAuthError,
            id="other-code",
        ),
        pytest.param(
            403,
            _upstream_error(UPSTREAM_JSON_FAILURE, message="Other failure"),
            ProviderAuthError,
            id="other-message",
        ),
        pytest.param(
            403,
            json.dumps(UPSTREAM_THINKING_FAILURE),
            ProviderAuthError,
            id="403-with-the-400-body",
        ),
        pytest.param(
            400,
            _upstream_error(UPSTREAM_THINKING_FAILURE, type="invalid_request_error"),
            ProviderError,
            id="400-other-type",
        ),
        pytest.param(
            400,
            _upstream_error(UPSTREAM_THINKING_FAILURE, message="Upstream request failed: [x] y"),
            ProviderError,
            id="400-other-message",
        ),
        pytest.param(
            400,
            json.dumps({"error": {"message": _THINKING_MESSAGE}}),
            ProviderError,
            id="400-without-type",
        ),
    ],
)
@pytest.mark.asyncio
async def test_only_the_observed_upstream_failures_escape_their_status_policy(
    status: int, body: str, expected: type[ProviderError]
) -> None:
    with respx.mock, caller_owns_retries():
        route = respx.post(CHAT_URL).mock(return_value=httpx.Response(status, text=body))
        with pytest.raises(ProviderError) as failure:
            await go_adapter().send(HELLO, model_id=CHAT_MODEL)

    assert isinstance(failure.value, expected)
    assert failure.value.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_other_compatible_providers_keep_the_shared_403_policy() -> None:
    adapter = OpenAICompatibleAdapter(go_config(), API_KEY)

    with respx.mock, caller_owns_retries():
        respx.post(CHAT_URL).mock(return_value=httpx.Response(403, json=UPSTREAM_JSON_FAILURE))
        with pytest.raises(ProviderAuthError) as failure:
            await adapter.send(HELLO, model_id=CHAT_MODEL)
    await adapter.aclose()

    assert failure.value.retryable is False


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.parametrize(
    ("wire", "error"),
    [
        pytest.param(
            "chat",
            {"type": "GoUsageLimitError", "message": "Monthly usage limit reached."},
            id="chat-go-usage",
        ),
        pytest.param(
            "messages",
            {"type": "FreeUsageLimitError", "message": "Monthly usage limit has been reached."},
            id="messages-free-usage",
        ),
        pytest.param(
            "responses",
            {"code": "insufficient_quota", "message": "Quota exceeded."},
            id="responses-quota",
        ),
        pytest.param(
            "gemini",
            {"type": "GoUsageLimitError", "message": "Monthly usage limit reached."},
            id="gemini-quota",
        ),
    ],
)
@pytest.mark.asyncio
async def test_subscription_exhaustion_429_is_never_retried(
    wire: str, error: dict[str, str], streaming: bool
) -> None:
    """Account exhaustion shares HTTP 429 with throttling but waiting cannot fix it."""
    model_id, url = WIRES[wire]

    with respx.mock:
        route = respx.post(GEMINI_STREAM_URL if wire == "gemini" and streaming else url).mock(
            return_value=httpx.Response(429, json={"type": "error", "error": error})
        )
        with pytest.raises(ProviderError) as failure:
            await go_request(go_adapter(), model_id, streaming=streaming)

    assert type(failure.value) is ProviderError
    assert failure.value.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_transient_rate_limit_retries_with_the_session_header() -> None:
    adapter = go_adapter()

    with respx.mock:
        route = respx.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(
                    429, json={"error": {"type": "rate_limit_error", "message": "Slow down."}}
                ),
                success_response("chat", streaming=False),
            ]
        )
        response = await adapter.send(HELLO, model_id=CHAT_MODEL, **_affinity(adapter))

    assert response["choices"][0]["message"]["content"] == "ok"
    assert route.call_count == 2
    assert all(
        call.request.headers[OPENCODE_SESSION_HEADER] == "vbot-retry-affinity"
        for call in route.calls
    )


@pytest.mark.asyncio
async def test_responses_error_body_read_failure_is_a_retryable_network_error_and_closes() -> None:
    class InterruptedErrorBody(httpx.AsyncByteStream):
        closed = False

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"error":'
            raise httpx.ReadError("body interrupted")

        @override
        async def aclose(self) -> None:
            self.closed = True

    body = InterruptedErrorBody()

    with respx.mock, caller_owns_retries():
        respx.post(RESPONSES_URL).mock(return_value=httpx.Response(503, stream=body))
        with pytest.raises(NetworkError) as caught:
            await go_request(go_adapter(), RESPONSES_MODEL, streaming=True)

    assert caught.value.retryable is True
    assert body.closed
