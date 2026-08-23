"""Tests for the unified OpenAI provider adapter.

Covers both the default ``/chat/completions`` mode (``api-key`` connection)
and the Codex Responses mode (``subscription`` connection with
``connection_mode="codex_responses"``).
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore
from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import NetworkError, ProviderAuthError, ProviderTimeoutError
from core.providers.openai import (
    CODEX_EXTRA_HEADERS,
    CODEX_RESPONSES_ENDPOINT,
    CODEX_RESPONSES_MODE,
    CODEX_WEBSOCKET_BETA,
    OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS,
    OpenAIAdapter,
)
from core.providers.openai_compatible import CHAT_COMPLETIONS_ENDPOINT
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


def _subscription_model_lookup(levels: tuple[str, ...]):
    def model_lookup(model_id: str) -> Model:
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="levels" if levels else None,
                    levels=levels,
                ),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )

    return model_lookup


OPENAI_API_KEY_URL = f"https://api.openai.com/v1{CHAT_COMPLETIONS_ENDPOINT}"
OPENAI_PLATFORM_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_SUBSCRIPTION_URL = f"https://chatgpt.com/backend-api{CODEX_RESPONSES_ENDPOINT}"
SAMPLE_MESSAGES = [
    {"role": "system", "content": "Use concise answers."},
    {"role": "user", "content": "Hello"},
]


def _platform_config() -> ProviderConfig:
    """Provider config matching the OpenAI Platform ``api-key`` connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENAI_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


def _subscription_config(*, include_mode: bool = True) -> ProviderConfig:
    """Provider config matching the ChatGPT ``subscription`` connection."""

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://chatgpt.com/backend-api",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(header="Authorization", prefix="Bearer "),
                mode=CODEX_RESPONSES_MODE if include_mode else None,
            )
        ],
        defaults={"max_tokens": 8192},
    )


