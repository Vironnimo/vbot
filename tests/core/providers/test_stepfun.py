"""StepFun request, response, catalog, and transport policy tests."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Model
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.stepfun import (
    STEPFUN_CONTEXT_WINDOW,
    STEPFUN_DIRECT_MODE,
    STEPFUN_PLAN_MODE,
    STEPFUN_ROUTER_MAX_OUTPUT_TOKENS,
    StepFunAdapter,
)

from .adapter_test_support import bind_connection

STEPFUN_DIRECT_CHAT_URL = "https://api.stepfun.com/v1/chat/completions"
STEPFUN_PLAN_CHAT_URL = "https://api.stepfun.com/step_plan/v1/chat/completions"
HELLO = [{"role": "user", "content": "Hello"}]
CHAT_SUCCESS = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
ABSENT = object()


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="stepfun",
        name="StepFun",
        adapter="stepfun",
        base_url="https://api.stepfun.com/v1",
        connections=[
            ConnectionConfig(
                id="direct-api",
                type="api_key",
                label="Direct API",
                mode=STEPFUN_DIRECT_MODE,
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="STEPFUN_DIRECT_API_KEY",
                ),
            ),
            ConnectionConfig(
                id="step-plan",
                type="api_key",
                label="Step Plan",
                base_url="https://api.stepfun.com/step_plan/v1",
                mode=STEPFUN_PLAN_MODE,
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="STEPFUN_API_KEY",
                ),
            ),
        ],
        defaults={"temperature": 0.5},
        context_window=STEPFUN_CONTEXT_WINDOW,
    )


def _models() -> dict[str, Model]:
    return {
        model_id: StepFunAdapter.normalize_catalog_entry({"id": model_id}, {})
        for model_id in (
            "step-3.5-flash",
            "step-3.5-flash-2603",
            "step-3.7-flash",
            "step-router-v1",
        )
    }


def _adapter(connection_id: str) -> StepFunAdapter:
    config = _config()
    connection = config.get_connection(connection_id)
    models = _models()
    adapter = StepFunAdapter(
        config,
        "plan-secret" if connection_id == "step-plan" else "direct-secret",
        base_url=connection.base_url or config.base_url,
        auth_config=connection.auth,
        model_lookup=models.get,
        connection_mode=connection.mode,
    )
    return bind_connection(
        adapter, provider_id="stepfun", connection_id=connection_id, model_lookup=models.get
    )


def _chat_url(connection_id: str) -> str:
    return STEPFUN_PLAN_CHAT_URL if connection_id == "step-plan" else STEPFUN_DIRECT_CHAT_URL


@pytest.mark.parametrize(
    ("connection_id", "model_id", "request_kwargs", "expected"),
    [
        pytest.param(
            "direct-api",
            "step-3.5-flash-2603",
            {
                "max_output_tokens": 220_000,
                "max_completion_tokens": 200_000,
                "thinking_effort": "high",
                "temperature": 1.5,
                "top_p": 0.9,
                "frequency_penalty": -0.5,
            },
            {
                "max_tokens": 200_000,
                "max_output_tokens": ABSENT,
                "max_completion_tokens": ABSENT,
                "reasoning_effort": "high",
                "temperature": 1.5,
                "top_p": 0.9,
                "frequency_penalty": -0.5,
            },
            id="one-output-field-model-ladder-and-documented-sampling",
        ),
        pytest.param(
            "direct-api",
            "step-3.5-flash",
            {"thinking_effort": "high"},
            {"reasoning_effort": ABSENT},
            id="base-flash-has-no-effort-control",
        ),
        pytest.param(
            "step-plan",
            "step-router-v1",
            {"max_tokens": 999_999, "thinking_effort": "medium"},
            {"max_tokens": STEPFUN_ROUTER_MAX_OUTPUT_TOKENS, "reasoning_effort": "medium"},
            id="plan-router-caps-output",
        ),
    ],
)
@pytest.mark.asyncio
async def test_request_follows_the_model_policy(
    connection_id: str,
    model_id: str,
    request_kwargs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    adapter = _adapter(connection_id)
    with respx.mock:
        route = respx.post(_chat_url(connection_id)).mock(
            return_value=httpx.Response(200, json=CHAT_SUCCESS)
        )
        await adapter.send(HELLO, model_id=model_id, **request_kwargs)

    body = json.loads(route.calls.last.request.content)
    for key, value in expected.items():
        if value is ABSENT:
            assert key not in body, key
        else:
            assert body[key] == value, key
    # StepFun documents images; vBot has no video wire encoder.
    assert adapter.wire_media_support(model_id) == IMAGE_WIRE_MEDIA_TYPES


@pytest.mark.parametrize(
    ("connection_id", "model_id", "request_kwargs", "message"),
    [
        pytest.param(
            "direct-api", "step-router-v1", {}, "only through the Step Plan", id="router-direct"
        ),
        pytest.param("direct-api", "step-3.7-flash", {"temperature": 2.1}, "temperature"),
        pytest.param("direct-api", "step-3.7-flash", {"top_p": 0}, "top_p"),
        pytest.param(
            "direct-api", "step-3.7-flash", {"frequency_penalty": -2.1}, "frequency_penalty"
        ),
        pytest.param("direct-api", "step-3.7-flash", {"n": 2}, "exactly 1"),
        pytest.param("direct-api", "step-3.7-flash", {"seed": 7}, "does not accept"),
        pytest.param(
            "direct-api", "step-3.7-flash", {"reasoning_format": "future"}, "reasoning_format"
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_undocumented_requests_fail_before_network(
    connection_id: str, model_id: str, request_kwargs: dict[str, Any], message: str
) -> None:
    adapter = _adapter(connection_id)
    with respx.mock:
        route = respx.post(_chat_url(connection_id))
        with pytest.raises(ProviderError, match=message) as exc_info:
            await adapter.send(HELLO, model_id=model_id, **request_kwargs)

    assert exc_info.value.retryable is False
    assert route.call_count == 0


def test_catalog_is_exact_and_carries_current_capabilities() -> None:
    multimodal = StepFunAdapter.normalize_catalog_entry(
        {"id": "step-3.7-flash", "name": "Current 3.7"},
        {},
    )
    optimized = StepFunAdapter.normalize_catalog_entry(
        {"id": "step-3.5-flash-2603"},
        {},
    )

    assert multimodal.name == "Current 3.7"
    assert multimodal.context_window == STEPFUN_CONTEXT_WINDOW
    assert multimodal.capabilities.input_modalities == ("text", "image", "video")
    assert multimodal.capabilities.reasoning.levels == ("low", "medium", "high")
    assert multimodal.capabilities.tools is True
    assert multimodal.capabilities.json_mode is True
    assert multimodal.metadata == {}
    assert optimized.capabilities.reasoning.levels == ("low", "high")

    with pytest.raises(CatalogEntrySkipped):
        StepFunAdapter.normalize_catalog_entry({"id": "stepaudio-2.5-chat"}, {})


def test_response_normalizes_reasoning_tools_cache_and_terminal_outcome() -> None:
    normalized = _adapter("direct-api").normalize_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "Need a Tool",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Berlin"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 256},
                "completion_tokens_details": {"reasoning_tokens": 12},
            },
        },
        model_id="step-3.7-flash",
    )

    assert normalized["reasoning"] == "Need a Tool"
    assert normalized["tool_calls"] == [
        {"id": "call_1", "name": "weather", "arguments": {"city": "Berlin"}}
    ]
    assert normalized["terminal_outcome"] == "tool_calls"
    assert normalized["usage"] == {
        "input_tokens": 500,
        "output_tokens": 20,
        "cache_read_tokens": 256,
        "reasoning_tokens": 12,
    }


@respx.mock
@pytest.mark.asyncio
async def test_plan_stream_uses_plan_endpoint_without_undocumented_stream_options() -> None:
    body = (
        'data: {"choices":[{"delta":{"reasoning":"Check"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Done"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx.post(STEPFUN_PLAN_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )

    deltas = [
        delta async for delta in _adapter("step-plan").stream(HELLO, model_id="step-3.7-flash")
    ]

    assert deltas == [
        {"type": "reasoning_delta", "text": "Check"},
        {"type": "content_delta", "text": "Done"},
        {"type": "finish", "reason": "stop"},
    ]
    request = json.loads(route.calls.last.request.content)
    assert request["stream"] is True
    assert "stream_options" not in request
    assert route.calls.last.request.headers["authorization"] == "Bearer plan-secret"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "detail"),
    [(402, "entitlement"), (451, "content safety")],
)
async def test_stepfun_fatal_account_and_safety_errors_are_not_retried(
    status_code: int, detail: str
) -> None:
    route = respx.post(STEPFUN_DIRECT_CHAT_URL).mock(
        return_value=httpx.Response(status_code, json={"error": "rejected"})
    )

    with pytest.raises(ProviderError, match=detail) as exc_info:
        await _adapter("direct-api").send(HELLO, model_id="step-3.7-flash")

    assert route.call_count == 1
    assert exc_info.value.retryable is False
