"""OpenCode Go's observed upstream failure and ordinary authentication failures."""

from __future__ import annotations

import json
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.errors import NetworkError, ProviderAuthError, ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OPENCODE_SESSION_HEADER, OpenCodeGoAdapter
from core.utils.retry import MAX_RETRIES, RetryNotice, caller_owns_retries, observe_retries
from tests.core.providers.opencode_go_helpers import (
    OPENCODE_GO_MESSAGES_URL,
    OPENCODE_GO_RESPONSES_URL,
    OPENCODE_GO_URL,
    RESPONSES_COMPLETED_RESPONSE,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_adapter as opencode_go_adapter,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_config as opencode_go_config,
)

UPSTREAM_JSON_FAILURE = {
    "error": {
        "type": "server_error",
        "code": "server_error",
        "message": "Upstream request failed: [server_error] Upstream response was not valid JSON",
    }
}


@pytest.fixture(
    params=[
        pytest.param(("deepseek-v4-flash", OPENCODE_GO_URL, "chat"), id="chat"),
        pytest.param(("minimax-m3", OPENCODE_GO_MESSAGES_URL, "messages"), id="messages"),
        pytest.param(("gpt-5.6-luna", OPENCODE_GO_RESPONSES_URL, "responses"), id="responses"),
    ]
)
def go_wire(request: pytest.FixtureRequest) -> tuple[str, str, str]:
    return cast(tuple[str, str, str], request.param)


def _success_response(wire: str, streaming: bool) -> httpx.Response:
    if streaming:
        text = {
            "chat": (
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
            "messages": (
                'event: message_delta\ndata: {"type":"message_delta",'
                '"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
                'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            ),
            "responses": (
                "event: response.completed\n"
                f"data: {json.dumps({'response': RESPONSES_COMPLETED_RESPONSE})}\n\n"
            ),
        }[wire]
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
    payload = {
        "chat": {
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ]
        },
        "messages": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
        },
        "responses": RESPONSES_COMPLETED_RESPONSE,
    }[wire]
    return httpx.Response(200, json=payload)


async def _request(adapter: OpenCodeGoAdapter, model_id: str, streaming: bool) -> None:
    messages = [{"role": "user", "content": "hello"}]
    context = adapter.request_context_kwargs(
        agent_id="test-agent",
        session_id="test-session",
        prompt_cache_affinity_id="retry-affinity",
    )
    if streaming:
        deltas = [delta async for delta in adapter.stream(messages, model_id=model_id, **context)]
        assert any(delta.get("type") == "finish" for delta in deltas)
    else:
        await adapter.send(messages, model_id=model_id, **context)


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@respx.mock
@pytest.mark.asyncio
async def test_upstream_json_failure_retries_identical_request(
    opencode_go_adapter: OpenCodeGoAdapter,
    go_wire: tuple[str, str, str],
    streaming: bool,
) -> None:
    model_id, url, wire = go_wire
    route = respx.post(url).mock(
        side_effect=[
            httpx.Response(403, json=UPSTREAM_JSON_FAILURE, headers={"retry-after": "7"}),
            _success_response(wire, streaming),
        ]
    )
    notices: list[RetryNotice] = []
    with (
        observe_retries(notices.append),
        patch("core.utils.retry._sleep", new_callable=AsyncMock) as sleep,
    ):
        await _request(opencode_go_adapter, model_id, streaming)

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
    assert isinstance(error, ProviderError)
    assert not isinstance(error, ProviderAuthError)
    assert error.retryable is True
    assert error.status_code == 403
    assert error.retry_after == 7
    assert json.loads(str(error).split("403 ", 1)[1]) == UPSTREAM_JSON_FAILURE


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@respx.mock
@pytest.mark.asyncio
async def test_upstream_json_failure_defers_retry_to_chat_owner(
    opencode_go_adapter: OpenCodeGoAdapter,
    go_wire: tuple[str, str, str],
    streaming: bool,
) -> None:
    model_id, url, _ = go_wire
    route = respx.post(url).mock(return_value=httpx.Response(403, json=UPSTREAM_JSON_FAILURE))

    with caller_owns_retries(), pytest.raises(ProviderError) as failure:
        await _request(opencode_go_adapter, model_id, streaming)

    assert type(failure.value) is ProviderError
    assert failure.value.retryable is True
    assert failure.value.status_code == 403
    assert failure.value.attempts_made == 1
    assert route.call_count == 1


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@respx.mock
@pytest.mark.asyncio
async def test_persistent_upstream_json_failure_exhausts_standalone_retry_budget(
    opencode_go_adapter: OpenCodeGoAdapter,
    go_wire: tuple[str, str, str],
    streaming: bool,
) -> None:
    model_id, url, _ = go_wire
    route = respx.post(url).mock(return_value=httpx.Response(403, json=UPSTREAM_JSON_FAILURE))

    with (
        patch("core.utils.retry._sleep", new_callable=AsyncMock),
        pytest.raises(ProviderError) as failure,
    ):
        await _request(opencode_go_adapter, model_id, streaming)

    assert type(failure.value) is ProviderError
    assert failure.value.retryable is True
    assert failure.value.attempts_made == MAX_RETRIES + 1
    assert route.call_count == MAX_RETRIES + 1