class _RotatingTokenGetter:
    """Async token getter that yields a fresh token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token


def _jwt_with_account(account_id: str = "acct_vbot") -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


def _codex_sse_response(response: dict[str, object]) -> httpx.Response:
    body = (
        "event: response.completed\n"
        f"data: {json.dumps({'type': 'response.completed', 'response': response})}\n\n"
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


class _FakeCodexWebSocket:
    def __init__(self, event_batches: list[list[dict[str, Any] | BaseException]]) -> None:
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


def _codex_completed_event(
    response_id: str,
    output: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    }


def _codex_output_item_event(output_index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "output_index": output_index,
        "item": item,
    }


_CODEX_REASONING_ITEM = {
    "id": "rs_1",
    "type": "reasoning",
    "summary": [{"type": "summary_text", "text": "Checking"}],
    "encrypted_content": "opaque-reasoning",
}
_CODEX_TOOL_CALL_ITEM = {
    "id": "fc_1",
    "type": "function_call",
    "status": "completed",
    "call_id": "call_1",
    "name": "lookup",
    "arguments": '{"query":"river"}',
}
_CODEX_FINAL_MESSAGE_ITEM = {
    "id": "msg_2",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Done"}],
}
_CODEX_TOOLS = [
    {
        "name": "lookup",
        "description": "Look up data",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "strict": True,
    }
]


def _messages_with_codex_tool_result(normalized: dict[str, Any]) -> list[dict[str, Any]]:
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
            "content": '{"ok":true,"data":{"level":91}}',
        },
    ]


def _model_lookup_with_openai_wire_policies(model_id: str) -> Model:
    api_key_policy: dict[str, str] = {
        "protocol": "responses",
    }
    if model_id.startswith("gpt-5.6"):
        api_key_policy["reasoning_context"] = "all_turns"
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control="levels",
                levels=("none", "low", "medium", "high", "xhigh", "max"),
            ),
            input_modalities=("text", "image", "pdf"),
            output_modalities=("text",),
            supported_parameters=("parallel_tool_calls", "reasoning", "response_format", "tools"),
        ),
        context_window=1_050_000,
        max_output_tokens=128_000,
        metadata={
            "openai": {
                "wire_policies": {
                    "api-key": api_key_policy,
                    "subscription": {
                        "protocol": "responses",
                    },
                }
            }
        },
    )


# ------------------------------------------------------------------
# Codex Responses mode (subscription connection)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_headers_ignore_provider_extra_headers() -> None:
    """Provider extra_headers must not leak onto the Codex Responses wire."""
    access_token = _jwt_with_account("acct_openai")
    config = replace(_subscription_config(), extra_headers={"X-Injected": "leak"})
    adapter = OpenAIAdapter(config, access_token, connection_mode=CODEX_RESPONSES_MODE)

    headers = await adapter._build_codex_headers()

    assert "X-Injected" not in headers
    assert headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert headers["originator"] == CODEX_EXTRA_HEADERS["originator"]


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_posts_responses_payload_with_account_and_beta_headers() -> None:
    """Codex send() targets ``/codex/responses`` with the unified Codex headers."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response(
            {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Hi"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            }
        )
    )

    response = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-codex",
        thinking_effort="max",
        response_format={"type": "json_object"},
        temperature=0.2,
        tools=[
            {
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object"},
                "strict": True,
            }
        ],
    )

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {access_token}"
    assert request.headers["chatgpt-account-id"] == "acct_openai"
    assert request.headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert request.headers["originator"] == CODEX_EXTRA_HEADERS["originator"]
    payload = json.loads(request.content)
    assert payload == {
        "model": "gpt-5-codex",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "instructions": "Use concise answers.",
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": {"type": "object"},
                "strict": False,
            }
        ],
        "reasoning": {"effort": "xhigh", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "text": {"format": {"type": "json_object"}},
        "store": False,
        "stream": True,
    }
    assert adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Hi",
        "reasoning": None,
        "reasoning_meta": {
            "response_id": "resp_1",
            "response_output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Hi"}],
                }
            ],
        },
        "tool_calls": None,
        "usage": {"input_tokens": 2, "output_tokens": 3},
        "terminal_outcome": "stop",
    }


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_collects_text_deltas_when_completed_output_is_empty() -> None:
    """The live Codex wire may leave completed.output empty after streaming text."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
    )
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Generated "}\n\n'
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"title"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_1",'
        '"status":"completed","output":[],"usage":{"input_tokens":2,'
        '"output_tokens":2}}}\n\n'
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    response = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5")

    assert route.call_count == 1
    assert adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Generated title",
        "reasoning": None,
        "reasoning_meta": {"response_id": "resp_1"},
        "tool_calls": None,
        "usage": {"input_tokens": 2, "output_tokens": 2},
        "terminal_outcome": "stop",
    }


@pytest.mark.asyncio
async def test_codex_send_times_out_when_its_internal_stream_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-streaming adapter semantics bound the internally streamed Codex wire."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account(),
        connection_mode=CODEX_RESPONSES_MODE,
    )

    async def _stalled_stream(*args: Any, **kwargs: Any):
        del args, kwargs
        await asyncio.Future()
        yield {}

    monkeypatch.setattr(adapter, "_stream_responses", _stalled_stream)
    monkeypatch.setattr(
        "core.providers.openai.PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(ProviderTimeoutError):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5")


def test_request_context_kwargs_separates_conversation_and_cache_affinity() -> None:
    """The hook keeps transport identity distinct from cache routing."""

    adapter = OpenAIAdapter(_subscription_config(), _jwt_with_account())

    context = adapter.request_context_kwargs(
        agent_id="orchestrator",
        session_id="sess-42",
        prompt_cache_affinity_id="shared-cache-lineage",
    )

    assert context == {
        "conversation_id": "orchestrator:sess-42",
        "prompt_cache_affinity_id": "shared-cache-lineage",
    }


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_stamps_cache_scope_headers() -> None:
    """The Codex request pins the prompt cache with per-conversation headers."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_transport="sse",
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response(
            {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-codex",
        conversation_id="orchestrator:sess-42",
        prompt_cache_affinity_id="shared-cache-lineage",
    )

    request = route.calls.last.request
    assert request.headers["session_id"] == "shared-cache-lineage"
    assert request.headers["x-client-request-id"] == "shared-cache-lineage"
    # The routing hint rides on headers only — never on the request body.
    request_body = json.loads(request.content)
    assert "conversation_id" not in request_body
    assert "prompt_cache_affinity_id" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_cache_scope_headers_without_conversation() -> None:
    """Absent a conversation id, no cache-scope headers are sent (no blank values)."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response(
            {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    request = route.calls.last.request
    assert "session_id" not in request.headers
    assert "x-client-request-id" not in request.headers


@pytest.mark.asyncio
async def test_codex_websocket_reuses_connection_and_sends_only_new_tool_result() -> None:
    websocket = _FakeCodexWebSocket(
        [
            [
                _codex_output_item_event(0, dict(_CODEX_REASONING_ITEM)),
                _codex_output_item_event(1, dict(_CODEX_TOOL_CALL_ITEM)),
                _codex_completed_event(
                    "resp_1",
                    [],
                ),
            ],
            [_codex_completed_event("resp_2", [dict(_CODEX_FINAL_MESSAGE_ITEM)])],
        ]
    )
    connector = _FakeCodexWebSocketConnector([websocket])
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )

    first_raw = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-42",
        thinking_effort="high",
        tools=_CODEX_TOOLS,
    )
    first = adapter.normalize_response(first_raw)
    second_raw = await adapter.send(
        _messages_with_codex_tool_result(first),
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-42",
        thinking_effort="high",
        tools=_CODEX_TOOLS,
    )
    second = adapter.normalize_response(second_raw)

    assert second["content"] == "Done"
    assert len(connector.calls) == 1
    assert len(websocket.sent_payloads) == 2
    first_payload, second_payload = websocket.sent_payloads
    assert first_payload["type"] == "response.create"
    assert first_payload["store"] is False
    assert "previous_response_id" not in first_payload
    assert second_payload["previous_response_id"] == "resp_1"
    assert second_payload["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok":true,"data":{"level":91}}',
        }
    ]
    _, connect_kwargs = connector.calls[0]
    websocket_headers = connect_kwargs["additional_headers"]
    assert websocket_headers["OpenAI-Beta"] == CODEX_WEBSOCKET_BETA
    assert websocket_headers["session-id"] == "orchestrator:sess-42"
    assert "session_id" not in websocket_headers
    await adapter.aclose()
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_codex_shared_cache_affinity_does_not_share_websocket_continuation() -> None:
    source_websocket = _FakeCodexWebSocket(
        [
            [
                _codex_output_item_event(0, dict(_CODEX_REASONING_ITEM)),
                _codex_output_item_event(1, dict(_CODEX_TOOL_CALL_ITEM)),
                _codex_completed_event("resp_source", []),
            ]
        ]
    )
    fork_websocket = _FakeCodexWebSocket(
        [[_codex_completed_event("resp_fork", [dict(_CODEX_FINAL_MESSAGE_ITEM)])]]
    )
    connector = _FakeCodexWebSocketConnector([source_websocket, fork_websocket])
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )

    source_raw = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:source",
        prompt_cache_affinity_id="shared-cache-lineage",
        thinking_effort="high",
        tools=_CODEX_TOOLS,
    )
    source = adapter.normalize_response(source_raw)
    await adapter.send(
        _messages_with_codex_tool_result(source),
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:fork",
        prompt_cache_affinity_id="shared-cache-lineage",
        thinking_effort="high",
        tools=_CODEX_TOOLS,
    )

    assert len(connector.calls) == 2
    assert source_websocket.closed is True
    assert "previous_response_id" not in source_websocket.sent_payloads[0]
    assert "previous_response_id" not in fork_websocket.sent_payloads[0]
    assert fork_websocket.sent_payloads[0]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok":true,"data":{"level":91}}',
    }
    for _url, connect_kwargs in connector.calls:
        headers = connect_kwargs["additional_headers"]
        assert headers["session-id"] == "shared-cache-lineage"
        assert headers["x-client-request-id"] == "shared-cache-lineage"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_codex_websocket_exchange_keeps_canonical_debug_trace(
    tmp_path: Path,
) -> None:
    websocket = _FakeCodexWebSocket(
        [[_codex_completed_event("resp_1", [dict(_CODEX_FINAL_MESSAGE_ITEM)])]]
    )
    connector = _FakeCodexWebSocketConnector([websocket])
    debug_store = DebugTraceStore(tmp_path, trace_limit=10)
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
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
            model_id="gpt-5.6-terra",
            streaming=True,
            iteration_number=1,
        )
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-42",
    )

    traces = debug_store.get_traces()
    assert len(traces) == 1
    trace = debug_store.get_trace(traces[0]["trace_id"])
    assert trace["request"]["method"] == "WEBSOCKET"
    assert trace["request"]["url"] == "wss://chatgpt.com/backend-api/codex/responses"
    assert json.loads(trace["request"]["body"])["type"] == "response.create"
    assert trace["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert trace["response"]["status_code"] == 101
    assert json.loads(trace["response"]["body"])["type"] == "response.completed"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_codex_websocket_clamps_cache_scope_headers_to_openai_limit() -> None:
    websocket = _FakeCodexWebSocket(
        [[_codex_completed_event("resp_1", [dict(_CODEX_FINAL_MESSAGE_ITEM)])]]
    )
    connector = _FakeCodexWebSocketConnector([websocket])
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )
    conversation_id = "orchestrator:" + ("session-" * 20)

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-terra",
        conversation_id=conversation_id,
    )

    _, connect_kwargs = connector.calls[0]
    websocket_headers = connect_kwargs["additional_headers"]
    assert websocket_headers["session-id"] == conversation_id[:64]
    assert websocket_headers["x-client-request-id"] == conversation_id[:64]
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_codex_websocket_failure_before_events_disables_route_and_falls_back_to_sse() -> None:
    connector = _FakeCodexWebSocketConnector([OSError("upgrade unavailable")])
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        side_effect=[
            _codex_sse_response(
                {
                    "id": response_id,
                    "status": "completed",
                    "output": [dict(_CODEX_FINAL_MESSAGE_ITEM)],
                }
            )
            for response_id in ("resp_sse_1", "resp_sse_2")
        ]
    )

    for _ in range(2):
        raw = await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-5.6-terra",
            conversation_id="orchestrator:sess-42",
        )
        assert adapter.normalize_response(raw)["content"] == "Done"

    assert len(connector.calls) == 1
    assert route.call_count == 2
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_codex_websocket_failure_after_event_disables_route_for_next_attempt() -> None:
    websocket = _FakeCodexWebSocket(
        [
            [
                {"type": "response.created", "response": {"id": "resp_started"}},
                OSError("socket dropped"),
            ]
        ]
    )
    connector = _FakeCodexWebSocketConnector([websocket])
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )
    sse_route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response(
            {
                "id": "should_not_run",
                "status": "completed",
                "output": [dict(_CODEX_FINAL_MESSAGE_ITEM)],
            }
        )
    )

    with pytest.raises(NetworkError, match="socket dropped"):
        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="gpt-5.6-terra",
            conversation_id="orchestrator:sess-42",
        )

    assert sse_route.call_count == 0
    assert websocket.closed is True

    raw = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-42",
    )

    assert adapter.normalize_response(raw)["content"] == "Done"
    assert sse_route.call_count == 1
    assert len(connector.calls) == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_codex_websocket_missing_continuation_reconnects_with_full_context() -> None:
    first_websocket = _FakeCodexWebSocket(
        [
            [
                _codex_completed_event(
                    "resp_1",
                    [dict(_CODEX_REASONING_ITEM), dict(_CODEX_TOOL_CALL_ITEM)],
                )
            ],
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
    replacement_websocket = _FakeCodexWebSocket(
        [[_codex_completed_event("resp_2", [dict(_CODEX_FINAL_MESSAGE_ITEM)])]]
    )
    connector = _FakeCodexWebSocketConnector([first_websocket, replacement_websocket])
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )

    first_raw = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-42",
        tools=_CODEX_TOOLS,
    )
    first = adapter.normalize_response(first_raw)
    second_raw = await adapter.send(
        _messages_with_codex_tool_result(first),
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-42",
        tools=_CODEX_TOOLS,
    )

    assert adapter.normalize_response(second_raw)["content"] == "Done"
    assert len(connector.calls) == 2
    assert first_websocket.sent_payloads[1]["previous_response_id"] == "resp_1"
    replay = replacement_websocket.sent_payloads[0]
    assert "previous_response_id" not in replay
    assert [item.get("type", item.get("role")) for item in replay["input"]] == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert first_websocket.closed is True
    await adapter.aclose()


@pytest.mark.parametrize(
    ("first_conversation", "second_conversation", "first_model", "second_model"),
    [
        ("orchestrator:sess-1", "orchestrator:sess-2", "gpt-5.6-terra", "gpt-5.6-terra"),
        ("orchestrator:sess-1", "orchestrator:sess-1", "gpt-5.6-terra", "gpt-5.6-sol"),
    ],
)
@pytest.mark.asyncio
async def test_codex_websocket_never_chains_across_conversation_or_model_change(
    first_conversation: str,
    second_conversation: str,
    first_model: str,
    second_model: str,
) -> None:
    first_websocket = _FakeCodexWebSocket(
        [
            [
                _codex_completed_event(
                    "resp_1",
                    [dict(_CODEX_REASONING_ITEM), dict(_CODEX_TOOL_CALL_ITEM)],
                )
            ]
        ]
    )
    second_websocket = _FakeCodexWebSocket(
        [[_codex_completed_event("resp_2", [dict(_CODEX_FINAL_MESSAGE_ITEM)])]]
    )
    connector = _FakeCodexWebSocketConnector([first_websocket, second_websocket])
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )

    first_raw = await adapter.send(
        SAMPLE_MESSAGES,
        model_id=first_model,
        conversation_id=first_conversation,
        tools=_CODEX_TOOLS,
    )
    first = adapter.normalize_response(first_raw)
    await adapter.send(
        _messages_with_codex_tool_result(first),
        model_id=second_model,
        conversation_id=second_conversation,
        tools=_CODEX_TOOLS,
    )

    assert len(connector.calls) == 2
    second_payload = second_websocket.sent_payloads[0]
    assert "previous_response_id" not in second_payload
    assert len(second_payload["input"]) == 4
    assert first_websocket.closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_codex_websocket_never_chains_across_account_change() -> None:
    first_websocket = _FakeCodexWebSocket(
        [
            [
                _codex_completed_event(
                    "resp_1",
                    [dict(_CODEX_REASONING_ITEM), dict(_CODEX_TOOL_CALL_ITEM)],
                )
            ]
        ]
    )
    second_websocket = _FakeCodexWebSocket(
        [[_codex_completed_event("resp_2", [dict(_CODEX_FINAL_MESSAGE_ITEM)])]]
    )
    connector = _FakeCodexWebSocketConnector([first_websocket, second_websocket])
    token_getter = _RotatingTokenGetter(
        [_jwt_with_account("acct_one"), _jwt_with_account("acct_two")]
    )
    adapter = OpenAIAdapter(
        _subscription_config(),
        token_getter,
        connection_mode=CODEX_RESPONSES_MODE,
        codex_websocket_connect=connector,
    )

    first_raw = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-1",
        tools=_CODEX_TOOLS,
    )
    first = adapter.normalize_response(first_raw)
    await adapter.send(
        _messages_with_codex_tool_result(first),
        model_id="gpt-5.6-terra",
        conversation_id="orchestrator:sess-1",
        tools=_CODEX_TOOLS,
    )

    assert len(connector.calls) == 2
    assert connector.calls[0][1]["additional_headers"]["chatgpt-account-id"] == "acct_one"
    assert connector.calls[1][1]["additional_headers"]["chatgpt-account-id"] == "acct_two"
    second_payload = second_websocket.sent_payloads[0]
    assert "previous_response_id" not in second_payload
    assert len(second_payload["input"]) == 4
    assert first_websocket.closed is True
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_completions_send_ignores_conversation_id() -> None:
    """The ``api-key`` path drops the conversation id — never onto the wire."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    route = respx.post(OPENAI_API_KEY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "Hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-4o", conversation_id="orchestrator:sess-42")

    request = route.calls.last.request
    assert "session_id" not in request.headers
    assert "conversation_id" not in json.loads(request.content)


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("gpt-5.5", "full_history"),
        ("gpt-5.6", "full_history"),
        ("gpt-5.6-sol", "full_history"),
        ("gpt-5.6-terra", "full_history"),
        ("gpt-5.6-luna", "full_history"),
    ],
)
def test_reasoning_replay_policy_defaults_to_full_history_across_connections(
    model_id: str,
    expected: str,
) -> None:
    platform = OpenAIAdapter(
        _platform_config(),
        "sk-test",
        model_lookup=_model_lookup_with_openai_wire_policies,
    )
    subscription = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account(),
        connection_mode=CODEX_RESPONSES_MODE,
        model_lookup=_model_lookup_with_openai_wire_policies,
    )

    assert platform.reasoning_replay_policy(model_id) == expected
    assert subscription.reasoning_replay_policy(model_id) == expected


