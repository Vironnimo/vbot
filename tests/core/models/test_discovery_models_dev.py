"""Models: models.dev enrichment of a Provider refresh.

The fixture capture's ``deepseek-v4-pro`` is the worked example: the canonical
layer holds the lab ladder ``[high, max]``; OpenRouter deviates to
``[high, xhigh]``; the lab's own section does not deviate.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.models.discovery import refresh_models
from core.models.models import ModelRegistry
from core.models.models_dev import ModelsDevCatalog, refresh_canonical_layer
from core.models.query import ModelQuery

from .discovery_test_support import (
    API_KEY,
    FIXTURES_DIR,
    OPENROUTER_MODELS_URL,
    fixture_models_dev_catalog,
    mock_openrouter_image_catalog,
    openrouter_config,
    raw_openrouter_model,
    read_models_file,
    simple_compatible_config,
)


@respx.mock
@pytest.mark.asyncio
async def test_lab_provider_gets_an_auto_pointer_and_inherits_the_canonical_ladder(
    tmp_path: Path,
) -> None:
    resources_dir = tmp_path / "resources"
    catalog = fixture_models_dev_catalog()
    await refresh_canonical_layer(resources_dir, catalog=catalog)
    config = simple_compatible_config(
        id="deepseek", name="DeepSeek", base_url="https://api.deepseek.com/v1"
    )
    respx.get("https://api.deepseek.com/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"}]}
        )
    )

    await refresh_models(config, API_KEY, resources_dir, models_dev_catalog=catalog)

    written = read_models_file(resources_dir, "deepseek.json")["models"]["deepseek-v4-pro"]
    assert written["canonical"] == "deepseek/deepseek-v4-pro"
    # A non-deviating Provider layer carries no ladder of its own.
    assert "reasoning" not in written["capabilities"]
    registry = ModelRegistry.load(resources_dir)
    assert registry.get("deepseek", "deepseek-v4-pro").capabilities.reasoning.levels == (
        "high",
        "max",
    )
    # The raw models.dev dump the canonical refresh wrote is never loaded as a Provider.
    assert {provider_id for provider_id, _ in registry.query(ModelQuery())} == {"deepseek"}


@respx.mock
@pytest.mark.asyncio
async def test_gateway_provider_records_its_deviating_ladder_and_reasoning_field(
    tmp_path: Path,
) -> None:
    resources_dir = tmp_path / "resources"
    catalog = fixture_models_dev_catalog()
    await refresh_canonical_layer(resources_dir, catalog=catalog)
    mock_openrouter_image_catalog()
    respx.get(OPENROUTER_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    raw_openrouter_model(
                        model_id="deepseek/deepseek-v4-pro",
                        name="DeepSeek V4 Pro",
                        input_modalities=["text"],
                        supported_parameters=["tools", "reasoning"],
                    ),
                    raw_openrouter_model(
                        model_id="google/gemini-2.5-flash", name="Gemini 2.5 Flash"
                    ),
                ]
            },
        )
    )

    await refresh_models(openrouter_config(), API_KEY, resources_dir, models_dev_catalog=catalog)

    written = read_models_file(resources_dir, "openrouter.json")["models"]
    canonical = read_models_file(resources_dir, "models.json")["models"]
    assert canonical["deepseek/deepseek-v4-pro"]["capabilities"]["reasoning"] == {
        "supported": True,
        "control": "levels",
        "levels": ["high", "max"],
    }
    # models.dev ``interleaved`` projects to the Provider-scoped reasoning field.
    assert "reasoning_response_field" not in (
        written["google/gemini-2.5-flash"].get("metadata", {}).get("openrouter", {})
    )
    deepseek = ModelRegistry.load(resources_dir).get("openrouter", "deepseek/deepseek-v4-pro")
    assert (deepseek.capabilities.reasoning.control, deepseek.capabilities.reasoning.levels) == (
        "levels",
        ("high", "xhigh"),
    )
    assert deepseek.metadata["openrouter"]["reasoning_response_field"] == "reasoning_content"


@respx.mock
@pytest.mark.asyncio
async def test_the_provider_catalog_price_wins_over_models_dev(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    raw_catalog = json.loads((FIXTURES_DIR / "models_dev_catalog.json").read_text("utf-8"))
    raw_catalog["providers"]["openrouter"]["models"]["qwen/qwen3-embedding-8b"] = {
        "cost": {"input": 0.5}
    }
    embedding = raw_openrouter_model(
        model_id="qwen/qwen3-embedding-8b",
        name="Qwen3 Embedding 8B",
        output_modalities=["embeddings"],
    )
    embedding["pricing"] = {"prompt": "0.00000001", "completion": "0"}
    mock_openrouter_image_catalog()
    respx.get(OPENROUTER_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [embedding]})
    )

    await refresh_models(
        openrouter_config(),
        API_KEY,
        resources_dir,
        models_dev_catalog=ModelsDevCatalog(raw_catalog),
    )

    written = read_models_file(resources_dir, "openrouter.json")["models"]
    assert written["qwen/qwen3-embedding-8b"]["pricing"] == {
        "source": "openrouter:qwen/qwen3-embedding-8b",
        "rates": {"input": 0.01},
    }


@respx.mock
@pytest.mark.asyncio
async def test_bare_endpoint_is_filled_from_the_provider_section(tmp_path: Path) -> None:
    """Limits, family and widened modalities come from models.dev when the
    endpoint reports only id and name."""

    resources_dir = tmp_path / "resources"
    config = simple_compatible_config(
        id="alibaba", name="Alibaba", base_url="https://example.test/v1"
    )
    respx.get("https://example.test/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "qwen3.5-plus", "name": "Qwen3.5 Plus"}]}
        )
    )

    await refresh_models(
        config, API_KEY, resources_dir, models_dev_catalog=fixture_models_dev_catalog()
    )

    model = read_models_file(resources_dir, "alibaba.json")["models"]["qwen3.5-plus"]
    assert (model["context_window"], model["max_output_tokens"], model["family"]) == (
        1000000,
        65536,
        "qwen",
    )
    assert model["capabilities"]["input_modalities"] == ["text", "image", "video"]
    assert model["capabilities"]["vision"] is True
