"""Openai: websocket behavior."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import respx

from core.debug.recorder import DebugContext, ProviderDebugRecorder
from core.debug.store import DebugTraceStore
from core.providers.errors import NetworkError
from core.providers.openai import (
    CODEX_RESPONSES_MODE,
    CODEX_WEBSOCKET_BETA,
    OpenAIAdapter,
)
from tests.core.providers.openai_helpers import (
    _CODEX_TOOLS,
    OPENAI_SUBSCRIPTION_URL,
    SAMPLE_MESSAGES,
    _codex_sse_response,
    _jwt_with_account,
    _RotatingTokenGetter,
    _subscription_config,
)


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