@respx.mock
@pytest.mark.asyncio
async def test_platform_gpt_5_6_uses_responses_all_turns_and_preserves_phase() -> None:
    adapter = OpenAIAdapter(
        _platform_config(),
        "sk-test",
        model_lookup=_model_lookup_with_openai_wire_policies,
    )
    output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Done."}],
        },
    ]
    route = respx.post(OPENAI_PLATFORM_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "reasoning": {"context": "all_turns"},
                "output": output,
            },
        )
    )

    response = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-sol",
        thinking_effort="high",
    )
    payload = json.loads(route.calls.last.request.content)
    normalized = adapter.normalize_response(response, model_id="gpt-5.6-sol")

    assert payload["reasoning"] == {
        "effort": "high",
        "summary": "auto",
        "context": "all_turns",
    }
    assert payload["store"] is False
    assert payload.get("stream", False) is False
    assert normalized["phase"] == "final_answer"
    assert normalized["reasoning_meta"]["reasoning_context"] == "all_turns"
    assert normalized["reasoning_meta"]["response_output"] == output


@respx.mock
@pytest.mark.asyncio
async def test_subscription_gpt_5_6_does_not_assume_public_reasoning_context() -> None:
    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account(),
        connection_mode=CODEX_RESPONSES_MODE,
        model_lookup=_model_lookup_with_openai_wire_policies,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_sub_56", "status": "completed", "output": []})
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.6-sol",
        thinking_effort="high",
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert adapter.reasoning_replay_policy("gpt-5.6-sol") == "full_history"


