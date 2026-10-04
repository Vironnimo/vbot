"""OpenAI Adapter Codex WebSocket transport: continuation, route isolation, fallback and tracing.

Every socket is an in-memory fake injected through ``codex_websocket_connect``.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import respx
from websockets.datastructures import Headers

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore
from core.providers.errors import NetworkError, ProviderAuthError, ProviderError
from core.providers.openai import CODEX_WEBSOCKET_BETA, OpenAIAdapter
from core.utils.tls import shared_ssl_context

from .openai_test_support import (
    ACCOUNT_ID,
    CODEX_TOOLS,
    OPENAI_SUBSCRIPTION_URL,
    SAMPLE_MESSAGES,
    RotatingTokenGetter,
    codex_adapter,
    codex_sse_response,
    jwt_with_account,
)

MODEL_ID = "gpt-5.6-terra"
CONVERSATION_ID = "orchestrator:sess-42"


class _FakeCodexWebSocket:
    """Replays one scripted event batch per ``response.create`` sent."""

    def __init__(self, event_batches: Sequence[Sequence[dict[str, Any] | BaseException]]) -> None:
        self._event_batches = deque(deque(batch) for batch in event_batches)
        self._active_events: deque[dict[str, Any] | BaseException] = deque()
        self.sent_payloads: list[dict[str, Any]] = []
        self.closed = False
        self.response = SimpleNamespace(status_code=101, headers={"x-test-transport": "ws"})

    async def send(self, data: str) -> None:
        self.sent_payloads.append(json.loads(data))
        if not self._event_batches:
            raise AssertionError("unexpected WebSocket request")
        self._active_events = self._event_batches.popleft()

    async def recv(self) -> str:
        if not self._active_events:
            raise AssertionError("WebSocket response ended without a terminal event")
        event = self._active_events.popleft()
        if isinstance(event, BaseException):
            raise event
        return json.dumps(event)

    async def close(self) -> None:
        self.closed = True


class _FakeCodexWebSocketConnector:
    def __init__(self, connections: list[_FakeCodexWebSocket | BaseException]) -> None:
        self._connections = deque(connections)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> _FakeCodexWebSocket:
        self.calls.append((url, kwargs))
        if not self._connections:
            raise AssertionError("unexpected WebSocket connection")
        connection = self._connections.popleft()
        if isinstance(connection, BaseException):
            raise connection
        return connection

    def headers(self, name: str) -> list[str]:
        return [kwargs["additional_headers"][name] for _url, kwargs in self.calls]


def _completed(response_id: str, output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    }


def _output_item_done(output_index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.done", "output_index": output_index, "item": item}


_REASONING_ITEM = {
    "id": "rs_1",
    "type": "reasoning",
    "summary": [{"type": "summary_text", "text": "Checking"}],
    "encrypted_content": "opaque-reasoning",
}

_TOOL_CALL_ITEM = {
    "id": "fc_1",
    "type": "function_call",
    "status": "completed",
    "call_id": "call_1",
    "name": "lookup",
    "arguments": '{"query":"river"}',
}

_FINAL_MESSAGE_ITEM = {
    "id": "msg_2",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Done"}],
}

_TOOL_OUTPUT = {
    "type": "function_call_output",
    "call_id": "call_1",
    "output": '{"ok":true,"data":{"level":91}}',
}

_FULL_CONTEXT_INPUT_KINDS = ["user", "reasoning", "function_call", "function_call_output"]


def _tool_call_turn(response_id: str) -> list[dict[str, Any]]:
    return [_completed(response_id, [dict(_REASONING_ITEM), dict(_TOOL_CALL_ITEM)])]


def _final_turn(response_id: str) -> list[dict[str, Any]]:
    return [_completed(response_id, [dict(_FINAL_MESSAGE_ITEM)])]


def _input_kinds(payload: dict[str, Any]) -> list[str]:
    return [item.get("type", item.get("role")) for item in payload["input"]]


def _messages_with_tool_result(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *SAMPLE_MESSAGES,
        {
            "role": "assistant",
            "content": normalized["content"],
            "reasoning": normalized["reasoning"],
            "reasoning_meta": normalized["reasoning_meta"],
            "tool_calls": normalized["tool_calls"],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "lookup",
            "content": _TOOL_OUTPUT["output"],
        },
    ]


async def _send(
    adapter: OpenAIAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs.setdefault("model_id", MODEL_ID)
    kwargs.setdefault("conversation_id", CONVERSATION_ID)
    return adapter.normalize_response(await adapter.send(messages, **kwargs))


# ---------------------------------------------------------------------------
# Continuation on one socket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_websocket_reuses_connection_and_sends_only_new_tool_result() -> None:
    """A Tool continuation chains by ``previous_response_id`` with only the appended input.

    The Provider-visible cache headers are clamped to 64 characters while the full
    conversation id keeps keying the local route.
    """

    websocket = _FakeCodexWebSocket(
        [
            [
                _output_item_done(0, dict(_REASONING_ITEM)),
                _output_item_done(1, dict(_TOOL_CALL_ITEM)),
                _completed("resp_1", []),
            ],
            _final_turn("resp_2"),
        ]
    )
    connector = _FakeCodexWebSocketConnector([websocket])
    adapter = codex_adapter(codex_websocket_connect=connector)
    conversation_id = "orchestrator:" + ("session-" * 20)
    request: dict[str, Any] = {
        "conversation_id": conversation_id,
        "thinking_effort": "high",
        "tools": CODEX_TOOLS,
    }

    first = await _send(adapter, **request)
    second = await _send(adapter, _messages_with_tool_result(first), **request)

    assert second["content"] == "Done"
    assert len(connector.calls) == 1
    first_payload, second_payload = websocket.sent_payloads
    assert first_payload["type"] == "response.create"
    assert first_payload["store"] is False
    assert "previous_response_id" not in first_payload
    assert second_payload["previous_response_id"] == "resp_1"
    assert second_payload["input"] == [_TOOL_OUTPUT]
    url, connect_kwargs = connector.calls[0]
    assert url == "wss://chatgpt.com/backend-api/codex/responses"
    headers = connect_kwargs["additional_headers"]
    assert headers["OpenAI-Beta"] == CODEX_WEBSOCKET_BETA
    assert headers["session-id"] == conversation_id[:64]
    assert headers["x-client-request-id"] == conversation_id[:64]
    assert "session_id" not in headers
    # One process-wide TLS context: no CA bundle parse per connection on the loop.
    assert connect_kwargs["ssl"] is shared_ssl_context()
    await adapter.aclose()
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_closing_a_partial_codex_stream_releases_the_socket_for_the_next_request() -> None:
    partial = _FakeCodexWebSocket([[{"type": "response.output_text.delta", "delta": "partial"}]])
    replacement = _FakeCodexWebSocket([_final_turn("resp_2")])
    connector = _FakeCodexWebSocketConnector([partial, replacement])
    adapter = codex_adapter(codex_websocket_connect=connector)
    stream = cast(
        AsyncGenerator[dict[str, Any]],
        adapter.stream(SAMPLE_MESSAGES, model_id=MODEL_ID, conversation_id=CONVERSATION_ID),
    )
    try:
        assert await anext(stream) == {"type": "content_delta", "text": "partial"}
        await stream.aclose()
        assert partial.closed is True

        # A socket lock left held by the abandoned stream would block this request.
        assert (await asyncio.wait_for(_send(adapter), timeout=1))["content"] == "Done"
        assert len(connector.calls) == 2
        assert "previous_response_id" not in replacement.sent_payloads[0]
    finally:
        await stream.aclose()
        await adapter.aclose()


@pytest.mark.asyncio
async def test_codex_websocket_missing_continuation_reconnects_with_full_context() -> None:
    first_websocket = _FakeCodexWebSocket(
        [
            _tool_call_turn("resp_1"),
            [
                {
                    "type": "error",
                    "error": {
                        "code": "previous_response_not_found",
                        "message": "Previous response was not found.",
                    },
                }
            ],
        ]
    )
    replacement_websocket = _FakeCodexWebSocket([_final_turn("resp_2")])
    connector = _FakeCodexWebSocketConnector([first_websocket, replacement_websocket])
    adapter = codex_adapter(codex_websocket_connect=connector)

    first = await _send(adapter, tools=CODEX_TOOLS)
    second = await _send(adapter, _messages_with_tool_result(first), tools=CODEX_TOOLS)

    assert second["content"] == "Done"
    assert len(connector.calls) == 2
    assert first_websocket.sent_payloads[1]["previous_response_id"] == "resp_1"
    replay = replacement_websocket.sent_payloads[0]
    assert "previous_response_id" not in replay
    assert _input_kinds(replay) == _FULL_CONTEXT_INPUT_KINDS
    assert first_websocket.closed is True
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Route isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("second_conversation", "second_model", "accounts"),
    [
        pytest.param(
            "orchestrator:fork", MODEL_ID, ("acct_one", "acct_one"), id="conversation-change"
        ),
        pytest.param(CONVERSATION_ID, "gpt-5.6-sol", ("acct_one", "acct_one"), id="model-change"),
        pytest.param(CONVERSATION_ID, MODEL_ID, ("acct_one", "acct_two"), id="account-change"),
    ],
)
@pytest.mark.asyncio
async def test_codex_websocket_never_chains_across_a_route_change(
    second_conversation: str, second_model: str, accounts: tuple[str, str]
) -> None:
    """Conversation, Model and ChatGPT Account isolate continuation; cache affinity does not."""

    first_websocket = _FakeCodexWebSocket([_tool_call_turn("resp_1")])
    second_websocket = _FakeCodexWebSocket([_final_turn("resp_2")])
    connector = _FakeCodexWebSocketConnector([first_websocket, second_websocket])
    adapter = codex_adapter(
        RotatingTokenGetter([jwt_with_account(account) for account in accounts]),
        codex_websocket_connect=connector,
    )
    shared: dict[str, Any] = {
        "prompt_cache_affinity_id": "shared-cache-lineage",
        "tools": CODEX_TOOLS,
    }

    first = await _send(adapter, **shared)
    await _send(
        adapter,
        _messages_with_tool_result(first),
        model_id=second_model,
        conversation_id=second_conversation,
        **shared,
    )

    assert len(connector.calls) == 2
    assert first_websocket.closed is True
    second_payload = second_websocket.sent_payloads[0]
    assert "previous_response_id" not in second_payload
    assert _input_kinds(second_payload) == _FULL_CONTEXT_INPUT_KINDS
    assert connector.headers("chatgpt-account-id") == list(accounts)
    assert connector.headers("session-id") == ["shared-cache-lineage"] * 2
    assert connector.headers("x-client-request-id") == ["shared-cache-lineage"] * 2
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _failed(code: str) -> dict[str, Any]:
    return {
        "type": "response.failed",
        "response": {
            "id": "resp_failed",
            "status": "failed",
            "instructions": "test-instructions-sentinel",
            "error": {"code": code, "message": "Test failure. Please retry."},
        },
    }


def _error_frame(status: int, **error: str) -> dict[str, Any]:
    return {
        "type": "error",
        "status": status,
        "error": {"message": "Test failure.", **error},
        "headers": {"x-test-header": "test-header-sentinel"},
    }


@pytest.mark.parametrize(
    ("event", "sentinel", "expected_type", "retryable"),
    [
        pytest.param(
            _failed("test_unknown_code"),
            "test_unknown_code",
            ProviderError,
            True,
            id="unknown-code-retries",
        ),
        pytest.param(
            _failed("insufficient_quota"),
            "insufficient_quota",
            ProviderError,
            False,
            id="known-fatal-code-stops",
        ),
        pytest.param(
            _error_frame(500, type="test_unknown_type"),
            "test_unknown_type",
            ProviderError,
            True,
            id="server-status-retries",
        ),
        pytest.param(
            _error_frame(400, type="invalid_request_error", code="test_rejected_value"),
            "test_rejected_value",
            ProviderError,
            False,
            id="client-status-stops",
        ),
        pytest.param(
            _error_frame(400, code="websocket_connection_limit_reached"),
            "websocket_connection_limit_reached",
            ProviderError,
            True,
            id="connection-limit-retries",
        ),
        pytest.param(
            _error_frame(401, type="test_auth_type"),
            "test_auth_type",
            ProviderAuthError,
            False,
            id="auth-status",
        ),
    ],
)
@pytest.mark.asyncio
async def test_codex_error_events_follow_codex_retry_classification(
    event: dict[str, Any],
    sentinel: str,
    expected_type: type[ProviderError],
    retryable: bool,
) -> None:
    """Unknown codes retry; known fatal codes and rejected requests stop; facts stay visible."""

    connector = _FakeCodexWebSocketConnector([_FakeCodexWebSocket([[event]])])
    adapter = codex_adapter(codex_websocket_connect=connector)

    with pytest.raises(ProviderError) as caught:
        await _send(adapter)

    assert type(caught.value) is expected_type
    assert caught.value.retryable is retryable
    message = str(caught.value)
    assert sentinel in message
    assert "test-instructions-sentinel" not in message
    assert "test-header-sentinel" not in message
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Transport failure and SSE fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_websocket_failure_before_events_disables_route_and_falls_back_to_sse() -> None:
    connector = _FakeCodexWebSocketConnector([OSError("upgrade unavailable")])
    adapter = codex_adapter(codex_websocket_connect=connector)

    with respx.mock:
        route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            side_effect=[
                codex_sse_response(
                    {
                        "id": response_id,
                        "status": "completed",
                        "output": [dict(_FINAL_MESSAGE_ITEM)],
                    }
                )
                for response_id in ("resp_sse_1", "resp_sse_2")
            ]
        )
        for _ in range(2):
            assert (await _send(adapter))["content"] == "Done"

    assert len(connector.calls) == 1
    assert route.call_count == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_codex_websocket_failure_after_event_propagates_and_next_attempt_uses_sse() -> None:
    """An in-flight exchange is never replayed internally; Chat's next attempt uses SSE."""

    websocket = _FakeCodexWebSocket(
        [
            [
                {"type": "response.created", "response": {"id": "resp_started"}},
                OSError("socket dropped"),
            ]
        ]
    )
    connector = _FakeCodexWebSocketConnector([websocket])
    adapter = codex_adapter(codex_websocket_connect=connector)

    with respx.mock:
        sse_route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
            return_value=codex_sse_response(
                {"id": "resp_sse", "status": "completed", "output": [dict(_FINAL_MESSAGE_ITEM)]}
            )
        )
        with pytest.raises(NetworkError):
            await _send(adapter)

        assert sse_route.call_count == 0
        assert websocket.closed is True

        assert (await _send(adapter))["content"] == "Done"

    assert sse_route.call_count == 1
    assert len(connector.calls) == 1
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Debug tracing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_websocket_exchange_keeps_canonical_debug_trace(tmp_path: Path) -> None:
    websocket = _FakeCodexWebSocket([_final_turn("resp_1")])
    # chatgpt.com answers the upgrade with several Set-Cookie headers.
    websocket.response.headers = Headers(
        [("x-test-transport", "ws"), ("set-cookie", "a=1"), ("set-cookie", "b=2")]
    )
    connector = _FakeCodexWebSocketConnector([websocket])
    debug_store = DebugTraceStore(tmp_path, trace_limit=10)
    adapter = codex_adapter(
        codex_websocket_connect=connector,
        debug_recorder=ProviderDebugRecorder(debug_store),
    )
    adapter.set_debug_context(
        DebugContext(
            run_id="run-ws",
            agent_id="orchestrator",
            session_id="sess-42",
            provider_id="openai",
            connection_id="openai:subscription",
            model_id=MODEL_ID,
            streaming=True,
            iteration_number=1,
        )
    )

    await _send(adapter)

    traces = debug_store.get_traces()
    assert len(traces) == 1
    trace = debug_store.get_trace(traces[0]["trace_id"])
    assert trace["request"]["method"] == "WEBSOCKET"
    assert trace["request"]["url"] == "wss://chatgpt.com/backend-api/codex/responses"
    assert json.loads(trace["request"]["body"])["type"] == "response.create"
    assert trace["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert trace["request"]["headers"]["chatgpt-account-id"] == "[REDACTED]"
    assert ACCOUNT_ID not in json.dumps(trace["request"]["headers"])
    assert trace["response"]["status_code"] == 101
    assert trace["response"]["headers"]["x-test-transport"] == "ws"
    assert trace["response"]["headers"]["set-cookie"] == "[REDACTED]"
    assert json.loads(trace["response"]["body"])["type"] == "response.completed"
    assert len(connector.calls) == 1
    await adapter.aclose()
