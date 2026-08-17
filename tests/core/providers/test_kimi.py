"""Tests for Kimi Coding Plan and Platform Chat Completions policy."""

from __future__ import annotations

import base64

import pytest

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import ProviderError
from core.providers.kimi import (
    KIMI_CODING_MODE,
    KIMI_IMAGE_VIDEO_MEDIA_TYPES,
    KimiAdapter,
    _validate_kimi_request_size,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


def _model(model_id: str, *, reasoning: bool = True) -> Model:
    return KimiAdapter.normalize_catalog_entry(
        {"id": model_id, "supports_reasoning": reasoning},
        {"max_tokens": 32768},
    )


@pytest.fixture()
def kimi_config() -> ProviderConfig:
    return ProviderConfig(
        id="kimi",
        name="Kimi",
        adapter="kimi",
        base_url="https://api.moonshot.ai/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="Global Platform API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="KIMI_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 32768},
    )


@pytest.fixture()
def direct_adapter(kimi_config: ProviderConfig) -> KimiAdapter:
    models = {model_id: _model(model_id) for model_id in ("kimi-k3", "kimi-k2.6", "kimi-k2.7-code")}
    models["plain-model"] = Model(
        model_id="plain-model",
        name="Plain",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=128000,
        max_output_tokens=8192,
    )
    return KimiAdapter(kimi_config, "kimi-secret", model_lookup=models.get)


@pytest.fixture()
def coding_adapter(kimi_config: ProviderConfig) -> KimiAdapter:
    models = {model_id: _model(model_id) for model_id in ("k3", "k3-256k", "kimi-for-coding")}
    return KimiAdapter(
        kimi_config,
        "kimi-coding-secret",
        base_url="https://api.kimi.com/coding/v1",
        model_lookup=models.get,
        connection_mode=KIMI_CODING_MODE,
    )


@pytest.mark.parametrize(
    ("effort", "wire_effort"),
    [
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "high"),
        ("high", "high"),
        ("xhigh", "max"),
        ("max", "max"),
    ],
)
def test_k3_maps_vbot_effort_ladder(
    direct_adapter: KimiAdapter,
    effort: str,
    wire_effort: str,
) -> None:
    payload = direct_adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "kimi-k3",
        thinking_effort=effort,
    )

    assert payload["reasoning_effort"] == wire_effort


def test_direct_k3_none_degrades_to_low_and_consumes_both_effort_aliases(
    direct_adapter: KimiAdapter,
) -> None:
    payload = direct_adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "kimi-k3",
        thinking_effort="none",
        reasoning_effort="max",
    )

    assert payload["reasoning_effort"] == "low"
    assert "thinking_effort" not in payload


def test_coding_k3_none_disables_thinking_and_strips_replayed_reasoning(
    coding_adapter: KimiAdapter,
) -> None:
    payload = coding_adapter._build_payload(
        [
            {"role": "assistant", "content": "Prior", "reasoning": "Old trace"},
            {"role": "user", "content": "Continue"},
        ],
        "k3",
        thinking_effort="none",
    )

    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert "reasoning_content" not in payload["messages"][0]


def test_k2_6_switches_thinking_and_reasoning_replay(
    direct_adapter: KimiAdapter,
) -> None:
    messages = [
        {"role": "assistant", "content": "Prior", "reasoning": "Old trace"},
        {"role": "user", "content": "Continue"},
    ]

    enabled = direct_adapter._build_payload(messages, "kimi-k2.6", thinking_effort="high")
    disabled = direct_adapter._build_payload(messages, "kimi-k2.6", thinking_effort="none")

    assert enabled["thinking"] == {"type": "enabled", "keep": "all"}
    assert enabled["messages"][0]["reasoning_content"] == "Old trace"
    assert disabled["thinking"] == {"type": "disabled"}
    assert "reasoning_content" not in disabled["messages"][0]


def test_k2_7_is_fixed_on_for_platform_but_coding_none_can_route_without_thinking(
    direct_adapter: KimiAdapter,
    coding_adapter: KimiAdapter,
) -> None:
    messages = [{"role": "user", "content": "Hello"}]

    direct = direct_adapter._build_payload(
        messages,
        "kimi-k2.7-code",
        thinking_effort="none",
    )
    coding = coding_adapter._build_payload(
        messages,
        "kimi-for-coding",
        thinking_effort="none",
    )

    assert direct["thinking"] == {"type": "enabled", "keep": "all"}
    assert coding["thinking"] == {"type": "disabled"}


