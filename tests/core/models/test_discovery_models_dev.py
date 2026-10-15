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
from core.models.models import ModelRegistry, ReasoningCapabilities
from core.models.models_dev import ModelsDevCatalog, refresh_canonical_layer
from core.models.query import ModelQuery
from core.providers.providers import ProviderConfig

from .discovery_test_support import (
    API_KEY,
    FIXTURES_DIR,
    OPENROUTER_MODELS_URL,
    api_key_connection,
    fixture_models_dev_catalog,
    mock_openrouter_image_catalog,
    openrouter_config,
    raw_openrouter_model,
    read_models_file,
    simple_compatible_config,
)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [None, False, True])
@pytest.mark.parametrize("canonical", [True, False])
@pytest.mark.parametrize("refresh_catalog_available", [True, False])
async def test_lab_provider_gets_an_auto_pointer_and_inherits_the_canonical_ladder(
    tmp_path: Path,
    supported: bool | None,
    canonical: bool,
    refresh_catalog_available: bool,
) -> None:
    resources_dir = tmp_path / "resources"
    catalog = fixture_models_dev_catalog()
    without_canonical = {
        key: value for key, value in catalog.models.items() if key != "deepseek/deepseek-v4-pro"
    }
    if not canonical:
        catalog = ModelsDevCatalog({"models": without_canonical, "providers": catalog.providers})
    await refresh_canonical_layer(resources_dir, catalog=catalog)
    config = simple_compatible_config(
        id="deepseek", name="DeepSeek", base_url="https://api.deepseek.com/v1"
    )
    respx.get("https://api.deepseek.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek-v4-pro",
                        "name": "DeepSeek V4 Pro",
                        **(
                            {"reasoning": {"supported": supported}} if supported is not None else {}
                        ),
                    }
                ]
            },
        )
    )

    await refresh_models(config, API_KEY, resources_dir, models_dev_catalog=catalog)

    written = read_models_file(resources_dir, "deepseek.json")["models"]["deepseek-v4-pro"]
    assert written.get("canonical") == ("deepseek/deepseek-v4-pro" if canonical else None)
    registry = ModelRegistry.load(resources_dir)
    reasoning = registry.get("deepseek", "deepseek-v4-pro").capabilities.reasoning
    if supported is False:
        assert written["capabilities"]["reasoning"] == {"supported": False}
        assert reasoning == ReasoningCapabilities(supported=False)
    else:
        # An unknown or bare positive flag can inherit the richer canonical ladder.
        if canonical:
            assert "reasoning" not in written["capabilities"]
        else:
            assert written["capabilities"]["reasoning"] == {
                "supported": True,
                "control": "levels",
                "levels": ["high", "max"],
            }
        assert reasoning == ReasoningCapabilities(
            supported=True, control="levels", levels=("high", "max")
        )
    # The raw models.dev dump the canonical refresh wrote is never loaded as a Provider.
    assert {provider_id for provider_id, _ in registry.query(ModelQuery())} == {"deepseek"}

    # A full feed outage retains a previously inherited ladder when the
    # surviving canonical support still agrees with the endpoint.
    await refresh_models(
        config,
        API_KEY,
        resources_dir,
        models_dev_catalog=catalog if refresh_catalog_available else None,
    )
    assert (
        ModelRegistry.load(resources_dir).get("deepseek", "deepseek-v4-pro").capabilities.reasoning
        == reasoning
    )

    if canonical:
        # Losing a Provider entry must not drop a freshly reported positive
        # flag in favor of a now-contradictory canonical record. Losing both
        # entries must not make this still-listed Model invalid either.
        partial_models = without_canonical
        if supported is True:
            partial_models = {
                **catalog.models,
                "deepseek/deepseek-v4-pro": {
                    **catalog.models["deepseek/deepseek-v4-pro"],
                    "reasoning": False,
                },
            }
        partial = ModelsDevCatalog(
            {
                "models": partial_models,
                "providers": {
                    key: value for key, value in catalog.providers.items() if key != "deepseek"
                },
            }
        )
        await refresh_canonical_layer(resources_dir, catalog=partial)
        await refresh_models(
            config,
            API_KEY,
            resources_dir,
            models_dev_catalog=partial if refresh_catalog_available else None,
        )
        refreshed = ModelRegistry.load(resources_dir).get("deepseek", "deepseek-v4-pro")
        assert refreshed.capabilities.reasoning == ReasoningCapabilities(supported=supported)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_support", [False, True])
async def test_exact_provider_reasoning_flag_beats_the_canonical_fact(
    tmp_path: Path, provider_support: bool
) -> None:
    raw_catalog = json.loads((FIXTURES_DIR / "models_dev_catalog.json").read_text("utf-8"))
    raw_catalog["models"]["deepseek/deepseek-v4-pro"]["reasoning"] = not provider_support
    provider_entry = raw_catalog["providers"]["deepseek"]["models"]["deepseek-v4-pro"]
    provider_entry["reasoning"] = provider_support
    provider_entry.pop("reasoning_options", None)
    catalog = ModelsDevCatalog(raw_catalog)
    await refresh_canonical_layer(tmp_path, catalog=catalog)
    config = simple_compatible_config(
        id="deepseek", name="DeepSeek", base_url="https://api.deepseek.com/v1"
    )
    respx.get("https://api.deepseek.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}]})
    )

    await refresh_models(config, API_KEY, tmp_path, models_dev_catalog=catalog)

    loaded = ModelRegistry.load(tmp_path).get("deepseek", "deepseek-v4-pro")
    assert loaded.capabilities.reasoning == ReasoningCapabilities(supported=provider_support)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("models_dev_id", ["anthropic", "gateway"])
