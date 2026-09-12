"""Ollama: catalog behavior."""

from __future__ import annotations

from typing import Any

import pytest

from core.models.models import (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
)
from core.providers.errors import ProviderError
from core.providers.ollama import (
    OllamaAdapter,
)
from tests.core.providers.ollama_helpers import (
    OLLAMA_CLOUD_CONFIG,
    OLLAMA_CONFIG,
)
from tests.core.providers.ollama_helpers import (
    adapter as adapter,
)

# Real /api/show shape (trimmed to the consumed fields).
SHOW_RESPONSE: dict[str, Any] = {
    "capabilities": ["completion", "vision", "tools"],
    "details": {
        "format": "gguf",
        "family": "mistral3",
        "parameter_size": "8.9B",
        "quantization_level": "Q4_K_M",
    },
    "model_info": {
        "general.architecture": "mistral3",
        "mistral3.context_length": 262144,
        "mistral3.rope.scaling.original_context_length": 16384,
    },
}


# Catalog normalization and enrichment
class TestCatalogNormalization:
    def test_local_entry_is_stamped_local(self) -> None:
        # Arrange — real /api/tags local entry (trimmed).
        raw = {
            "name": "ministral-3:8b",
            "model": "ministral-3:8b",
            "size": 6022236616,
            "details": {
                "format": "gguf",
                "family": "mistral3",
                "parameter_size": "8.9B",
                "quantization_level": "Q4_K_M",
            },
        }

        # Act
        model = OllamaAdapter.normalize_catalog_entry(raw)

        # Assert
        assert model.model_id == "ministral-3:8b"
        assert model.family == "mistral3"
        assert model.metadata["ollama"] == {"local": True}
        assert model.capabilities.tools is False
        assert model.context_window is None

    def test_proxied_cloud_entry_is_stamped_remote(self) -> None:
        # Arrange — real /api/tags proxied cloud entry (trimmed). ``remote_host``
        # is the fact; the ``:cloud`` name suffix is convention.
        raw = {
            "name": "kimi-k2.6:cloud",
            "model": "kimi-k2.6:cloud",
            "remote_model": "kimi-k2.6",
            "remote_host": "https://ollama.com:443",
            "details": {"family": "kimi"},
        }

        # Act
        model = OllamaAdapter.normalize_catalog_entry(raw)

        # Assert
        assert model.metadata["ollama"] == {"remote": True}
        assert model.family == "kimi"

    def test_direct_cloud_connection_overrides_missing_remote_host_marker(self) -> None:
        raw = {
            "name": "glm-5.1",
            "model": "glm-5.1",
            "details": {"family": "glm5.1"},
        }
        baseline = OllamaAdapter.normalize_catalog_entry(raw)

        model = OllamaAdapter.finalize_discovered_model(
            baseline,
            OLLAMA_CLOUD_CONFIG.get_connection("api-key"),
        )

        assert baseline.metadata["ollama"] == {"local": True}
        assert model.metadata["ollama"] == {"remote": True}

    def test_current_tags_facts_are_used_before_show_enrichment(self) -> None:
        raw = {
            "model": "ministral-3:8b",
            "details": {"family": "mistral3", "context_length": 262144},
            "capabilities": ["vision", "completion", "tools"],
        }

        model = OllamaAdapter.normalize_catalog_entry(raw)

        assert model.context_window == 262144
        assert model.capabilities.vision is True
        assert model.capabilities.tools is True
        assert model.capabilities.input_modalities == ("text", "image")

    def test_entry_without_model_id_raises(self) -> None:
        with pytest.raises(ProviderError):
            OllamaAdapter.normalize_catalog_entry({"details": {}})