def test_payload_strips_unsupported_sampling_and_uses_max_completion_tokens(
    direct_adapter: KimiAdapter,
) -> None:
    payload = direct_adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "kimi-k2.6",
        temperature=0.2,
        top_p=0.8,
        n=2,
        max_tokens=40000,
        max_output_tokens=30000,
        max_completion_tokens=20000,
    )

    assert payload["max_completion_tokens"] == 20000
    assert "max_tokens" not in payload
    assert "max_output_tokens" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "n" not in payload


def test_default_output_limit_uses_model_fact(direct_adapter: KimiAdapter) -> None:
    k3 = direct_adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "kimi-k3",
    )
    k2 = direct_adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "kimi-k2.6",
    )

    assert k3["max_completion_tokens"] == 131072
    assert k2["max_completion_tokens"] == 32768


def test_image_and_video_content_use_kimi_data_url_parts(
    direct_adapter: KimiAdapter,
) -> None:
    payload = direct_adapter._build_payload(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect both"},
                    {"type": "media", "media_type": "image/webp", "base64": "aW1n"},
                    {"type": "media", "media_type": "video/mp4", "base64": "dmlk"},
                ],
            }
        ],
        "kimi-k3",
    )

    assert payload["messages"][0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/webp;base64,aW1n"},
    }
    assert payload["messages"][0]["content"][2] == {
        "type": "video_url",
        "video_url": {"url": "data:video/mp4;base64,dmlk"},
    }


def test_multimodal_size_guard_is_non_retryable() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(b"large-enough").decode("ascii")
                        },
                    }
                ],
            }
        ]
    }

    with pytest.raises(ProviderError, match="multimodal size limit") as exc_info:
        _validate_kimi_request_size(payload, max_bytes=10)

    assert exc_info.value.retryable is False


def test_catalog_normalization_applies_current_kimi_facts() -> None:
    k3 = KimiAdapter.normalize_catalog_entry({"id": "k3"})
    k3_256k = KimiAdapter.normalize_catalog_entry({"id": "k3-256k"})
    k2 = KimiAdapter.normalize_catalog_entry({"id": "kimi-k2.6"})

    assert k3.context_window == 1048576
    assert k3.max_output_tokens == 131072
    assert k3.capabilities.input_modalities == ("text", "image", "video")
    assert k3.capabilities.reasoning.levels == ("low", "high", "max")
    assert k3_256k.context_window == 262144
    assert k3_256k.capabilities.input_modalities == ("text", "image")
    assert k2.capabilities.reasoning.control == "on_off"
    assert k2.max_output_tokens == 32768


def test_unknown_catalog_entry_preserves_discovered_media_and_reasoning_flags() -> None:
    model = KimiAdapter.normalize_catalog_entry(
        {
            "id": "future-kimi",
            "supports_image_in": True,
            "supports_video_in": True,
            "supports_reasoning": True,
        }
    )

    assert model.capabilities.input_modalities == ("text", "image", "video")
    assert model.capabilities.reasoning.supported is True


def test_media_is_model_scoped_and_reasoning_replay_defaults_to_full_history(
    direct_adapter: KimiAdapter,
) -> None:
    assert direct_adapter.wire_media_support("kimi-k3") == KIMI_IMAGE_VIDEO_MEDIA_TYPES
    assert direct_adapter.reasoning_replay_policy("kimi-k3") == "full_history"
    assert direct_adapter.reasoning_replay_policy("plain-model") == "full_history"
    assert direct_adapter.reasoning_replay_policy("future-model") == "full_history"


def test_request_context_uses_stable_prompt_cache_affinity(
    coding_adapter: KimiAdapter,
) -> None:
    assert coding_adapter.request_context_kwargs(
        agent_id="agent",
        session_id="session",
        prompt_cache_affinity_id="shared-prefix",
    ) == {"prompt_cache_key": "shared-prefix"}
    assert coding_adapter.request_context_kwargs(
        agent_id="agent",
        session_id="session",
    ) == {"prompt_cache_key": "agent:session"}