async def test_a_ladder_the_provider_reports_survives_the_canonical_join(
    tmp_path: Path, models_dev_id: str
) -> None:
    resources_dir = tmp_path / "resources"
    raw_catalog = json.loads((FIXTURES_DIR / "models_dev_catalog.json").read_text("utf-8"))
    lab_model = raw_catalog["providers"]["deepseek"]["models"]["deepseek-v4-pro"]
    raw_catalog["models"]["anthropic/claude-x"] = {
        **raw_catalog["models"]["deepseek/deepseek-v4-pro"],
        "id": "anthropic/claude-x",
        "name": "Claude X",
    }
    raw_catalog["providers"]["anthropic"] = {
        "id": "anthropic",
        "models": {
            "claude-x": {
                **lab_model,
                "id": "claude-x",
                "reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}],
            }
        },
    }
    raw_catalog["providers"]["gateway"] = raw_catalog["providers"]["anthropic"]
    catalog = ModelsDevCatalog(raw_catalog)
    await refresh_canonical_layer(resources_dir, catalog=catalog)
    config = ProviderConfig(
        id="anthropic",
        name="Anthropic",
        adapter="anthropic",
        base_url="https://api.anthropic.com/v1",
        connections=[api_key_connection("ANTHROPIC_API_KEY")],
        defaults={"max_tokens": 8192},
        models_endpoint="/models",
        models_dev_id=models_dev_id,
    )
    respx.get(url__startswith="https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "claude-x",
                        "display_name": "Claude X",
                        "capabilities": {
                            "thinking": {
                                "supported": True,
                                "types": {"adaptive": {"supported": True}},
                            },
                            "effort": {"low": {"supported": True}, "high": {"supported": True}},
                        },
                    }
                ]
            },
        )
    )

    await refresh_models(config, API_KEY, resources_dir, models_dev_catalog=catalog)

    written = read_models_file(resources_dir, "anthropic.json")["models"]["claude-x"]
    assert written.get("canonical") == (
        "anthropic/claude-x" if models_dev_id == "anthropic" else None
    )
    assert written["capabilities"]["reasoning"] == {
        "supported": True,
        "control": "levels",
        "levels": ["low", "high"],
    }
    loaded = ModelRegistry.load(resources_dir).get("anthropic", "claude-x")
    assert loaded.capabilities.reasoning.levels == ("low", "high")


@respx.mock
@pytest.mark.asyncio
async def test_gateway_provider_records_its_deviating_ladder_and_reasoning_facts(
    tmp_path: Path,
) -> None:
    resources_dir = tmp_path / "resources"
    catalog = fixture_models_dev_catalog()
    await refresh_canonical_layer(resources_dir, catalog=catalog)
    mock_openrouter_image_catalog()
    deviating = raw_openrouter_model(
        model_id="deepseek/deepseek-v4-pro",
        name="DeepSeek V4 Pro",
        input_modalities=["text"],
        supported_parameters=["tools", "reasoning"],
    )
    # models.dev publishes no ladder for this one, so it inherits the canonical record.
    inheriting = raw_openrouter_model(model_id="deepseek/deepseek-r1", name="DeepSeek R1")
    for raw in (deviating, inheriting):
        raw["reasoning"] = {"mandatory": True}
    gemini = raw_openrouter_model(model_id="google/gemini-2.5-flash", name="Gemini 2.5 Flash")
    respx.get(OPENROUTER_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [deviating, inheriting, gemini]})
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
    assert "interleaved_field" not in (
        written["google/gemini-2.5-flash"].get("metadata", {}).get("openrouter", {})
    )
    # The inheriting Model drops only its bare flag; the reported fact stays.
    assert written["deepseek/deepseek-r1"]["capabilities"]["reasoning"] == {"mandatory": True}
    registry = ModelRegistry.load(resources_dir)
    deepseek = registry.get("openrouter", "deepseek/deepseek-v4-pro")
    assert deepseek.capabilities.reasoning == ReasoningCapabilities(
        supported=True, control="levels", levels=("high", "xhigh"), mandatory=True
    )
    assert deepseek.metadata["openrouter"]["interleaved_field"] == "reasoning_content"
    # ... and merges under the inherited canonical reasoning.
    assert registry.get(
        "openrouter", "deepseek/deepseek-r1"
    ).capabilities.reasoning == ReasoningCapabilities(supported=True, mandatory=True)


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
@pytest.mark.parametrize("supported", [None, False, True])
async def test_bare_endpoint_is_filled_from_the_provider_section(
    tmp_path: Path, supported: bool | None
) -> None:
    """Limits, family and widened modalities come from models.dev when the
    endpoint reports only id and name."""

    resources_dir = tmp_path / "resources"
    config = simple_compatible_config(
        id="alibaba", name="Alibaba", base_url="https://example.test/v1"
    )
    respx.get("https://example.test/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "qwen3.5-plus",
                        "name": "Qwen3.5 Plus",
                        **(
                            {"reasoning": {"supported": supported}} if supported is not None else {}
                        ),
                        **(
                            {"context_window": 200000, "max_output_tokens": 10000}
                            if supported is False
                            else {}
                        ),
                    }
                ]
            },
        )
    )

    await refresh_models(
        config, API_KEY, resources_dir, models_dev_catalog=fixture_models_dev_catalog()
    )

    model = read_models_file(resources_dir, "alibaba.json")["models"]["qwen3.5-plus"]
    assert (model["context_window"], model["max_output_tokens"], model["family"]) == (
        200000 if supported is False else 1000000,
        10000 if supported is False else 65536,
        "qwen",
    )
    assert model["capabilities"]["input_modalities"] == ["text", "image", "video"]
    assert model["capabilities"]["vision"] is True
    if supported is False:
        assert model["capabilities"]["reasoning"] == {"supported": False}
    else:
        assert "reasoning" not in model["capabilities"]