class TestEnrichment:
    @pytest.mark.asyncio
    async def test_show_response_fills_capabilities_and_window(self) -> None:
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry(
            {"model": "ministral-3:8b", "details": {"family": "mistral3"}}
        )
        posted: list[tuple[str, dict[str, Any]]] = []

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            posted.append((endpoint, payload))
            return SHOW_RESPONSE

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models({"ministral-3:8b": base}, post_json)

        # Assert
        model = enriched["ministral-3:8b"]
        assert posted == [("/api/show", {"model": "ministral-3:8b"})]
        assert model.capabilities.tools is True
        assert model.capabilities.vision is True
        assert model.capabilities.reasoning.supported is False
        # The exact "<arch>.context_length" key is read — never the rope
        # scaling original_context_length (16384 in the fixture).
        assert model.context_window == 262144
        assert model.metadata["ollama"] == {"local": True}
        assert model.family == "mistral3"

    @pytest.mark.asyncio
    async def test_thinking_capability_maps_to_on_off_control(self) -> None:
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry({"model": "kimi-k2.6:cloud"})
        show = {"capabilities": ["completion", "tools", "thinking"], "model_info": {}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models(
            {"kimi-k2.6:cloud": base}, post_json
        )

        # Assert
        reasoning = enriched["kimi-k2.6:cloud"].capabilities.reasoning
        assert reasoning.supported is True
        assert reasoning.control == REASONING_CONTROL_ON_OFF

    @pytest.mark.asyncio
    async def test_gpt_oss_thinking_capability_maps_to_level_control(self) -> None:
        base = OllamaAdapter.normalize_catalog_entry({"model": "gpt-oss:20b"})
        show = {"capabilities": ["completion", "tools", "thinking"], "model_info": {}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        enriched = await OllamaAdapter.enrich_discovered_models(
            {"gpt-oss:20b": base},
            post_json,
        )

        reasoning = enriched["gpt-oss:20b"].capabilities.reasoning
        assert reasoning.control == REASONING_CONTROL_LEVELS
        assert reasoning.levels == ("low", "medium", "high")

    @pytest.mark.asyncio
    async def test_glm_4_7_discovery_inherits_full_history_thinking_replay(self) -> None:
        base = OllamaAdapter.normalize_catalog_entry({"model": "glm-4.7:latest"})
        show = {"capabilities": ["completion", "thinking"], "model_info": {}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        enriched = await OllamaAdapter.enrich_discovered_models(
            {"glm-4.7:latest": base},
            post_json,
        )
        model = enriched["glm-4.7:latest"]
        lookup = {"glm-4.7:latest": model}.get
        adapter = OllamaAdapter(OLLAMA_CONFIG, "", model_lookup=lookup)

        assert model.metadata["ollama"] == {"local": True}
        assert adapter.reasoning_replay_policy("glm-4.7:latest") == "full_history"
        await adapter.aclose()

    @pytest.mark.asyncio
    async def test_glm_5_2_discovery_inherits_full_history_thinking_replay(self) -> None:
        base = OllamaAdapter.normalize_catalog_entry({"model": "glm-5.2"})
        show = {"capabilities": ["completion", "thinking"], "model_info": {}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        enriched = await OllamaAdapter.enrich_discovered_models(
            {"glm-5.2": base},
            post_json,
        )
        model = enriched["glm-5.2"]
        lookup = {"glm-5.2": model}.get
        adapter = OllamaAdapter(OLLAMA_CONFIG, "", model_lookup=lookup)

        assert model.metadata["ollama"] == {"local": True}
        assert adapter.reasoning_replay_policy("glm-5.2") == "full_history"
        await adapter.aclose()

    @pytest.mark.asyncio
    async def test_failed_show_keeps_conservative_baseline(self) -> None:
        """A failed per-model /api/show leaves that model at its baseline."""
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry({"model": "broken-model"})

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            raise ProviderError("boom", retryable=False)

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models({"broken-model": base}, post_json)

        # Assert — no enriched entry; discovery keeps the baseline model.
        assert enriched == {}

    @pytest.mark.asyncio
    async def test_missing_architecture_leaves_window_unknown(self) -> None:
        # Arrange
        base = OllamaAdapter.normalize_catalog_entry({"model": "odd-model"})
        show = {"capabilities": ["completion"], "model_info": {"other.context_length": 4096}}

        async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
            return show

        # Act
        enriched = await OllamaAdapter.enrich_discovered_models({"odd-model": base}, post_json)

        # Assert — honest unknown, never a guessed window.
        assert enriched["odd-model"].context_window is None
