"""Kimi: Coding Plan and Platform Chat Completions policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.kimi import KIMI_CODING_MODE, KimiAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

from .adapter_test_support import bind_connection

PLATFORM_URL = "https://api.moonshot.ai/v1/chat/completions"
CODING_URL = "https://api.kimi.com/coding/v1/chat/completions"
CHAT_SUCCESS = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
HISTORY = [
    {"role": "assistant", "content": "Prior", "reasoning": "Old trace"},
    {"role": "user", "content": "Continue"},
]
ABSENT = object()

CONFIG = ProviderConfig(
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
                header="Authorization", prefix="Bearer ", credential_key="KIMI_API_KEY"
            ),
        )
    ],
    defaults={"max_tokens": 32768},
)


# The bundled Model DB records: Kimi's per-Model facts live in its override file.
_REGISTRY = ModelRegistry.load(Path(__file__).resolve().parents[3] / "resources")
MODELS = {
    model_id: _REGISTRY.get("kimi", model_id)
    for model_id in ("kimi-k3", "kimi-k2.6", "kimi-k2.7-code", "k3", "kimi-for-coding")
}
MODELS["plain-model"] = Model(
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


def _adapter(connection: str) -> KimiAdapter:
    if connection == "coding":
        adapter = KimiAdapter(
            CONFIG,
            "kimi-coding-secret",
            base_url="https://api.kimi.com/coding/v1",
            model_lookup=MODELS.get,
            connection_mode=KIMI_CODING_MODE,
        )
        connection_id = "coding-plan"
    else:
        adapter = KimiAdapter(CONFIG, "kimi-secret", model_lookup=MODELS.get)
        connection_id = "api-key"
    return bind_connection(
        adapter, provider_id="kimi", connection_id=connection_id, model_lookup=MODELS.get
    )


async def _sent_body(
    connection: str, messages: list[dict[str, Any]], model_id: str, **kwargs: Any
) -> dict[str, Any]:
    adapter = _adapter(connection)
    with respx.mock:
        route = respx.post(CODING_URL if connection == "coding" else PLATFORM_URL).mock(
            return_value=httpx.Response(200, json=CHAT_SUCCESS)
        )
        try:
            await adapter.send(messages, model_id=model_id, **kwargs)
        finally:
            await adapter.aclose()
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


@pytest.mark.parametrize(
    ("connection", "model_id", "request_kwargs", "expected", "replays_reasoning"),
    [
        # K3 maps the vBot ladder onto low|high|max.
        *(
            pytest.param(
                "platform",
                "kimi-k3",
                {"thinking_effort": effort},
                {"reasoning_effort": wire_effort, "thinking": ABSENT},
                True,
                id=f"k3-{effort}-to-{wire_effort}",
            )
            for effort, wire_effort in (
                ("minimal", "low"),
                ("low", "low"),
                ("medium", "high"),
                ("high", "high"),
                ("xhigh", "max"),
                ("max", "max"),
            )
        ),
        pytest.param(
            "platform",
            "kimi-k3",
            {"thinking_effort": "none", "reasoning_effort": "max"},
            {"reasoning_effort": "low", "thinking_effort": ABSENT},
            True,
            id="platform-k3-none-degrades-to-low-and-consumes-both-aliases",
        ),
        pytest.param(
            "coding",
            "k3",
            {"thinking_effort": "none"},
            {"thinking": {"type": "disabled"}, "reasoning_effort": ABSENT},
            False,
            id="coding-k3-none-disables-thinking",
        ),
        pytest.param(
            "platform",
            "kimi-k2.6",
            {"thinking_effort": "high"},
            {"thinking": {"type": "enabled", "keep": "all"}},
            True,
            id="k2.6-enables-thinking",
        ),
        pytest.param(
            "platform",
            "kimi-k2.6",
            {"thinking_effort": "none"},
            {"thinking": {"type": "disabled"}},
            False,
            id="k2.6-disables-thinking",
        ),
        pytest.param(
            "platform",
            "kimi-k2.7-code",
            {"thinking_effort": "none"},
            {"thinking": {"type": "enabled", "keep": "all"}},
            True,
            id="platform-k2.7-is-fixed-on",
        ),
        pytest.param(
            "coding",
            "kimi-for-coding",
            {"thinking_effort": "none"},
            {"thinking": {"type": "disabled"}},
            False,
            id="coding-k2.7-none-routes-without-thinking",
        ),
        pytest.param(
            "platform",
            "plain-model",
            {"thinking_effort": "high"},
            {"thinking": ABSENT, "reasoning_effort": ABSENT},
            False,
            id="non-reasoning-model-strips-replay",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reasoning_controls_and_replay_follow_the_model_and_connection(
    connection: str,
    model_id: str,
    request_kwargs: dict[str, Any],
    expected: dict[str, Any],
    replays_reasoning: bool,
) -> None:
    body = await _sent_body(connection, HISTORY, model_id, **request_kwargs)

    for key, value in expected.items():
        if value is ABSENT:
            assert key not in body, key
        else:
            assert body[key] == value, key
    assert body["messages"][0].get("reasoning_content") == (
        "Old trace" if replays_reasoning else None
    )


@pytest.mark.parametrize(
    ("model_id", "request_kwargs", "output_limit"),
    [
        pytest.param("kimi-k3", {}, 131072, id="k3-model-fact"),
        pytest.param("kimi-k2.6", {}, 32768, id="k2-recommended-limit"),
        pytest.param(
            "kimi-k2.6",
            {
                "temperature": 0.2,
                "top_p": 0.8,
                "n": 2,
                "max_tokens": 40000,
                "max_output_tokens": 30000,
                "max_completion_tokens": 20000,
            },
            20000,
            id="smallest-alias-wins-and-sampling-is-removed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_output_limit_is_one_field_and_sampling_is_never_sent(
    model_id: str, request_kwargs: dict[str, Any], output_limit: int
) -> None:
    body = await _sent_body("platform", HISTORY, model_id, **request_kwargs)

    assert body["max_completion_tokens"] == output_limit
    for removed in ("max_tokens", "max_output_tokens", "temperature", "top_p", "n"):
        assert removed not in body, removed


@pytest.mark.asyncio
async def test_image_and_video_content_use_kimi_data_url_parts() -> None:
    body = await _sent_body(
        "platform",
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

    assert body["messages"][0]["content"][1:] == [
        {"type": "image_url", "image_url": {"url": "data:image/webp;base64,aW1n"}},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,dmlk"}},
    ]
    assert _adapter("platform").wire_media_support("kimi-k3") == frozenset(
        {
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
            "video/mp4",
            "video/quicktime",
            "video/webm",
        }
    )


@pytest.mark.parametrize(
    ("connection", "limit"),
    [
        pytest.param("coding", 80 * 1024 * 1024, id="coding-plan-80-mib"),
        pytest.param("platform", 100_000_000, id="platform-100-mb"),
    ],
)
def test_request_body_limit_follows_the_connection(connection: str, limit: int) -> None:
    assert _adapter(connection).request_body_limit("kimi-k3") == limit


def test_catalog_entry_preserves_discovered_media_and_reasoning_flags() -> None:
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


def test_request_context_uses_stable_prompt_cache_affinity() -> None:
    adapter = _adapter("coding")

    assert adapter.request_context_kwargs(
        agent_id="agent",
        session_id="session",
        prompt_cache_affinity_id="shared-prefix",
    ) == {"prompt_cache_key": "shared-prefix"}
    assert adapter.request_context_kwargs(agent_id="agent", session_id="session") == {
        "prompt_cache_key": "agent:session"
    }
