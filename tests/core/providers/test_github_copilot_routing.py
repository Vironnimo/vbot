"""Tests for GitHubCopilotAdapter behavior."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import ProviderError
from core.providers.github_copilot import (
    GitHubCopilotAdapter,
)
from core.providers.github_copilot_policy import RESPONSES_ENDPOINT
from core.providers.providers import ProviderConfig
from tests.core.providers.github_copilot_test_support import (
    API_KEY,
    COPILOT_CONFIG,
    COPILOT_URL,
    MESSAGES_URL,
    RESPONSES_URL,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    _copilot_metadata_lookup,
    _copilot_model,
    _copilot_model_with_metadata,
)
from tests.core.providers.github_copilot_test_support import (
    copilot_adapter as _copilot_adapter_fixture,
)
from tests.core.providers.github_copilot_test_support import (
    metadata_copilot_adapter as _metadata_copilot_adapter_fixture,
)

copilot_adapter = _copilot_adapter_fixture
metadata_copilot_adapter = _metadata_copilot_adapter_fixture


@respx.mock
@pytest.mark.asyncio
async def test_send_omits_reasoning_effort_for_safe_default_copilot_model(
    copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "claude-haiku-4.5"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_send_preserves_reasoning_effort_for_allowed_copilot_model(
    copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-mini",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_mini_to_responses_from_metadata(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-1",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-mini",
        thinking_effort="high",
        response_format={"type": "json_object"},
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "gpt-5-mini"
    assert request_body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert request_body["max_output_tokens"] == 64000
    assert request_body["text"] == {"format": {"type": "json_object"}}
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Hi",
        "reasoning": None,
        "reasoning_meta": {
            "response_id": "resp-1",
            "response_output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}
            ],
        },
        "tool_calls": None,
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_claude_to_messages_from_metadata(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Claude reply"}],
                "usage": {"input_tokens": 5, "output_tokens": 6},
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-sonnet-4.6",
        thinking_effort="high",
        response_format={"type": "json_object"},
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "claude-sonnet-4.6"
    assert request_body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request_body["output_config"] == {"effort": "high"}
    assert "response_format" not in request_body
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Claude reply",
        "reasoning": None,
        "reasoning_meta": None,
        "tool_calls": None,
        "usage": {"input_tokens": 5, "output_tokens": 6},
    }


@pytest.mark.parametrize(
    ("model_id", "expected_policy"),
    [
        ("gpt-5-mini", "full_history"),
        ("claude-sonnet-4.6", "full_history"),
        ("gpt-5.4", "current_run"),
        ("claude-haiku-4.5", "current_run"),
        ("gemini-3.1-pro-preview", "current_run"),
    ],
)
def test_reasoning_replay_policy_is_scoped_to_verified_model(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
    expected_policy: str,
) -> None:
    assert metadata_copilot_adapter.reasoning_replay_policy(model_id) == expected_policy


def test_reasoning_replay_policy_defaults_to_current_run_without_metadata(
    copilot_adapter: GitHubCopilotAdapter,
) -> None:
    assert copilot_adapter.reasoning_replay_policy("unknown-model") == "current_run"


@pytest.mark.parametrize("model_id", ["gpt-5.4", "claude-haiku-4.5", "gemini-3.1-pro-preview"])
def test_wire_media_support_is_image_only_across_endpoint_families(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    """Every Copilot endpoint family carries images only — no native audio."""
    assert metadata_copilot_adapter.wire_media_support(model_id) == IMAGE_WIRE_MEDIA_TYPES


@pytest.mark.parametrize("model_id", ["claude-sonnet-4.6", "claude-haiku-4.5"])
@respx.mock
@pytest.mark.asyncio
async def test_messages_models_send_exact_on_wire_payload(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id=model_id,
        thinking_effort="high",
        response_format={"type": "json_object"},
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": model_id,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        **({"output_config": {"effort": "high"}} if model_id == "claude-sonnet-4.6" else {}),
        "max_tokens": 32000 if model_id == "claude-sonnet-4.6" else 4096,
        **({} if model_id == "claude-sonnet-4.6" else {"temperature": 0.25}),
    }


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_responses_models_send_exact_on_wire_payload_without_temperature(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id=model_id,
        thinking_effort="high",
        response_format={"type": "json_object"},
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 64000 if model_id == "gpt-5-mini" else 4096,
    }


@respx.mock
@pytest.mark.asyncio
async def test_partial_openai_like_metadata_still_omits_temperature_on_responses(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.4-partial",
        temperature=0.25,
        top_p=0.9,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "gpt-5.4-partial",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "max_output_tokens": 4096,
        "top_p": 0.9,
    }


@respx.mock
@pytest.mark.asyncio
async def test_messages_alias_override_wins_over_provider_default_on_wire(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        max_output_tokens=2048,
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 2048,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_messages_max_completion_tokens_alias_maps_to_max_tokens_on_wire(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        max_completion_tokens=1024,
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 1024,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_gemini_3_1_preview_stays_chat_when_metadata_advertises_only_chat(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gemini-3.1-pro-preview",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "gemini-3.1-pro-preview"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_gemini_2_5_pro_without_endpoint_metadata_stays_conservative_chat(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gemini-2.5-pro",
        thinking_budget=4096,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "gemini-2.5-pro"
    assert "thinking_budget" not in request_body
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_unknown_model_uses_chat_fallback_and_omits_optional_controls(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="unknown-copilot-model",
        thinking_effort="high",
        tools=[{"name": "search", "description": "Search", "parameters": {"type": "object"}}],
        response_format={"type": "json_object"},
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "unknown-copilot-model"
    assert "reasoning_effort" not in request_body
    assert "tools" not in request_body
    assert "response_format" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_static_fallback_applies_only_when_metadata_missing() -> None:
    fallback_adapter = GitHubCopilotAdapter(COPILOT_CONFIG, API_KEY)
    metadata_adapter = GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=lambda model_id: _copilot_model(model_id),
    )
    chat_route = respx.post(COPILOT_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )
    responses_route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(200, json={"output": []})
    )

    await fallback_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-mini", thinking_effort="high")
    await metadata_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-mini", thinking_effort="high")

    chat_body = json.loads(chat_route.calls.last.request.content)
    responses_body = json.loads(responses_route.calls.last.request.content)
    assert chat_body["reasoning_effort"] == "high"
    assert responses_body["reasoning"] == {"effort": "high", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_headers_include_auth_and_extra_headers_for_all_endpoint_families() -> None:
    custom_config = ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
        base_url="https://api.githubcopilot.com",
        connections=COPILOT_CONFIG.connections,
        defaults={"max_tokens": 4096},
        extra_headers={"Editor-Version": "vBot/test"},
    )
    adapter = GitHubCopilotAdapter(
        custom_config,
        API_KEY,
        model_lookup=_copilot_metadata_lookup,
    )
    chat_route = respx.post(COPILOT_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )
    responses_route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(200, json={"output": []})
    )
    messages_route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"content": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="unknown-copilot-model")
    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-mini")
    await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4.6")

    for route in (chat_route, responses_route, messages_route):
        headers = route.calls.last.request.headers
        assert headers["Authorization"] == f"Bearer {API_KEY}"
        assert headers["Editor-Version"] == "vBot/test"
        assert headers["x-initiator"] == "user"


@respx.mock
@pytest.mark.asyncio
async def test_copilot_vision_headers_and_media_limits_follow_catalog_metadata() -> None:
    metadata = {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "supported_endpoints": [RESPONSES_ENDPOINT],
            "tool_calls": True,
            "vision": {
                "max_prompt_image_size": 4,
                "max_prompt_images": 1,
                "supported_media_types": ["image/png"],
            },
        }
    }
    adapter = GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=lambda model_id: _copilot_model_with_metadata(model_id, metadata),
    )
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))
    image = {"type": "media", "media_type": "image/png", "base64": "aW1n"}

    await adapter.send(
        [{"role": "user", "content": [{"type": "text", "text": "Look"}, image]}],
        model_id="gpt-5.4",
    )

    assert route.calls.last.request.headers["Copilot-Vision-Request"] == "true"
    assert adapter.wire_media_support("gpt-5.4") == frozenset({"image/png"})

    with pytest.raises(ProviderError, match="image-count limit exceeded"):
        await adapter.send(
            [{"role": "user", "content": [image, image]}],
            model_id="gpt-5.4",
        )


@respx.mock
@pytest.mark.asyncio
async def test_copilot_tool_followup_is_marked_as_agent_initiated() -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = GitHubCopilotAdapter(COPILOT_CONFIG, API_KEY)

    await adapter.send(
        [
            {"role": "user", "content": "Run it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "name": "run",
                        "arguments": "{}",
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
        model_id="unknown-copilot-model",
    )

    assert route.calls.last.request.headers["x-initiator"] == "agent"


@pytest.mark.asyncio
async def test_copilot_prompt_limit_fails_locally_before_request() -> None:
    metadata = {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "supported_endpoints": [RESPONSES_ENDPOINT],
            "max_prompt_tokens": 1,
        }
    }
    adapter = GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=lambda model_id: _copilot_model_with_metadata(model_id, metadata),
    )

    with pytest.raises(ProviderError, match="prompt limit"):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.4")
