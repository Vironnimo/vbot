"""Models: the models.dev catalog client and its projection for refresh.

Fixture-driven (``fixtures/models_dev_catalog.json``, a trimmed real capture), no
network: reasoning control derivation, the lab-spec ladder lift, the canonical
projection (the assembly file contract), per-provider section facts, the
canonical refresh, and shape verification.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.models.models_dev import (
    MODELS_DEV_CATALOG_URL,
    RAW_CATALOG_FILE_NAME,
    ModelsDevCatalog,
    ModelsDevError,
    auto_canonical_pointer,
    derive_reasoning_control,
    fetch_catalog,
    lift_canonical_ladder,
    project_canonical_models,
    provider_family,
    provider_limits,
    provider_modalities,
    provider_reasoning_block,
    provider_reasoning_supported,
    reasoning_response_field,
    refresh_canonical_layer,
)

CATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "models_dev_catalog.json"


def _raw_catalog() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    return raw


@pytest.fixture(scope="module")
def catalog() -> ModelsDevCatalog:
    """Read-only for every test that uses it."""

    return ModelsDevCatalog(_raw_catalog())


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        pytest.param(
            [{"type": "effort", "values": ["low", "ludicrous", "high"]}],
            {"control": "levels", "levels": ["low", "high"]},
            id="effort-keeps-known-levels",
        ),
        pytest.param(
            [{"type": "budget_tokens", "min": 1024, "max": 32768}],
            {"control": "budget", "budget_max": 32768},
            id="budget-with-max",
        ),
        # About half of the budget_tokens options carry no max.
        pytest.param(
            [{"type": "budget_tokens", "min": 1024}], {"control": "budget"}, id="budget-without-max"
        ),
        pytest.param([{"type": "toggle"}], {"control": "on_off"}, id="toggle"),
        pytest.param(
            [
                {"type": "toggle"},
                {"type": "budget_tokens", "min": 1024, "max": 64000},
                {"type": "effort", "values": ["high", "max"]},
            ],
            {"control": "levels", "levels": ["high", "max"]},
            id="effort-wins",
        ),
        pytest.param(None, None, id="no-options"),
    ],
)
def test_reasoning_control_derives_from_options(
    options: list[dict[str, Any]] | None, expected: dict[str, Any] | None
) -> None:
    assert derive_reasoning_control(options) == expected


@pytest.mark.parametrize(
    ("canonical_id", "expected"),
    [
        # The lab spec, not OpenRouter's deviating [high, xhigh].
        ("deepseek/deepseek-v4-pro", {"control": "levels", "levels": ["high", "max"]}),
        # Reasoning-capable, but the lab does not key it by this wire id: hand path.
        ("deepseek/deepseek-r1", None),
        # Reasoning at the lab without options: no fabricated ladder.
        ("alibaba/qwen3.5-plus", None),
    ],
)
def test_canonical_ladder_lifts_only_the_lab_spec(
    catalog: ModelsDevCatalog, canonical_id: str, expected: dict[str, Any] | None
) -> None:
    assert lift_canonical_ladder(catalog, canonical_id) == expected


def test_canonical_projection_follows_the_assembly_file_contract(
    catalog: ModelsDevCatalog,
) -> None:
    projected = project_canonical_models(catalog)

    # No provider_id: a canonical record is not a Provider file.
    assert projected["deepseek/deepseek-v4-pro"] == {
        "name": "DeepSeek V4 Pro",
        "family": "deepseek-thinking",
        "capabilities": {
            "vision": False,
            "tools": True,
            "json_mode": True,
            "reasoning": {"supported": True, "control": "levels", "levels": ["high", "max"]},
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_parameters": ["temperature"],
        },
        "context_window": 1000000,
        "max_output_tokens": 384000,
        "temperature": True,
        "knowledge": "2025-05",
        "release_date": "2026-04-24",
        "last_updated": "2026-04-24",
        "pricing": {
            "source": "models.dev:deepseek/deepseek-v4-pro",
            "rates": {"input": 0.435, "output": 0.87, "cache_read": 0.003625},
        },
    }
    capabilities = {
        model_id: {
            key: projected[model_id]["capabilities"][key]
            for key in ("vision", "json_mode", "reasoning", "input_modalities")
        }
        for model_id in (
            "google/gemini-2.5-flash",
            "alibaba/qwen3.5-plus",
            "xai/grok-4.20-0309-non-reasoning",
        )
    }
    assert capabilities == {
        # pdf and video stay verbatim; vision derives from image input.
        "google/gemini-2.5-flash": {
            "vision": True,
            "json_mode": True,
            "reasoning": {"supported": True, "control": "budget", "budget_max": 24576},
            "input_modalities": ["text", "image", "audio", "video", "pdf"],
        },
        # No structured_output means no json_mode; reasoning without options stays bare.
        "alibaba/qwen3.5-plus": {
            "vision": True,
            "json_mode": False,
            "reasoning": {"supported": True},
            "input_modalities": ["text", "image", "video"],
        },
        # xAI's non-reasoning twin is its own id, mirrored with reasoning unsupported.
        "xai/grok-4.20-0309-non-reasoning": {
            "vision": True,
            "json_mode": True,
            "reasoning": {"supported": False},
            "input_modalities": ["text", "image", "pdf"],
        },
    }


def test_provider_sections_supply_the_facts_bare_endpoints_omit(
    catalog: ModelsDevCatalog,
) -> None:
    openrouter_v4 = {"models_dev_id": "openrouter", "wire_id": "deepseek/deepseek-v4-pro"}
    openrouter_gemini = {"models_dev_id": "openrouter", "wire_id": "google/gemini-2.5-flash"}
    lab_v4 = {"models_dev_id": "deepseek", "wire_id": "deepseek-v4-pro"}

    assert auto_canonical_pointer(catalog, **lab_v4) == "deepseek/deepseek-v4-pro"
    # A deviating ladder is stamped with the section's own reasoning flag.
    assert provider_reasoning_block(catalog, **openrouter_v4) == {
        "supported": True,
        "control": "levels",
        "levels": ["high", "xhigh"],
    }
    # The lab does not deviate from its own spec; the Model inherits it at load.
    assert provider_reasoning_block(catalog, **lab_v4) is None
    assert reasoning_response_field(catalog, **openrouter_v4) == "reasoning_content"
    assert reasoning_response_field(catalog, **openrouter_gemini) is None
    assert provider_limits(catalog, **openrouter_v4) == (1048576, 384000)
    assert provider_modalities(catalog, **openrouter_gemini) == (
        ["text", "image", "audio", "video", "pdf"],
        ["text"],
    )
    assert provider_family(catalog, **lab_v4) == "deepseek-thinking"
    assert provider_reasoning_supported(catalog, **lab_v4) is True
    assert (
        provider_reasoning_supported(
            catalog, models_dev_id="xai", wire_id="grok-4.20-0309-non-reasoning"
        )
        is False
    )


@pytest.mark.parametrize(
    ("lookup", "expected"),
    [
        (auto_canonical_pointer, None),
        (reasoning_response_field, None),
        (provider_limits, (None, None)),
        (provider_modalities, None),
        (provider_family, None),
        (provider_reasoning_supported, None),
    ],
)
def test_provider_section_lookups_tolerate_unknown_models(
    catalog: ModelsDevCatalog, lookup: Callable[..., object], expected: object
) -> None:
    assert lookup(catalog, models_dev_id="openrouter", wire_id="does/not-exist") == expected
    assert lookup(catalog, models_dev_id="opencode-go", wire_id="x") == expected


@pytest.mark.asyncio
async def test_canonical_refresh_writes_the_layer_and_seeds_overrides(
    tmp_path: Path, catalog: ModelsDevCatalog
) -> None:
    models_dir = tmp_path / "models"

    result = await refresh_canonical_layer(tmp_path, catalog=catalog)

    canonical = json.loads((models_dir / "models.json").read_text(encoding="utf-8"))
    raw = json.loads((models_dir / RAW_CATALOG_FILE_NAME).read_text(encoding="utf-8"))
    assert "deepseek/deepseek-v4-pro" in canonical["models"]
    assert {"models", "providers"} <= set(raw)
    assert (models_dir / "models.overrides.json").exists()
    assert result["model_count"] == len(catalog.models)
    assert result["raw_path"] == str(models_dir / RAW_CATALOG_FILE_NAME)


@pytest.mark.asyncio
async def test_canonical_refresh_keeps_hand_overrides(
    tmp_path: Path, catalog: ModelsDevCatalog
) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    hand: dict[str, Any] = {
        "models": {"meta/llama-4-scout-17b-instruct": {"capabilities": {"reasoning": {}}}}
    }
    (models_dir / "models.overrides.json").write_text(json.dumps(hand), encoding="utf-8")

    await refresh_canonical_layer(tmp_path, catalog=catalog)

    assert json.loads((models_dir / "models.overrides.json").read_text(encoding="utf-8")) == hand


@pytest.mark.asyncio
async def test_canonical_refresh_prices_provider_files_without_credentials(
    tmp_path: Path,
) -> None:
    raw = _raw_catalog()
    raw["providers"]["openai"]["models"]["gpt-5.5"]["cost"] = {
        "input": 2,
        "output": 8,
        "cache_read": 0.2,
    }
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    path = models_dir / "gateway.json"
    path.write_text(
        json.dumps(
            {
                "provider_id": "gateway",
                "models": {
                    "gpt-5.5": {
                        "pricing": {"source": "models.dev:openai/gpt-5.5", "rates": {"input": 99}}
                    },
                    "gpt-5.5-guess": {
                        "pricing": {
                            "source": "models.dev:openai/gpt-5.5-guess",
                            "rates": {"input": 99},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    await refresh_canonical_layer(
        tmp_path, catalog=ModelsDevCatalog(raw), provider_catalog_ids={"gateway": "openai"}
    )

    models = json.loads(path.read_text(encoding="utf-8"))["models"]
    assert models["gpt-5.5"]["pricing"]["rates"] == {"input": 2, "output": 8, "cache_read": 0.2}
    assert "pricing" not in models["gpt-5.5-guess"]
    canonical = json.loads((models_dir / "models.json").read_text(encoding="utf-8"))
    assert canonical["models"]["openai/gpt-5.5"]["pricing"]["rates"]["input"] == 2


@pytest.mark.asyncio
async def test_fetch_catalog_reads_the_public_endpoint() -> None:
    raw = _raw_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == MODELS_DEV_CATALOG_URL
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = await fetch_catalog(client=client)

    assert "deepseek/deepseek-v4-pro" in catalog.models


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"models": {"x/y": {}}}, id="missing-providers"),
        pytest.param(
            {"models": {"x/y": {"reasoning": True}}, "providers": {"p": {"models": {}}}},
            id="model-without-modalities",
        ),
    ],
)
async def test_fetch_catalog_aborts_on_a_diverged_shape(payload: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelsDevError):
            await fetch_catalog(client=client)
