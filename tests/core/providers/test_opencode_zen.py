"""OpenCode Zen routing, Gemini wire, catalog, and error-policy tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import core.providers.opencode_zen as zen_module
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers._opencode_zen_gemini import _normalize_gemini_stream_chunk
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import (
    CatalogEntrySkipped,
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.tools import tool_failure, tool_success
from core.utils.retry import caller_owns_retries

BASE_URL = "https://opencode.ai/zen/v1"
RESPONSES_URL = f"{BASE_URL}/responses"
MESSAGES_URL = f"{BASE_URL}/messages"
CHAT_URL = f"{BASE_URL}/chat/completions"
GEMINI_URL = f"{BASE_URL}/models/gemini-3.5-flash:generateContent"
GEMINI_STREAM_URL = f"{BASE_URL}/models/gemini-3.5-flash:streamGenerateContent?alt=sse"


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="opencode-zen",
        name="OpenCode Zen",
        adapter="opencode_zen",
        base_url=BASE_URL,
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


def _model(model_id: str) -> Model:
    normalized = OpenCodeZenAdapter.normalize_catalog_entry({"id": model_id}, {})
    return replace(
        normalized,
        capabilities=Capabilities(
            vision=model_id.startswith("gemini"),
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control="levels",
                levels=("minimal", "low", "medium", "high"),
            ),
            input_modalities=("text", "image") if model_id.startswith("gemini") else ("text",),
            output_modalities=("text",),
        ),
        context_window=1_048_576,
        max_output_tokens=65_536,
    )


@pytest.fixture()
def adapter() -> OpenCodeZenAdapter:
    models = {
        model_id: _model(model_id)
        for model_id in (
            "gpt-5.6-sol",
            "claude-sonnet-5",
            "deepseek-v4-flash",
            "gemini-3.5-flash",
        )
    }
    return OpenCodeZenAdapter(_config(), "zen-secret", model_lookup=models.get)


def test_public_package_exports_opencode_zen_adapter() -> None:
    from core.providers import OpenCodeZenAdapter as PublicOpenCodeZenAdapter

    assert PublicOpenCodeZenAdapter is OpenCodeZenAdapter


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ReadError, NetworkError),
        (httpx.ReadTimeout, ProviderTimeoutError),
        (asyncio.CancelledError, asyncio.CancelledError),
    ],
)
async def test_gemini_rejected_stream_closes_when_error_body_read_fails(
    adapter: OpenCodeZenAdapter,
    failure: type[BaseException],
    expected: type[BaseException],
) -> None:
    class BrokenBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial error"
            raise failure("body interrupted")

        async def aclose(self) -> None:
            self.closed = True

    body = BrokenBody()
    route = respx.post(GEMINI_STREAM_URL).mock(return_value=httpx.Response(503, stream=body))
    with caller_owns_retries(), pytest.raises(expected):
        _ = [delta async for delta in adapter.stream([], model_id="gemini-3.5-flash")]
    assert body.closed
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id", ["gpt-5.6-sol", "claude-sonnet-5", "deepseek-v4-flash", "gemini-3.5-flash"]
)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("error_field", ["name", "type"])
@pytest.mark.parametrize(
    "error_code", ["CreditsError", "MonthlyLimitError", "UserLimitError", "ModelError"]
)
async def test_oauth_does_not_refresh_zen_entitlement_401(
    model_id: str,
    streaming: bool,
    error_code: str,
    error_field: str,
) -> None:
    class Getter:
        async def __call__(self) -> str:
            return "test-token"

        async def refresh_after_rejection(
            self, _rejected: str, *, status_code: int, response_body: str
        ) -> str | None:
            if status_code != 401:
                return None
            raise AssertionError("An entitlement failure cannot refresh credentials")

    adapter = OpenCodeZenAdapter(_config(), Getter(), model_lookup=_model)
    route = respx.route(method="POST").mock(
        return_value=httpx.Response(401, json={"error": {error_field: error_code}})
    )
    try:
        with pytest.raises(ProviderError) as caught:
            if streaming:
                _ = [
                    delta
                    async for delta in adapter.stream(
                        [{"role": "user", "content": "test"}], model_id=model_id
                    )
                ]
            else:
                await adapter.send([{"role": "user", "content": "test"}], model_id=model_id)
        assert not isinstance(caught.value, ProviderAuthError)
        assert caught.value.retryable is False
        assert route.call_count == 1
    finally:
        await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_responses_model_uses_responses_wire_and_bearer_auth(
    adapter: OpenCodeZenAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
        )
    )

    response = await adapter.send(
        [{"role": "user", "content": "hello"}],
        model_id="gpt-5.6-sol",
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "gpt-5.6-sol"
    assert route.calls.last.request.headers["authorization"] == "Bearer zen-secret"
    assert adapter.normalize_response(response, model_id="gpt-5.6-sol")["content"] == "done"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["gpt-6-sol", "gpt-6-luna"])
async def test_gpt6_catalog_models_use_zen_responses_wire(model_id: str) -> None:
    registry = ModelRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    adapter = OpenCodeZenAdapter(
        _config(),
        "zen-secret",
        model_lookup=lambda selected: registry.get("opencode-zen", selected),
    )
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_gpt6",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
        )
    )

    try:
        response = await adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id=model_id,
            thinking_effort="none",
        )
        payload = json.loads(route.calls.last.request.content)
        assert payload["model"] == model_id
        assert payload["reasoning"]["effort"] == "none"
        assert route.calls.last.request.headers["authorization"] == "Bearer zen-secret"
        assert adapter.normalize_response(response, model_id=model_id)["content"] == "done"
    finally:
        await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_messages_model_uses_messages_wire_and_x_api_key(
    adapter: OpenCodeZenAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
    )

    response = await adapter.send(
        [
            {"role": "system", "content": "Be exact"},
            {"role": "user", "content": "hello"},
        ],
        model_id="claude-sonnet-5",
    )

    payload = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["x-api-key"] == "zen-secret"
    assert "authorization" not in route.calls.last.request.headers
    assert "cache_control" in json.dumps(payload)
    assert adapter.normalize_response(response, model_id="claude-sonnet-5")["content"] == "done"


@respx.mock
@pytest.mark.asyncio
async def test_chat_model_uses_chat_completions_wire_and_bearer_auth(
    adapter: OpenCodeZenAdapter,
) -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )

    response = await adapter.send(
        [{"role": "user", "content": "hello"}],
        model_id="deepseek-v4-flash",
    )

    assert route.calls.last.request.headers["authorization"] == "Bearer zen-secret"
    assert adapter.normalize_response(response, model_id="deepseek-v4-flash")["content"] == "done"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected_effort", "wire_effort"),
    [("none", "low"), ("high", "high")],
)
async def test_space_bunny_uses_zen_chat_and_supported_reasoning_effort(
    selected_effort: str,
    wire_effort: str,
) -> None:
    registry = ModelRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    adapter = OpenCodeZenAdapter(
        _config(),
        "zen-secret",
        model_lookup=lambda selected: registry.get("opencode-zen", selected),
    )
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "reasoning_content": "worked",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )

    try:
        response = await adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="space-bunny-free",
            thinking_effort=selected_effort,
        )
        payload = json.loads(route.calls.last.request.content)
        assert payload["model"] == "space-bunny-free"
        assert payload["reasoning_effort"] == wire_effort
        assert (
            adapter.normalize_response(response, model_id="space-bunny-free")["reasoning"]
            == "worked"
        )
        description = adapter.describe_reasoning_render(
            model_lookup=lambda selected: registry.get("opencode-zen", selected),
            model_id="space-bunny-free",
            effort=selected_effort,
            provider_config=_config(),
        )
        assert description.effort_level == wire_effort
    finally:
        await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_gemini_request_preserves_native_tools_media_thinking_and_replay(
    adapter: OpenCodeZenAdapter,
) -> None:
    route = respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "responseId": "gem_1",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "done"}]},
                        "finishReason": "STOP",
                    }
                ],
            },
        )
    )
    replay_parts = [
        {"text": "think", "thought": True, "thoughtSignature": "opaque"},
        {"functionCall": {"id": "call_1", "name": "weather", "args": {"city": "Berlin"}}},
    ]

    response = await adapter.send(
        [
            {"role": "system", "content": "Be exact"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_meta": {"gemini_parts": replay_parts},
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "weather",
                        "arguments": {"city": "Berlin"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"temperature":21}',
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "continue"},
                    {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
                ],
            },
        ],
        model_id="gemini-3.5-flash",
        thinking_effort="high",
        max_output_tokens=70_000,
        temperature=1.2,
        tools=[
            {
                "name": "weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
        tool_choice="required",
    )

    payload = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["x-goog-api-key"] == "zen-secret"
    assert "authorization" not in route.calls.last.request.headers
    assert payload["systemInstruction"] == {"parts": [{"text": "Be exact"}]}
    assert payload["contents"][0] == {"role": "model", "parts": replay_parts}
    assert payload["contents"][1]["role"] == "user"
    assert payload["contents"][1]["parts"][0]["functionResponse"]["response"] == {"temperature": 21}
    assert payload["contents"][1]["parts"][0]["functionResponse"]["name"] == "weather"
    assert payload["contents"][2]["parts"][1] == {
        "inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}
    }
    assert payload["generationConfig"]["maxOutputTokens"] == 65_536
    assert payload["generationConfig"]["thinkingConfig"] == {
        "includeThoughts": True,
        "thinkingLevel": "high",
    }
    assert payload["toolConfig"]["functionCallingConfig"] == {"mode": "ANY"}
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "weather"
    assert adapter.normalize_response(response, model_id="gemini-3.5-flash")["content"] == "done"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("with_media", [False, True])
async def test_gemini_tool_results_keep_literal_json_and_failure_classification(
    adapter: OpenCodeZenAdapter, with_media: bool
) -> None:
    route = respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]
            },
        )
    )
    literal = json.dumps(tool_failure("inner", "Literal file content."), separators=(",", ":"))
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_read", "name": "read", "arguments": {}},
                {"id": "call_missing", "name": "read", "arguments": {}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "content": json.dumps(tool_success({"content": literal})),
        },
        {
            "role": "tool",
            "tool_call_id": "call_missing",
            "content": json.dumps(tool_failure("not_found", "No file x.")),
        },
    ]
    if with_media:
        for message in messages[1:]:
            message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = [
                {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
                {"type": "text", "text": "Supplemental path"},
            ]
    original = json.dumps(messages)

    await adapter.send(messages, model_id="gemini-3.5-flash")

    contents = json.loads(route.calls.last.request.content)["contents"]
    suffix = "\n\nSupplemental path" if with_media else ""
    assert contents[1]["parts"][0]["functionResponse"] == {
        "id": "call_read",
        "name": "read",
        "response": {"output": literal + suffix},
    }
    assert contents[2]["parts"][0]["functionResponse"] == {
        "id": "call_missing",
        "name": "read",
        "response": {"error": "Error (not_found): No file x." + suffix},
    }
    assert len(contents) == (4 if with_media else 3)
    if with_media:
        assert contents[3] == {
            "role": "user",
            "parts": [
                {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
                {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
            ],
        }
    assert json.dumps(messages) == original


def test_gemini_response_normalizes_signature_tools_cache_usage_and_outcome(
    adapter: OpenCodeZenAdapter,
) -> None:
    response = {
        "responseId": "gem_2",
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "text": "think",
                            "thought": True,
                            "thoughtSignature": "opaque",
                        },
                        {
                            "functionCall": {
                                "id": "call_2",
                                "name": "search",
                                "args": {"q": "vBot"},
                            }
                        },
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "cachedContentTokenCount": 20,
            "candidatesTokenCount": 8,
            "thoughtsTokenCount": 12,
        },
    }

    normalized = adapter.normalize_response(response, model_id="gemini-3.5-flash")

    assert normalized["reasoning"] == "think"
    assert normalized["reasoning_meta"]["gemini_parts"][0]["thoughtSignature"] == "opaque"
    assert normalized["tool_calls"] == [
        {"id": "call_2", "name": "search", "arguments": {"q": "vBot"}}
    ]
    assert normalized["terminal_outcome"] == "tool_calls"
    assert normalized["usage"] == {
        "input_tokens": 120,
        "output_tokens": 20,
        "reasoning_tokens": 12,
        "cache_read_tokens": 20,
    }


@pytest.mark.parametrize(
    ("raw_usage", "expected"),
    [
        ({}, None),
        ({"promptTokenCount": 12}, {"input_tokens": 12}),
        ({"candidatesTokenCount": 7}, {"output_tokens": 7}),
        (
            {"promptTokenCount": True, "candidatesTokenCount": 7},
            {"output_tokens": 7},
        ),
        (
            {"promptTokenCount": 12, "candidatesTokenCount": -1},
            {"input_tokens": 12},
        ),
        ({"promptTokenCount": "12", "candidatesTokenCount": None}, None),
        (
            {"candidatesTokenCount": 7, "thoughtsTokenCount": 3},
            {"output_tokens": 10, "reasoning_tokens": 3},
        ),
        ({"thoughtsTokenCount": 3, "cachedContentTokenCount": 4}, None),
        (
            {
                "promptTokenCount": 0,
                "candidatesTokenCount": 0,
                "thoughtsTokenCount": 0,
                "cachedContentTokenCount": 0,
            },
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cache_read_tokens": 0,
            },
        ),
        (
            {"promptTokenCount": 12, "candidatesTokenCount": 7, "thoughtsTokenCount": False},
            {"input_tokens": 12, "output_tokens": 7},
        ),
    ],
)
def test_gemini_usage_preserves_only_reported_valid_counters(
    adapter: OpenCodeZenAdapter,
    raw_usage: dict[str, Any],
    expected: dict[str, int] | None,
) -> None:
    chunk = {"candidates": [{"finishReason": "STOP"}], "usageMetadata": raw_usage}
    normalized = adapter.normalize_response(chunk, model_id="gemini-3.5-flash")
    assert normalized.get("usage") == expected
    deltas, _, _ = _normalize_gemini_stream_chunk(chunk, [], has_tool_calls=False)
    assert [delta for delta in deltas if delta["type"] == "usage"] == (
        [{"type": "usage", **expected}] if expected is not None else []
    )


@pytest.mark.parametrize("finish_reason", [[], {}, ["STOP"], {"reason": "STOP"}])
def test_gemini_malformed_finish_keeps_tool_attempt_and_unknown_outcome(
    adapter: OpenCodeZenAdapter, finish_reason: Any
) -> None:
    from core.chat.streaming import StreamingAccumulator

    chunk = {
        "candidates": [
            {
                "content": {
                    "parts": [{"functionCall": {"id": "call_1", "name": "read", "args": {}}}]
                },
                "finishReason": finish_reason,
            }
        ]
    }
    normalized = adapter.normalize_response(chunk, model_id="gemini-3.5-flash")
    assert normalized["terminal_outcome"] == "unknown"
    assert normalized["tool_calls"] == [{"id": "call_1", "name": "read", "arguments": {}}]
    deltas, _, finished = _normalize_gemini_stream_chunk(chunk, [], has_tool_calls=False)
    accumulator = StreamingAccumulator()
    for delta in deltas:
        accumulator.add_delta(delta)
    fields = accumulator.finalize_assistant_fields()
    assert finished
    assert fields.finish_reason == "unknown"
    assert fields.tool_calls == normalized["tool_calls"]


def test_gemini_response_preserves_malformed_tool_call_as_rejection(
    adapter: OpenCodeZenAdapter,
) -> None:
    normalized = adapter.normalize_response(
        {
            "responseId": "gem_bad",
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": {"functionCall": {"id": "call_bad", "args": ["bad"]}},
                    },
                    "finishReason": "STOP",
                }
            ],
        },
        model_id="gemini-3.5-flash",
    )

    assert normalized["tool_calls"][0]["id"] == "call_bad"
    assert normalized["tool_calls"][0]["name"] == "invalid_tool_call"
    assert normalized["tool_calls"][0]["arguments"] == {}
    assert normalized["tool_calls"][0]["rejection"]["code"] == "malformed_tool_call"


@respx.mock
@pytest.mark.asyncio
async def test_gemini_stream_preserves_reasoning_meta_tools_usage_and_finish(
    adapter: OpenCodeZenAdapter,
) -> None:
    chunks = [
        {
            "responseId": "gem_3",
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "think",
                                "thought": True,
                                "thoughtSignature": "opaque",
                            }
                        ]
                    }
                }
            ],
        },
        {
            "responseId": "gem_3",
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "call_3",
                                    "name": "search",
                                    "args": {"q": "vBot"},
                                }
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 2,
                "thoughtsTokenCount": 3,
            },
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    route = respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )

    deltas = [
        delta
        async for delta in adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id="gemini-3.5-flash",
        )
    ]

    assert route.called
    assert {delta["type"] for delta in deltas} >= {
        "reasoning_delta",
        "reasoning_meta",
        "tool_call_delta",
        "usage",
        "finish",
    }
    finish = next(delta for delta in deltas if delta["type"] == "finish")
    usage = next(delta for delta in deltas if delta["type"] == "usage")
    replay = [delta for delta in deltas if delta["type"] == "reasoning_meta"][-1]
    assert finish["reason"] == "tool_calls"
    assert usage == {
        "type": "usage",
        "input_tokens": 10,
        "output_tokens": 5,
        "reasoning_tokens": 3,
    }
    assert replay["reasoning_meta"]["gemini_parts"][0]["thoughtSignature"] == "opaque"


@respx.mock
@pytest.mark.asyncio
async def test_gemini_stream_preserves_malformed_tool_values_for_chat_rejection(
    adapter: OpenCodeZenAdapter,
) -> None:
    chunk = {
        "responseId": "gem_bad",
        "candidates": [
            {
                "content": {"parts": [{"functionCall": {"id": "call_bad", "args": ["bad"]}}]},
                "finishReason": "STOP",
            }
        ],
    }
    body = f"data: {json.dumps(chunk)}\n\n"
    respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )

    deltas = [
        delta
        async for delta in adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id="gemini-3.5-flash",
        )
    ]

    tool_delta = next(delta for delta in deltas if delta["type"] == "tool_call_delta")
    assert tool_delta == {
        "type": "tool_call_delta",
        "id": "call_bad",
        "name_delta": "",
        "arguments_delta": '["bad"]',
    }


@pytest.mark.parametrize(
    ("model_id", "protocol"),
    [
        ("gpt-6-astra", "responses"),
        ("gpt-6-sol", "responses"),
        ("gpt-6-luna", "responses"),
        ("grok-4.7", "responses"),
        ("muse-spark-1.3", "responses"),
        ("claude-fable-5-1", "messages"),
        ("claude-opus-5-5", "messages"),
        ("qwen3.8-flash", "messages"),
        ("gemini-3.8-flash", "gemini_generate_content"),
        ("glm-5.3-flash", "chat_completions"),
        ("deepseek-v4.1-flash", "chat_completions"),
    ],
)
def test_catalog_uses_reviewed_current_endpoints(model_id: str, protocol: str) -> None:
    model = OpenCodeZenAdapter.normalize_catalog_entry({"id": model_id}, {})
    assert model.metadata["opencode_zen"]["protocol"] == protocol


@pytest.mark.parametrize(
    "model_id",
    [
        "big-pickle",
        "mimo-v2.6-flash-free",
        "muse-spark-1.3-contributor-free",
        "claude-opus-4-1",
        "minimax-m2.5",
        "kimi-k2.5",
        "glm-5",
        "jev-1.13",
        "future-model",
    ],
)
def test_catalog_skips_restricted_retired_and_unreviewed_models(model_id: str) -> None:
    with pytest.raises(CatalogEntrySkipped):
        OpenCodeZenAdapter.normalize_catalog_entry({"id": model_id}, {})


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["big-pickle", "mimo-v2.6-flash-free", "claude-opus-4-1"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_stale_restricted_selection_fails_before_network(
    model_id: str,
    streaming: bool,
) -> None:
    stale_model = replace(_model("deepseek-v4-flash"), model_id=model_id)
    adapter = OpenCodeZenAdapter(_config(), "test-key", model_lookup=lambda _: stale_model)
    try:
        with pytest.raises(ProviderError) as caught:
            if streaming:
                _ = [item async for item in adapter.stream([], model_id=model_id)]
            else:
                await adapter.send([], model_id=model_id)
        assert type(caught.value) is ProviderError
        assert caught.value.retryable is False
        assert not respx.calls
    finally:
        await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id", ["gpt-5.6-sol", "claude-sonnet-5", "deepseek-v4-flash", "gemini-3.5-flash"]
)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("error_field", ["name", "type"])
async def test_free_tier_rejection_is_not_an_auth_failure(
    adapter: OpenCodeZenAdapter,
    model_id: str,
    streaming: bool,
    error_field: str,
) -> None:
    route = respx.route(method="POST").mock(
        return_value=httpx.Response(403, json={"error": {error_field: "FreeTierError"}})
    )
    with pytest.raises(ProviderError) as caught:
        if streaming:
            _ = [item async for item in adapter.stream([], model_id=model_id)]
        else:
            await adapter.send([], model_id=model_id)
    assert type(caught.value) is ProviderError
    assert caught.value.status_code == 403
    assert caught.value.retryable is False
    assert route.call_count == 1
    await adapter.aclose()


def test_unknown_model_is_rejected_without_alias_or_protocol_guess(
    adapter: OpenCodeZenAdapter,
) -> None:
    with pytest.raises(ProviderError) as exc_info:
        adapter._model_protocol("openai/gpt-5.6-sol")

    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    ("kwargs", "_message"),
    [
        ({"temperature": 2.1}, "temperature"),
        ({"top_p": -0.1}, "top_p"),
        ({"top_k": 0}, "top_k"),
        ({"presence_penalty": 2.1}, "presence_penalty"),
        ({"stop": 7}, "stop"),
        ({"logprobs": True}, "does not support"),
    ],
)
def test_gemini_rejects_invalid_or_undocumented_parameters_before_network(
    adapter: OpenCodeZenAdapter,
    kwargs: dict[str, object],
    _message: str,
) -> None:
    with pytest.raises(ProviderError) as exc_info:
        adapter._build_gemini_payload(
            [{"role": "user", "content": "hello"}],
            "gemini-3.5-flash",
            kwargs,
        )

    assert exc_info.value.retryable is False


def test_gemini_enforces_inline_request_size_limit(
    adapter: OpenCodeZenAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zen_module, "_ZEN_INLINE_REQUEST_MAX_BYTES", 100)

    with pytest.raises(ProviderError) as exc_info:
        adapter._build_gemini_payload(
            [{"role": "user", "content": "x" * 200}],
            "gemini-3.5-flash",
            {},
        )

    assert exc_info.value.retryable is False


def test_gemini_rejects_unknown_content_block_before_network(
    adapter: OpenCodeZenAdapter,
) -> None:
    with pytest.raises(ProviderError):
        adapter._build_gemini_payload(
            [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "data:"}}],
                }
            ],
            "gemini-3.5-flash",
            {},
        )


@respx.mock
@pytest.mark.asyncio
async def test_responses_transport_uses_zen_entitlement_error_policy(
    adapter: OpenCodeZenAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"name": "CreditsError", "message": "balance empty"}},
        )
    )

    with pytest.raises(ProviderError, match="account or Model access denied") as exc_info:
        await adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id="gpt-5.6-sol",
        )

    assert not isinstance(exc_info.value, ProviderAuthError)
    assert exc_info.value.retryable is False
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "detail", "error_type", "retryable"),
    [
        (401, "AuthError: invalid api key", ProviderAuthError, False),
        (401, "CreditsError: balance empty", ProviderError, False),
        (403, "RegionError: unsupported country", ProviderError, False),
        (429, "FreeUsageLimitError: daily allowance exhausted", ProviderError, False),
        (429, "RateLimitError: burst limit", ProviderRateLimitError, True),
    ],
)
def test_error_policy_distinguishes_auth_entitlement_region_and_retryable_rate_limit(
    adapter: OpenCodeZenAdapter,
    status: int,
    detail: str,
    error_type: type[ProviderError],
    retryable: bool,
) -> None:
    with pytest.raises(error_type) as exc_info:
        adapter._classify_http_status(
            status,
            detail=detail,
            response_headers=httpx.Headers(),
        )

    assert exc_info.value.retryable is retryable


def test_messages_route_keeps_zen_policy_and_retries_overloaded_529(
    adapter: OpenCodeZenAdapter,
) -> None:
    messages = adapter._messages
    with pytest.raises(ProviderError) as overloaded:
        messages._classify_http_status(
            529,
            detail='529 {"type":"error","error":{"type":"overloaded_error"}}',
            response_headers=httpx.Headers(),
        )
    with pytest.raises(ProviderError) as exhausted:
        messages._classify_http_status(
            429,
            detail="429 FreeUsageLimitError: daily allowance exhausted",
            response_headers=httpx.Headers(),
        )
    with pytest.raises(ProviderError) as chat_route_529:
        adapter._classify_http_status(529, detail="529", response_headers=httpx.Headers())

    assert overloaded.value.retryable is True
    assert exhausted.value.retryable is False
    assert chat_route_529.value.retryable is False


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", [None, "same_response"])
@pytest.mark.parametrize("batched", [False, True])
async def test_gemini_stream_idless_calls_survive_chat_accumulation(
    adapter: OpenCodeZenAdapter, response_id: str | None, batched: bool
) -> None:
    from core.chat.streaming import StreamingAccumulator

    parts = [
        {"functionCall": {"name": "read", "args": {"path": "a"}}},
        {"functionCall": {"name": "search", "args": {"q": "b"}}},
        {"functionCall": {"id": "real_call", "name": "read", "args": {"path": "c"}}},
    ]
    chunks: list[dict[str, Any]] = [
        {"responseId": response_id, "candidates": [{"content": {"parts": group}}]}
        for group in ([parts] if batched else [[part] for part in parts])
    ]
    chunks.append(
        {
            "candidates": [{"finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 100000,
                "cachedContentTokenCount": 95000,
                "candidatesTokenCount": 10,
            },
        }
    )
    respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(
            200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        )
    )
    accumulator = StreamingAccumulator()
    async for delta in adapter.stream(
        [{"role": "user", "content": "test"}], model_id="gemini-3.5-flash"
    ):
        accumulator.add_delta(delta)
    fields = accumulator.finalize_assistant_fields()
    calls = fields.tool_calls
    assert calls is not None
    assert len({call["id"] for call in calls}) == 3
    assert [(call["name"], call["arguments"]) for call in calls] == [
        ("read", {"path": "a"}),
        ("search", {"q": "b"}),
        ("read", {"path": "c"}),
    ]
    assert calls[-1]["id"] == "real_call"
    assert fields.finish_reason == "tool_calls"
    assert fields.usage == {"input_tokens": 100000, "output_tokens": 10, "cache_read_tokens": 95000}
    assert fields.reasoning_meta == {"gemini_parts": parts}


@respx.mock
@pytest.mark.asyncio
async def test_gemini_stream_prompt_block_matches_completed_response(
    adapter: OpenCodeZenAdapter,
) -> None:
    chunk = {
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {"promptTokenCount": 12},
    }
    respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")
    )
    normalized = adapter.normalize_response(chunk, model_id="gemini-3.5-flash")
    deltas = [delta async for delta in adapter.stream([], model_id="gemini-3.5-flash")]
    assert normalized["terminal_outcome"] == "content_filtered"
    assert [delta for delta in deltas if delta["type"] == "finish"] == [
        {"type": "finish", "reason": normalized["terminal_outcome"]}
    ]
    assert {"type": "usage", "input_tokens": 12} in deltas


@pytest.mark.parametrize("arguments", ['{"path":"a"}', "{}{}", "", "not-json"])
def test_gemini_stream_encoded_tool_arguments_match_completed_response(
    adapter: OpenCodeZenAdapter, arguments: str
) -> None:
    from core.chat.streaming import StreamingAccumulator

    chunk = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "id": "call_1",
                                "name": "read",
                                "args": arguments,
                            }
                        }
                    ]
                },
                "finishReason": "STOP",
            }
        ],
    }
    normalized = adapter.normalize_response(chunk, model_id="gemini-3.5-flash")
    deltas, _, _ = _normalize_gemini_stream_chunk(chunk, [], has_tool_calls=False)
    accumulator = StreamingAccumulator()
    for delta in deltas:
        accumulator.add_delta(delta)
    fields = accumulator.finalize_assistant_fields()
    calls = fields.tool_calls
    assert calls is not None
    assert calls[0]["id"] == "call_1"
    assert len({call["id"] for call in calls}) == len(calls)
    assert [{key: value for key, value in call.items() if key != "id"} for call in calls] == [
        {key: value for key, value in call.items() if key != "id"}
        for call in normalized["tool_calls"]
    ]
    assert fields.finish_reason == normalized["terminal_outcome"]
    assert fields.reasoning_meta == normalized["reasoning_meta"]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["failed-after-stop", {"message": "failed-after-stop"}])
async def test_gemini_stream_error_after_stop_cannot_become_success(
    adapter: OpenCodeZenAdapter, error: Any
) -> None:
    chunks = [
        {"candidates": [{"content": {"parts": [{"text": "partial"}]}, "finishReason": "STOP"}]},
        {"error": error},
    ]
    route = respx.post(GEMINI_STREAM_URL).mock(
        return_value=httpx.Response(
            200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        )
    )
    received = []
    with pytest.raises(ProviderError) as exc_info:
        async for delta in adapter.stream([], model_id="gemini-3.5-flash"):
            received.append(delta)
    assert {"type": "finish", "reason": "stop"} in received
    assert not exc_info.value.retryable
    assert "failed-after-stop" in str(exc_info.value)
    assert route.call_count == 1
