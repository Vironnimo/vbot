"""Ollama catalog discovery: ``/api/tags`` normalization, Connection scope, and
``/api/show`` enrichment."""

from __future__ import annotations

from typing import Any

import pytest

from core.models.models import REASONING_CONTROL_LEVELS, REASONING_CONTROL_ON_OFF
from core.providers.errors import ProviderError
from core.providers.ollama import OllamaAdapter
from tests.core.providers.ollama_test_support import CLOUD_CONFIG, LOCAL_CONFIG

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


def _show(response: dict[str, Any] | Exception, posted: list | None = None):
    """A /api/show POST double answering every Model with ``response``."""

    async def post_json(endpoint: str, payload: dict[str, Any]) -> Any:
        if posted is not None:
            posted.append((endpoint, payload))
        if isinstance(response, Exception):
            raise response
        return response

    return post_json


def test_local_tags_entry_is_stamped_local_with_conservative_facts() -> None:
    # Real /api/tags local entry (trimmed).
    model = OllamaAdapter.normalize_catalog_entry(
        {
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
    )

    assert model.model_id == "ministral-3:8b"
    assert model.family == "mistral3"
    assert model.metadata["ollama"] == {"local": True}
    assert model.capabilities.tools is False
    assert model.context_window is None


def test_proxied_cloud_tags_entry_is_stamped_remote_by_remote_host() -> None:
    # ``remote_host`` is the fact; the ``:cloud`` name suffix is convention.
    model = OllamaAdapter.normalize_catalog_entry(
        {
            "name": "kimi-k2.6:cloud",
            "model": "kimi-k2.6:cloud",
            "remote_model": "kimi-k2.6",
            "remote_host": "https://ollama.com:443",
            "details": {"family": "kimi"},
        }
    )

    assert model.metadata["ollama"] == {"remote": True}
    assert model.family == "kimi"


def test_direct_cloud_connection_is_authoritative_remote_scope() -> None:
    baseline = OllamaAdapter.normalize_catalog_entry(
        {"name": "glm-5.1", "model": "glm-5.1", "details": {"family": "glm5.1"}}
    )

    model = OllamaAdapter.finalize_discovered_model(
        baseline, CLOUD_CONFIG.get_connection("api-key")
    )

    assert baseline.metadata["ollama"] == {"local": True}
    assert model.metadata["ollama"] == {"remote": True}


def test_current_tags_facts_are_used_before_show_enrichment() -> None:
    model = OllamaAdapter.normalize_catalog_entry(
        {
            "model": "ministral-3:8b",
            "details": {"family": "mistral3", "context_length": 262144},
            "capabilities": ["vision", "completion", "tools"],
        }
    )

    assert model.context_window == 262144
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.input_modalities == ("text", "image")


@pytest.mark.parametrize(
    ("capabilities", "task_types"),
    [
        pytest.param(["embedding"], ("text_embedding",), id="embedding-only"),
        pytest.param(
            ["completion", "embedding"], ("chat", "text_output", "text_embedding"), id="both"
        ),
    ],
)
@pytest.mark.asyncio
async def test_embedding_capability_tags_the_model_for_text_embedding(
    capabilities: list[str], task_types: tuple[str, ...]
) -> None:
    tags_model = OllamaAdapter.normalize_catalog_entry(
        {
            "model": "nomic-embed-text:latest",
            "details": {"family": "nomic-bert"},
            "capabilities": capabilities,
        }
    )
    untagged = OllamaAdapter.normalize_catalog_entry(
        {"model": "nomic-embed-text:latest", "details": {"family": "nomic-bert"}}
    )
    show = {
        "capabilities": capabilities,
        "model_info": {"general.architecture": "nomic-bert", "nomic-bert.context_length": 2048},
    }

    enriched = await OllamaAdapter.enrich_discovered_models(
        {"nomic-embed-text:latest": untagged}, _show(show)
    )

    # Both catalog paths read the capability; locality stays stamped.
    for model in (tags_model, enriched["nomic-embed-text:latest"]):
        assert set(model.capabilities.task_types) == set(task_types)
        assert model.metadata["ollama"] == {"local": True}
    assert enriched["nomic-embed-text:latest"].context_window == 2048


def test_tags_entry_without_model_id_raises() -> None:
    with pytest.raises(ProviderError):
        OllamaAdapter.normalize_catalog_entry({"details": {}})


@pytest.mark.asyncio
async def test_show_fills_capabilities_and_the_exact_architecture_window() -> None:
    base = OllamaAdapter.normalize_catalog_entry(
        {"model": "ministral-3:8b", "details": {"family": "mistral3"}}
    )
    posted: list[tuple[str, dict[str, Any]]] = []

    enriched = await OllamaAdapter.enrich_discovered_models(
        {"ministral-3:8b": base}, _show(SHOW_RESPONSE, posted)
    )

    model = enriched["ministral-3:8b"]
    assert posted == [("/api/show", {"model": "ministral-3:8b"})]
    assert model.capabilities.tools is True
    assert model.capabilities.vision is True
    assert model.capabilities.reasoning.supported is False
    # Only "<arch>.context_length" counts, never the rope-scaling original length.
    assert model.context_window == 262144
    assert model.metadata["ollama"] == {"local": True}
    assert model.family == "mistral3"


@pytest.mark.parametrize(
    ("model_id", "control", "levels"),
    [
        ("kimi-k2.6:cloud", REASONING_CONTROL_ON_OFF, ()),
        pytest.param(
            "gpt-oss:20b", REASONING_CONTROL_LEVELS, ("low", "medium", "high"), id="gpt-oss-levels"
        ),
    ],
)
@pytest.mark.asyncio
async def test_thinking_capability_maps_to_the_model_reasoning_control(
    model_id: str, control: str, levels: tuple[str, ...]
) -> None:
    base = OllamaAdapter.normalize_catalog_entry({"model": model_id})
    show = {"capabilities": ["completion", "tools", "thinking"], "model_info": {}}

    enriched = await OllamaAdapter.enrich_discovered_models({model_id: base}, _show(show))

    reasoning = enriched[model_id].capabilities.reasoning
    assert reasoning.supported is True
    assert reasoning.control == control
    assert reasoning.levels == levels


@pytest.mark.asyncio
async def test_discovery_stamps_no_replay_scope_so_thinking_models_inherit_full_history() -> None:
    model_ids = ("glm-4.7:latest", "glm-5.2")
    show = {"capabilities": ["completion", "thinking"], "model_info": {}}

    enriched = await OllamaAdapter.enrich_discovered_models(
        {
            model_id: OllamaAdapter.normalize_catalog_entry({"model": model_id})
            for model_id in model_ids
        },
        _show(show),
    )
    adapter = OllamaAdapter(LOCAL_CONFIG, "", model_lookup=enriched.get)

    for model_id in model_ids:
        assert enriched[model_id].metadata["ollama"] == {"local": True}
        assert adapter.reasoning_replay_policy(model_id) == "full_history"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_failed_show_keeps_the_conservative_baseline() -> None:
    base = OllamaAdapter.normalize_catalog_entry({"model": "broken-model"})

    enriched = await OllamaAdapter.enrich_discovered_models(
        {"broken-model": base}, _show(ProviderError("boom", retryable=False))
    )

    # No enriched entry; discovery keeps the baseline Model.
    assert enriched == {}


@pytest.mark.asyncio
async def test_missing_architecture_leaves_the_window_honestly_unknown() -> None:
    base = OllamaAdapter.normalize_catalog_entry({"model": "odd-model"})
    show = {"capabilities": ["completion"], "model_info": {"other.context_length": 4096}}

    enriched = await OllamaAdapter.enrich_discovered_models({"odd-model": base}, _show(show))

    assert enriched["odd-model"].context_window is None