@pytest.mark.parametrize("code", ["authentication_error", "permission_denied"])
@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@respx.mock
@pytest.mark.asyncio
async def test_real_forbidden_response_is_never_retried(
    opencode_go_adapter: OpenCodeGoAdapter,
    go_wire: tuple[str, str, str],
    streaming: bool,
    code: str,
) -> None:
    model_id, url, _ = go_wire
    route = respx.post(url).mock(
        return_value=httpx.Response(
            403,
            json={"error": {"type": code, "code": code, "message": "Access denied"}},
        )
    )

    with pytest.raises(ProviderAuthError) as failure:
        await _request(opencode_go_adapter, model_id, streaming)

    assert failure.value.retryable is False
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, json.dumps(UPSTREAM_JSON_FAILURE)),
        (403, "Forbidden"),
        (403, "{invalid json"),
        (403, "null"),
        (403, "[]"),
        (403, '{"error":"server_error"}'),
        (403, '{"error":{"type":"server_error","code":"server_error"}}'),
        (403, json.dumps({"error": {**UPSTREAM_JSON_FAILURE["error"], "code": "invalid_api_key"}})),
        (
            403,
            json.dumps({"error": {**UPSTREAM_JSON_FAILURE["error"], "type": "permission_error"}}),
        ),
        (
            403,
            json.dumps({"error": {**UPSTREAM_JSON_FAILURE["error"], "message": "Other failure"}}),
        ),
    ],
)
def test_only_the_observed_structured_403_failure_overrides_auth_classification(
    opencode_go_adapter: OpenCodeGoAdapter, status: int, body: str
) -> None:
    with pytest.raises(ProviderAuthError) as failure:
        opencode_go_adapter._classify_http_status(
            status, detail=f"{status} {body}", response_headers=httpx.Headers()
        )
    assert failure.value.retryable is False


def test_other_compatible_providers_keep_the_shared_403_policy(
    opencode_go_adapter: OpenCodeGoAdapter,
) -> None:
    with pytest.raises(ProviderAuthError) as failure:
        OpenAICompatibleAdapter._classify_http_status(
            opencode_go_adapter,
            403,
            detail=f"403 {json.dumps(UPSTREAM_JSON_FAILURE)}",
            response_headers=httpx.Headers(),
        )
    assert failure.value.retryable is False


class _InterruptedErrorBody(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        yield b'{"error":'
        raise httpx.ReadError("body interrupted")

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
@pytest.mark.asyncio
async def test_responses_error_body_read_failure_is_retryable_network_error_and_closes(
    opencode_go_adapter: OpenCodeGoAdapter,
) -> None:
    body = _InterruptedErrorBody()
    respx.post(OPENCODE_GO_RESPONSES_URL).mock(return_value=httpx.Response(503, stream=body))

    with caller_owns_retries(), pytest.raises(NetworkError) as caught:
        async for _ in opencode_go_adapter.stream(
            [{"role": "user", "content": "hello"}], model_id="gpt-5.6-luna"
        ):
            pass

    assert caught.value.retryable is True
    assert body.closed