@respx.mock
@pytest.mark.asyncio
async def test_platform_gpt_5_5_uses_responses_without_all_turns() -> None:
    adapter = OpenAIAdapter(
        _platform_config(),
        "sk-test",
        model_lookup=_model_lookup_with_openai_wire_policies,
    )
    route = respx.post(OPENAI_PLATFORM_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_55",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Checking."}],
                    }
                ],
            },
        )
    )

    response = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5")
    payload = json.loads(route.calls.last.request.content)
    normalized = adapter.normalize_response(response, model_id="gpt-5.5")

    assert "reasoning" not in payload
    assert normalized["phase"] == "commentary"


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_preserves_xhigh_reasoning_effort() -> None:
    """GPT-5.5 advertises xhigh reasoning through the Codex models endpoint."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5", thinking_effort="xhigh")

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_snaps_against_effective_model_ladder() -> None:
    """A subscription model with a feed ladder snaps within it, not the constant.

    A model whose effective ladder is ``[low, medium]`` snaps ``xhigh`` down to
    ``medium`` — the ``OPENAI_SUBSCRIPTION_REASONING_EFFORTS`` constant (which
    carries ``xhigh``) is bypassed in favor of the per-model ladder.
    """
    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
        model_lookup=_subscription_model_lookup(("low", "medium")),
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5", thinking_effort="xhigh")

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_falls_back_to_constant_without_feed_ladder() -> None:
    """A subscription reasoning model with no feed ladder uses the constant floor.

    ``xhigh`` is inside ``OPENAI_SUBSCRIPTION_REASONING_EFFORTS`` but outside a
    narrow ladder; with an empty ladder it survives as ``xhigh``, proving the
    constant floor is used when the looked-up model has no ladder.
    """
    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
        model_lookup=_subscription_model_lookup(()),
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.5", thinking_effort="xhigh")

    payload = json.loads(route.calls.last.request.content)
    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_adds_default_instructions_without_system_message() -> None:
    """The Codex backend requires instructions even without a system prompt."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send([{"role": "user", "content": "Hello"}], model_id="gpt-5.5")

    payload = json.loads(route.calls.last.request.content)
    assert payload["instructions"] == OPENAI_SUBSCRIPTION_DEFAULT_INSTRUCTIONS
    assert payload["store"] is False
    assert "max_output_tokens" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_unsupported_output_token_limits() -> None:
    """The Codex backend rejects Responses output-token limit parameters."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.5",
        max_tokens=2048,
        max_output_tokens=1024,
    )

    payload = json.loads(route.calls.last.request.content)
    assert "max_output_tokens" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_unspecified_top_p() -> None:
    """An unspecified Chat-loop top_p must not become a Codex body field."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.6-luna", top_p=None)

    payload = json.loads(route.calls.last.request.content)
    assert "top_p" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_omits_unsupported_top_p() -> None:
    """The Codex backend rejects sampling top_p for subscription models."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=_codex_sse_response({"id": "resp_1", "status": "completed", "output": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.6-luna", top_p=0.9)

    payload = json.loads(route.calls.last.request.content)
    assert "top_p" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_codex_send_rejects_oauth_token_without_account_id() -> None:
    """Subscription requests need the ChatGPT account id claim from the OAuth JWT."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        "not-a-jwt",
        connection_mode=CODEX_RESPONSES_MODE,
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(ProviderAuthError):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-codex")

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_codex_stream_yields_normalized_responses_deltas() -> None:
    """stream() parses Responses SSE events from the Codex backend."""

    access_token = _jwt_with_account("acct_openai")
    adapter = OpenAIAdapter(
        _subscription_config(),
        access_token,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hel"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed",'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    )
    respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for chunk in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-codex"):
        chunks.append(chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Hel"},
        {"type": "reasoning_meta", "reasoning_meta": {"response_id": "resp_1"}},
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_codex_stream_rebuilds_headers_per_connect_attempt() -> None:
    """A retried Codex stream connect re-consults the token getter (OAuth refresh)."""

    token_getter = _RotatingTokenGetter(
        [_jwt_with_account("acct-stale"), _jwt_with_account("acct-fresh")]
    )
    adapter = OpenAIAdapter(
        _subscription_config(),
        token_getter,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n'
    )
    route = respx.post(OPENAI_SUBSCRIPTION_URL).mock(
        side_effect=[
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"}),
        ]
    )

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        async for _ in adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-codex"):
            pass

    # The retry rebuilds headers, so the refreshed token's account id is used.
    assert route.call_count == 2
    assert route.calls[0].request.headers.get("chatgpt-account-id") == "acct-stale"
    assert route.calls[1].request.headers.get("chatgpt-account-id") == "acct-fresh"


def test_codex_discovery_headers_merge_extra_headers() -> None:
    """Discovery merges the adapter-owned Codex headers on top of caller headers."""

    access_token = _jwt_with_account("acct_openai")
    headers = OpenAIAdapter.discovery_headers(
        _subscription_config(),
        access_token,
        {"User-Agent": "vbot-test"},
    )

    assert headers["User-Agent"] == "vbot-test"
    assert headers["chatgpt-account-id"] == "acct_openai"
    assert headers["OpenAI-Beta"] == CODEX_EXTRA_HEADERS["OpenAI-Beta"]
    assert headers["originator"] == CODEX_EXTRA_HEADERS["originator"]


# ------------------------------------------------------------------
# Default mode (api-key connection → /chat/completions)
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_default_mode_send_targets_chat_completions_endpoint() -> None:
    """Default mode delegates to the inherited ``/chat/completions`` request."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    route = respx.post(OPENAI_API_KEY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello back",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )
    )

    response = await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-test"
    # Codex-specific headers must NOT leak into the Platform request.
    assert "OpenAI-Beta" not in request.headers
    assert "originator" not in request.headers
    assert "chatgpt-account-id" not in request.headers
    payload = json.loads(request.content)
    assert 0 < payload.pop("max_tokens") < 8192
    assert payload == {
        "model": "gpt-5.2",
        "messages": [
            {"role": "system", "content": "Use concise answers."},
            {"role": "user", "content": "Hello"},
        ],
    }
    assert response == {
        "id": "chatcmpl-1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello back",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }


@respx.mock
@pytest.mark.asyncio
async def test_default_mode_normalize_response_falls_back_to_openai_compatible() -> None:
    """Default-mode normalize_response uses the inherited chat/completions shape."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Hi there",
                }
            }
        ]
    }

    normalized = adapter.normalize_response(response)
    assert normalized == {
        "role": "assistant",
        "content": "Hi there",
        "reasoning": None,
        "reasoning_meta": None,
        "tool_calls": None,
        "terminal_outcome": "unknown",
    }


def test_default_mode_inherits_connection_mode_none() -> None:
    """Without ``connection_mode`` the adapter defaults to chat/completions mode."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    assert adapter._connection_mode is None


def test_codex_mode_stores_connection_mode() -> None:
    """The adapter records the connection mode set at construction time."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
    )
    assert adapter._connection_mode == CODEX_RESPONSES_MODE


def test_chat_mode_wire_media_supports_images_audio_and_pdf() -> None:
    """The ``/chat/completions`` wire carries images, the OpenAI audio formats, and PDF."""

    adapter = OpenAIAdapter(_platform_config(), "sk-test")
    assert adapter.wire_media_support("gpt-5.2") == (
        IMAGE_WIRE_MEDIA_TYPES | frozenset({"audio/wav", "audio/mpeg", "application/pdf"})
    )


def test_codex_mode_wire_media_is_image_only() -> None:
    """The Codex Responses wire carries images only — no native audio or PDF."""

    adapter = OpenAIAdapter(
        _subscription_config(),
        _jwt_with_account("acct_openai"),
        connection_mode=CODEX_RESPONSES_MODE,
    )
    assert adapter.wire_media_support("gpt-5.2-codex") == IMAGE_WIRE_MEDIA_TYPES
