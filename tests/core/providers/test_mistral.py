"""Tests for MistralAdapter."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped
from core.providers.mistral import MistralAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-mistral-key"
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]


@pytest.fixture()
def mistral_config() -> ProviderConfig:
    return ProviderConfig(
        id="mistral",
        name="Mistral AI",
        adapter="mistral",
        base_url="https://api.mistral.ai/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="MISTRAL_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


@pytest.fixture()
def mistral_adapter(mistral_config: ProviderConfig) -> MistralAdapter:
    return MistralAdapter(mistral_config, API_KEY)


@pytest.fixture()
def mistral_adapter_with_reasoning_lookup(mistral_config: ProviderConfig) -> MistralAdapter:
    def _model_reasoning_supported(model_id: str) -> bool | None:
        if model_id == "mistral-medium-latest":
            return False
        if model_id.startswith("magistral-medium"):
            return True
        return None

    return MistralAdapter(
        mistral_config,
        API_KEY,
        model_reasoning_supported_lookup=_model_reasoning_supported,
    )


def raw_mistral_model(
    *,
    model_id: str = "mistral-large-latest",
    name: str = "Mistral Large",
    completion_chat: bool = True,
    function_calling: bool = True,
    reasoning: bool = False,
    vision: bool = True,
    archived: bool = False,
    max_context_length: int | None = 128000,
) -> dict:
    raw = {
        "id": model_id,
        "name": name,
        "capabilities": {
            "completion_chat": completion_chat,
            "function_calling": function_calling,
            "reasoning": reasoning,
            "vision": vision,
        },
        "archived": archived,
    }
    if max_context_length is not None:
        raw["max_context_length"] = max_context_length
    return raw


def test_normalize_catalog_entry_maps_chat_model_capabilities() -> None:
    model = MistralAdapter.normalize_catalog_entry(raw_mistral_model(), {"max_tokens": 8192})

    assert model == Model(
        model_id="mistral-large-latest",
        name="Mistral Large",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=128000,
        max_output_tokens=8192,
    )


def test_normalize_catalog_entry_marks_magistral_models_as_reasoning_capable() -> None:
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(
            model_id="magistral-medium-latest",
            name="Magistral Medium",
            reasoning=True,
        ),
        {"max_tokens": 8192},
    )

    assert model.capabilities.reasoning.supported is True


def test_normalize_catalog_entry_marks_non_magistral_reasoning_models_as_reasoning_capable() -> (
    None
):
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(
            model_id="mistral-small-2603",
            name="mistral-small-2603",
            reasoning=True,
        ),
        {"max_tokens": 8192},
    )

    assert model.capabilities.reasoning.supported is True


def test_normalize_catalog_entry_rejects_non_chat_models() -> None:
    with pytest.raises(CatalogEntrySkipped, match="Skipped non-chat model"):
        MistralAdapter.normalize_catalog_entry(
            raw_mistral_model(completion_chat=False),
            {"max_tokens": 8192},
        )


def test_normalize_catalog_entry_rejects_archived_models() -> None:
    with pytest.raises(CatalogEntrySkipped, match="Skipped non-chat model"):
        MistralAdapter.normalize_catalog_entry(
            raw_mistral_model(archived=True),
            {"max_tokens": 8192},
        )


def test_normalize_catalog_entry_defaults_missing_context_window_to_zero() -> None:
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(max_context_length=None),
        {"max_tokens": 8192},
    )

    assert model.context_window == 0


def test_normalize_catalog_entry_uses_provider_default_for_missing_max_output_tokens() -> None:
    model = MistralAdapter.normalize_catalog_entry(raw_mistral_model(), {"max_tokens": 8192})

    assert model.max_output_tokens == 8192


def test_normalize_catalog_entry_sets_json_mode_true_for_chat_models() -> None:
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(function_calling=False, vision=False),
        {"max_tokens": 8192},
    )

    assert model.capabilities.json_mode is True


@pytest.mark.parametrize("thinking_effort", ["high", "xhigh", "max", "medium"])
@respx.mock
@pytest.mark.asyncio
async def test_build_payload_maps_supported_reasoning_efforts_to_high(
    mistral_adapter: MistralAdapter,
    thinking_effort: str,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-large-latest",
        thinking_effort=thinking_effort,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_does_not_set_reasoning_effort_for_low(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-large-latest",
        thinking_effort="low",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_does_not_set_reasoning_effort_for_minimal(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-large-latest",
        thinking_effort="minimal",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_sets_reasoning_effort_none_when_disabled(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-large-latest",
        thinking_effort="none",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "none"


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_uses_prompt_mode_reasoning(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["prompt_mode"] == "reasoning"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_2509_uses_prompt_mode_reasoning(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-2509",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["prompt_mode"] == "reasoning"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_small_uses_reasoning_effort(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="magistral-small-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_mistral_small_uses_reasoning_effort(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-small-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_mistral_medium_suppresses_reasoning_when_lookup_disables_it(
    mistral_adapter_with_reasoning_lookup: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_reasoning_lookup.send(
        SAMPLE_MESSAGES,
        model_id="mistral-medium-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_lookup_keeps_prompt_mode_reasoning(
    mistral_adapter_with_reasoning_lookup: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_reasoning_lookup.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["prompt_mode"] == "reasoning"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_none_effort_sends_no_reasoning_params(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-latest",
        thinking_effort="none",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_omits_reasoning_effort_when_not_provided(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(SAMPLE_MESSAGES, model_id="mistral-large-latest")

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_send_returns_normal_response(mistral_adapter: MistralAdapter) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    response = await mistral_adapter.send(SAMPLE_MESSAGES, model_id="mistral-large-latest")

    assert route.called
    assert response == SUCCESS_RESPONSE


def test_normalize_response_extracts_message_thinking_as_reasoning(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Final answer",
                    "thinking": "Reasoning trace",
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["reasoning"] == "Reasoning trace"


def test_normalize_response_typed_list_extracts_text_and_reasoning(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "ThinkA"},
                        {"type": "text", "text": "AnswerA"},
                    ],
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["content"] == "AnswerA"
    assert normalized["reasoning"] == "ThinkA"


def test_normalize_response_typed_list_multiple_text_blocks_concatenated(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "A"},
                        {"type": "text", "text": "B"},
                    ],
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["content"] == "AB"


def test_normalize_response_string_content_delegates_to_base(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "plain string",
                    "thinking": "Reasoning trace",
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["content"] == "plain string"
    assert normalized["reasoning"] == "Reasoning trace"


@respx.mock
@pytest.mark.asyncio
async def test_stream_requests_usage_and_yields_content_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    sse_body = 'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    route = respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for chunk in mistral_adapter.stream(SAMPLE_MESSAGES, model_id="mistral-large-latest"):
        chunks.append(chunk)

    request_body = json.loads(route.calls.last.request.content)
    assert chunks == [{"type": "content_delta", "text": "Hi"}]
    assert request_body["stream"] is True
    assert request_body["stream_options"] == {"include_usage": True}


@respx.mock
@pytest.mark.asyncio
async def test_stream_yields_reasoning_delta_for_delta_thinking(
    mistral_adapter: MistralAdapter,
) -> None:
    sse_body = 'data: {"choices":[{"delta":{"thinking":"Reasoning delta"}}]}\n\ndata: [DONE]\n\n'
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for chunk in mistral_adapter.stream(SAMPLE_MESSAGES, model_id="mistral-large-latest"):
        chunks.append(chunk)

    assert chunks == [{"type": "reasoning_delta", "text": "Reasoning delta"}]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_list_delta_thinking_yields_reasoning_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": [
                        {"type": "thinking", "thinking": "Think1"},
                    ]
                }
            }
        ]
    }
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [{"type": "reasoning_delta", "text": "Think1"}]


@respx.mock
@pytest.mark.asyncio
async def test_stream_typed_list_delta_text_yields_content_delta(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": [
                        {"type": "text", "text": "Text1"},
                    ]
                }
            }
        ]
    }
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [{"type": "content_delta", "text": "Text1"}]


@respx.mock
@pytest.mark.asyncio
async def test_stream_string_content_delta_delegates_to_base(
    mistral_adapter: MistralAdapter,
) -> None:
    chunk = {"choices": [{"delta": {"content": "Hi"}}]}
    sse_body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    respx.post(MISTRAL_URL).mock(
        return_value=httpx.Response(
            200,
            text=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )

    chunks = []
    async for stream_chunk in mistral_adapter.stream(
        SAMPLE_MESSAGES, model_id="mistral-large-latest"
    ):
        chunks.append(stream_chunk)

    assert chunks == [{"type": "content_delta", "text": "Hi"}]
