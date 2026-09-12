"""Openai: configuration behavior."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import ProviderAuthError
from core.providers.openai import (
    CODEX_EXTRA_HEADERS,
    CODEX_RESPONSES_MODE,
    OpenAIAdapter,
)
from core.providers.openai_compatible import CHAT_COMPLETIONS_ENDPOINT
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from tests.core.providers.openai_helpers import (
    _CODEX_TOOLS,
    OPENAI_SUBSCRIPTION_URL,
    SAMPLE_MESSAGES,
    _codex_sse_response,
    _jwt_with_account,
    _subscription_config,
)

OPENAI_API_KEY_URL = f"https://api.openai.com/v1{CHAT_COMPLETIONS_ENDPOINT}"

OPENAI_PLATFORM_RESPONSES_URL = "https://api.openai.com/v1/responses"


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
        connection_context_windows={"api-key": 1_050_000, "subscription": 272_000},
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


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        (None, None),
        ("none", "low"),
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("max", "max"),
    ],
)
@pytest.mark.asyncio
async def test_gpt6_subscription_uses_catalog_efforts_and_verified_minimum(effort, expected):
    registry = ModelRegistry.load(Path(__file__).resolve().parents[3] / "resources")

    def lookup(model_id):
        return registry.get("openai", model_id)

    adapter = OpenAIAdapter(
        _subscription_config(),
        "test-token",
        model_lookup=lookup,
        connection_mode=CODEX_RESPONSES_MODE,
    )
    try:
        payload = adapter._build_responses_payload(
            SAMPLE_MESSAGES,
            model_id="gpt-6-astra",
            thinking_effort=effort,
            max_output_tokens=1234,
            top_p=0.9,
            tools=_CODEX_TOOLS,
        )
        assert payload.get("reasoning", {}).get("effort") == expected
        assert "context" not in payload.get("reasoning", {})
        assert payload["include"] == ["reasoning.encrypted_content"]
        assert payload["store"] is False
        assert "max_output_tokens" not in payload
        assert "top_p" not in payload
        assert all(tool["strict"] is False for tool in payload["tools"])
        assert adapter.reasoning_replay_policy("gpt-6-astra") == "full_history"
        intent = adapter.describe_reasoning_render(
            model_lookup=lookup,
            model_id="gpt-6-astra",
            effort=effort,
        )
        assert intent.effort_level == expected
    finally:
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
        ("gpt-5.2", "full_history"),
        ("gpt-5.4", "full_history"),
        ("gpt-5.4-mini", "full_history"),
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


def test_openai_adapter_uses_connection_specific_context_window() -> None:
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

    assert platform._model_context_window("gpt-5.6-sol") == 1_050_000
    assert subscription._model_context_window("gpt-5.6-sol") == 272_000


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
@pytest.mark.parametrize("model_id", ["gpt-5.2", "gpt-5.4", "gpt-5.4-mini", "gpt-5.5"])
async def test_platform_pre_5_6_models_use_responses_without_all_turns(model_id: str) -> None:
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

    response = await adapter.send(SAMPLE_MESSAGES, model_id=model_id)
    payload = json.loads(route.calls.last.request.content)
    normalized = adapter.normalize_response(response, model_id=model_id)

    assert "reasoning" not in payload
    assert normalized["phase"] == "commentary"


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


# Default mode (api-key connection → /chat/completions)
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
async def test_platform_401_does_not_refresh_static_api_key() -> None:
    """Static API keys never acquire an OAuth recovery capability."""

    adapter = OpenAIAdapter(
        _platform_config(),
        "sk-invalid",
        model_lookup=_model_lookup_with_openai_wire_policies,
    )
    route = respx.post(OPENAI_PLATFORM_RESPONSES_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid API key"}})
    )

    with pytest.raises(ProviderAuthError):
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.6-terra")

    assert route.call_count == 1


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
