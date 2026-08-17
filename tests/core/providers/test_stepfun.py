"""StepFun request, response, catalog, and transport policy tests."""

from __future__ import annotations

import json

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

STEPFUN_DIRECT_CHAT_URL = "https://api.stepfun.com/v1/chat/completions"
STEPFUN_PLAN_CHAT_URL = "https://api.stepfun.com/step_plan/v1/chat/completions"


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


@pytest.fixture()
def direct_adapter() -> StepFunAdapter:
    config = _config()
    connection = config.get_connection("direct-api")
    return StepFunAdapter(
        config,
        "direct-secret",
        base_url=connection.base_url or config.base_url,
        auth_config=connection.auth,
        model_lookup=_models().get,
        connection_mode=connection.mode,
    )


@pytest.fixture()
def plan_adapter() -> StepFunAdapter:
    config = _config()
    connection = config.get_connection("step-plan")
    return StepFunAdapter(
        config,
        "plan-secret",
        base_url=connection.base_url or config.base_url,
        auth_config=connection.auth,
        model_lookup=_models().get,
        connection_mode=connection.mode,
    )


def test_payload_uses_one_output_field_and_model_effort_ladder(
    direct_adapter: StepFunAdapter,
) -> None:
    payload = direct_adapter._build_payload(
        [{"role": "user", "content": "Solve this"}],
        "step-3.5-flash-2603",
        max_output_tokens=220_000,
        max_completion_tokens=200_000,
        thinking_effort="high",
        temperature=1.5,
        top_p=0.9,
        frequency_penalty=-0.5,
    )

    assert payload["max_tokens"] == 200_000
    assert "max_output_tokens" not in payload
    assert "max_completion_tokens" not in payload
    assert payload["reasoning_effort"] == "high"
    assert payload["temperature"] == 1.5
    assert payload["top_p"] == 0.9
    assert payload["frequency_penalty"] == -0.5


def test_base_flash_omits_unavailable_effort_control(direct_adapter: StepFunAdapter) -> None:
    payload = direct_adapter._build_payload(
        [{"role": "user", "content": "Think"}],
        "step-3.5-flash",
        thinking_effort="high",
    )

    assert "reasoning_effort" not in payload


def test_router_is_plan_only_and_caps_output(
    direct_adapter: StepFunAdapter,
    plan_adapter: StepFunAdapter,
) -> None:
    with pytest.raises(ProviderError):
        direct_adapter._build_payload(
            [{"role": "user", "content": "Route this"}],
            "step-router-v1",
        )

    payload = plan_adapter._build_payload(
        [{"role": "user", "content": "Route this"}],
        "step-router-v1",
        max_tokens=999_999,
        thinking_effort="medium",
    )

    assert payload["max_tokens"] == STEPFUN_ROUTER_MAX_OUTPUT_TOKENS
    assert payload["reasoning_effort"] == "medium"


@pytest.mark.parametrize(
    ("kwargs", "_message"),
    [
        ({"temperature": 2.1}, "temperature"),
        ({"top_p": 0}, "top_p"),
        ({"frequency_penalty": -2.1}, "frequency_penalty"),
        ({"n": 2}, "exactly 1"),
        ({"seed": 7}, "does not document"),
        ({"stream_options": {"include_usage": True}}, "does not document"),
        ({"reasoning_format": "future"}, "reasoning_format"),
    ],
)
def test_invalid_or_undocumented_parameters_fail_before_network(
    direct_adapter: StepFunAdapter,
    kwargs: dict[str, object],
    _message: str,
) -> None:
    with pytest.raises(ProviderError) as exc_info:
        direct_adapter._build_payload(
            [{"role": "user", "content": "Hello"}],
            "step-3.7-flash",
            **kwargs,
        )

    assert exc_info.value.retryable is False


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
    assert multimodal.metadata["stepfun"]["prompt_cache"] == "automatic"
    assert optimized.capabilities.reasoning.levels == ("low", "high")

    with pytest.raises(CatalogEntrySkipped):
        StepFunAdapter.normalize_catalog_entry({"id": "stepaudio-2.5-chat"}, {})


def test_media_and_reasoning_replay_use_system_defaults(
    direct_adapter: StepFunAdapter,
) -> None:
    assert direct_adapter.wire_media_support("step-3.7-flash") == IMAGE_WIRE_MEDIA_TYPES
    assert direct_adapter.reasoning_replay_policy("step-3.7-flash") == "full_history"


def test_response_normalizes_reasoning_tools_cache_and_terminal_outcome(
    direct_adapter: StepFunAdapter,
) -> None:
    normalized = direct_adapter.normalize_response(
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
async def test_plan_stream_uses_plan_endpoint_without_undocumented_stream_options(
    plan_adapter: StepFunAdapter,
) -> None:
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
        delta
        async for delta in plan_adapter.stream(
            [{"role": "user", "content": "Hello"}],
            model_id="step-3.7-flash",
        )
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
    direct_adapter: StepFunAdapter,
    status_code: int,
    detail: str,
) -> None:
    route = respx.post(STEPFUN_DIRECT_CHAT_URL).mock(
        return_value=httpx.Response(status_code, json={"error": "rejected"})
    )

    with pytest.raises(ProviderError, match=detail) as exc_info:
        await direct_adapter.send(
            [{"role": "user", "content": "Hello"}],
            model_id="step-3.7-flash",
        )

    assert route.call_count == 1
    assert exc_info.value.retryable is False
