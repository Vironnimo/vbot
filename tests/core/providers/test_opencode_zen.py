"""OpenCode Zen routing, Gemini wire, catalog, and error-policy tests."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
import respx

import core.providers.opencode_zen as zen_module
from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import (
    CatalogEntrySkipped,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

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
        "input_tokens": 100,
        "output_tokens": 20,
        "reasoning_tokens": 12,
        "cache_read_tokens": 20,
    }


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


def test_catalog_policy_is_exact_and_marks_privacy_and_deprecation() -> None:
    responses = OpenCodeZenAdapter.normalize_catalog_entry({"id": "gpt-5.6-sol"}, {})
    messages = OpenCodeZenAdapter.normalize_catalog_entry({"id": "claude-opus-4-1"}, {})
    gemini = OpenCodeZenAdapter.normalize_catalog_entry({"id": "gemini-3.5-flash"}, {})
    free = OpenCodeZenAdapter.normalize_catalog_entry({"id": "big-pickle"}, {})

    assert responses.metadata["opencode_zen"]["protocol"] == "responses"
    assert messages.metadata["opencode_zen"]["protocol"] == "messages"
    assert messages.metadata["opencode_zen"]["deprecates_at"] == "2026-08-05"
    assert gemini.metadata["opencode_zen"]["protocol"] == "gemini_generate_content"
    assert "reasoning_replay" not in gemini.metadata["opencode_zen"]
    assert free.metadata["opencode_zen"]["privacy"] == "free_model_data_collection"

    with pytest.raises(CatalogEntrySkipped):
        OpenCodeZenAdapter.normalize_catalog_entry({"id": "glm-5"}, {})
    with pytest.raises(CatalogEntrySkipped):
        OpenCodeZenAdapter.normalize_catalog_entry({"id": "future-model"}, {})


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
