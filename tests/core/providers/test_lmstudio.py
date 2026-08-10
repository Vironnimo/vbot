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


class TestCatalogNormalization:
    def test_native_llm_entry_preserves_local_capabilities(self) -> None:
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

    def test_non_chat_entry_is_skipped(self) -> None:
        with pytest.raises(CatalogEntrySkipped):
            LMStudioAdapter.normalize_catalog_entry({"type": "embedding", "key": "nomic-embed"})


class TestLazyModelLoading:
    @respx.mock
    @pytest.mark.asyncio
    async def test_unloaded_model_is_loaded_with_resolved_context_before_chat(self) -> None:
        adapter = LMStudioAdapter(
            LMSTUDIO_CONFIG,
            "",
            local_context_resolver=lambda model_id: 32768,
        )
        models_route = respx.get("http://localhost:1234/api/v1/models").mock(
            return_value=httpx.Response(200, json={"models": [NATIVE_MODEL]})
        )
        load_route = respx.post("http://localhost:1234/api/v1/models/load").mock(
            return_value=httpx.Response(200, json={"instance_id": "loaded-instance"})
        )
        chat_route = respx.post("http://localhost:1234/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=CHAT_RESPONSE)
        )

        response = await adapter.send(
            [{"role": "user", "content": "Hello"}],
            model_id=MODEL_ID,
        )

        assert response == CHAT_RESPONSE
        assert models_route.called
        assert load_route.called
        assert chat_route.called
        load_payload = json.loads(load_route.calls.last.request.content)
        assert load_payload == {"model": MODEL_ID, "context_length": 32768}
        assert "authorization" not in chat_route.calls.last.request.headers
        await adapter.aclose()

    @respx.mock
    @pytest.mark.asyncio
    async def test_already_loaded_model_is_reused_without_load_request(self) -> None:
        adapter = LMStudioAdapter(
            LMSTUDIO_CONFIG,
            "",
            local_context_resolver=lambda model_id: 32768,
        )
        loaded_model = {**NATIVE_MODEL, "loaded_instances": [{"id": "existing-instance"}]}
        respx.get("http://localhost:1234/api/v1/models").mock(
            return_value=httpx.Response(200, json={"models": [loaded_model]})
        )
        load_route = respx.post("http://localhost:1234/api/v1/models/load").mock(
            return_value=httpx.Response(200, json={"instance_id": "unexpected"})
        )
        respx.post("http://localhost:1234/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=CHAT_RESPONSE)
        )

        await adapter.send([{"role": "user", "content": "Hello"}], model_id=MODEL_ID)

        assert load_route.call_count == 0
        await adapter.aclose()
