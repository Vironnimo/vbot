"""NVIDIA API Catalog request and catalog policy tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Model, ModelRegistry
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.nvidia import NvidiaAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

from .adapter_test_support import bind_connection

NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
HELLO = [{"role": "user", "content": "Hello"}]
CHAT_SUCCESS = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
ABSENT = object()

# The bundled Model DB records, refreshed from NVIDIA's listing and models.dev.
_REGISTRY = ModelRegistry.load(Path(__file__).resolve().parents[3] / "resources")


def _adapter() -> NvidiaAdapter:
    config = ProviderConfig(
        id="nvidia",
        name="NVIDIA",
        adapter="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization", prefix="Bearer ", credential_key="NVIDIA_API_KEY"
                ),
            )
        ],
        defaults={"max_tokens": 16384},
    )

    def lookup(model_id: str) -> Model | None:
        return _REGISTRY.get("nvidia", model_id)

    adapter = NvidiaAdapter(config, "nvapi-secret", model_lookup=lookup)
    return bind_connection(
        adapter, provider_id="nvidia", connection_id="api-key", model_lookup=lookup
    )


def test_catalog_keeps_chat_models_only() -> None:
    listed = {
        # Chat Models, including ids that merely contain a filtered word.
        "nvidia/nemotron-3.5-lightning-30b-a3b": True,
        "nvidia/nemotron-3.5-super-vl-120b-a12b": True,
        "nvidia/cosmos-reason2-8b": True,
        "nvidia/riva-translate-4b-instruct-v2": True,
        "google/gemma-4-31b-it": True,
        "openai/gpt-oss-20b": True,
        # Retrievers, classifiers and detectors.
        "nvidia/nemotron-3.5-embed-8b": False,
        "nvidia/llama-3.2-nv-embedqa-1b-v1": False,
        "nvidia/llama-3.2-nemoretriever-1b-vlm-embed-v1": False,
        "snowflake/arctic-embed-l": False,
        "nvidia/nvclip": False,
        "nvidia/llama-3.1-nemoguard-8b-topic-control": False,
        "nvidia/llama-3.1-nemotron-safety-guard-8b-v3": False,
        "nvidia/nemotron-3.5-content-safety": False,
        "nvidia/ai-synthetic-video-detector": False,
    }

    assert {
        model_id: NvidiaAdapter.accepts_discovered_model({"id": model_id}, None)
        for model_id in listed
    } == listed


@pytest.mark.parametrize(
    ("model_id", "effort", "expected"),
    [
        pytest.param(
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "high",
            {"chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": ABSENT},
            id="nemotron-switches-thinking-on",
        ),
        pytest.param(
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "none",
            {"chat_template_kwargs": {"enable_thinking": False}},
            id="nemotron-switches-thinking-off",
        ),
        pytest.param(
            "nvidia/nemotron-3-ultra-550b-a55b",
            "low",
            {"chat_template_kwargs": {"enable_thinking": True, "medium_effort": True}},
            id="ultra-snaps-to-its-medium-effort",
        ),
        pytest.param(
            "nvidia/nemotron-3-ultra-550b-a55b",
            None,
            {"chat_template_kwargs": ABSENT, "reasoning_effort": ABSENT},
            id="unset-effort-sends-nothing",
        ),
        pytest.param(
            "openai/gpt-oss-20b",
            "none",
            {"reasoning_effort": "low", "chat_template_kwargs": ABSENT},
            id="gpt-oss-cannot-switch-off",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reasoning_selection_reaches_the_models_own_control(
    model_id: str, effort: str | None, expected: dict[str, Any]
) -> None:
    adapter = _adapter()
    with respx.mock:
        route = respx.post(NVIDIA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=CHAT_SUCCESS)
        )
        await adapter.send(HELLO, model_id=model_id, thinking_effort=effort)

    request = route.calls.last.request
    body = json.loads(request.content)
    for key, value in expected.items():
        if value is ABSENT:
            assert key not in body, key
        else:
            assert body[key] == value, key
    assert request.headers["authorization"] == "Bearer nvapi-secret"
    assert adapter.wire_media_support(model_id) == IMAGE_WIRE_MEDIA_TYPES
