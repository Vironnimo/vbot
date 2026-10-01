"""Tests for LM Studio native discovery and lazy model loading."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.models.models import REASONING_CONTROL_ON_OFF
from core.providers.errors import CatalogEntrySkipped
from core.providers.lmstudio import LMStudioAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

LMSTUDIO_CONFIG = ProviderConfig(
    id="lmstudio",
    name="LM Studio",
    adapter="lmstudio",
    base_url="http://localhost:1234",
    models_endpoint="/api/v1/models",
    connections=[
        ConnectionConfig(
            id="local",
            type="none",
            label="Local",
            auth=AuthConfig(header="", prefix="", credential_key=""),
        )
    ],
)
MODEL_ID = "gemma-4-12b-heretic-abliterated"
NATIVE_MODEL = {
    "type": "llm",
    "key": MODEL_ID,
    "display_name": "Gemma 4 12B Heretic",
    "architecture": "gemma4",
    "loaded_instances": [],
    "max_context_length": 262144,
    "capabilities": {
        "vision": True,
        "trained_for_tool_use": True,
        "reasoning": {"allowed_options": ["off", "on"], "default": "on"},
    },
}
CHAT_RESPONSE = {
    "id": "chatcmpl-test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello."},
            "finish_reason": "stop",
        }
    ],
}


def test_native_llm_entry_preserves_local_capabilities() -> None:
    model = LMStudioAdapter.normalize_catalog_entry(NATIVE_MODEL)

    assert model.model_id == MODEL_ID
    assert model.name == "Gemma 4 12B Heretic"
    assert model.family == "gemma4"
    assert model.context_window == 262144
    assert model.metadata["lmstudio"] == {"local": True}
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.reasoning.supported is True
    assert model.capabilities.reasoning.control == REASONING_CONTROL_ON_OFF


def test_native_embedding_entry_becomes_a_local_embedding_model() -> None:
    # Native /api/v1/models embedding entry (trimmed); it carries no capabilities.
    model = LMStudioAdapter.normalize_catalog_entry(
        {
            "type": "embedding",
            "key": "text-embedding-nomic-embed-text-v1.5",
            "display_name": "Nomic Embed Text v1.5",
            "publisher": "nomic-ai",
            "architecture": "nomic-bert",
            "max_context_length": 2048,
        }
    )

    assert (model.model_id, model.name, model.context_window) == (
        "text-embedding-nomic-embed-text-v1.5",
        "Nomic Embed Text v1.5",
        2048,
    )
    assert model.capabilities.task_types == ("text_embedding",)
    assert model.metadata["lmstudio"] == {"local": True}


def test_entry_of_another_type_is_skipped() -> None:
    with pytest.raises(CatalogEntrySkipped):
        LMStudioAdapter.normalize_catalog_entry({"type": "tts", "key": "kokoro"})


@pytest.mark.parametrize(
    ("loaded_instances", "load_payload"),
    [
        pytest.param([], {"model": MODEL_ID, "context_length": 32768}, id="unloaded-is-loaded"),
        pytest.param([{"id": "existing-instance"}], None, id="loaded-is-reused"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_chat_loads_an_unloaded_model_with_the_resolved_context_first(
    loaded_instances: list[dict[str, str]], load_payload: dict[str, object] | None
) -> None:
    adapter = LMStudioAdapter(LMSTUDIO_CONFIG, "", local_context_resolver=lambda model_id: 32768)
    respx.get("http://localhost:1234/api/v1/models").mock(
        return_value=httpx.Response(
            200, json={"models": [{**NATIVE_MODEL, "loaded_instances": loaded_instances}]}
        )
    )
    load_route = respx.post("http://localhost:1234/api/v1/models/load").mock(
        return_value=httpx.Response(200, json={"instance_id": "loaded-instance"})
    )
    chat_route = respx.post("http://localhost:1234/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=CHAT_RESPONSE)
    )

    try:
        response = await adapter.send([{"role": "user", "content": "Hello"}], model_id=MODEL_ID)
    finally:
        await adapter.aclose()

    assert response == CHAT_RESPONSE
    requested = [call.request.url.path for call in respx.calls]
    if load_payload is None:
        assert requested == ["/api/v1/models", "/v1/chat/completions"]
    else:
        assert requested == ["/api/v1/models", "/api/v1/models/load", "/v1/chat/completions"]
        assert json.loads(load_route.calls.last.request.content) == load_payload
    assert "authorization" not in chat_route.calls.last.request.headers
