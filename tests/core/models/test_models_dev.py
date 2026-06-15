"""Tests for the models.dev catalog client + projection (Phase 3 refresh).

Fixture-driven (``fixtures/models_dev_catalog.json``, a trimmed real capture):
no network. Covers control derivation (all four branches incl. "effort wins"),
the lab-spec ladder lift, the canonical projection shape (matching the assembly
contract), per-provider deviating ladders + auto canonical pointers, the raw
dump, and shape verification.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    provider_reasoning_block,
    reasoning_capability_block,
    reasoning_response_field,
    refresh_canonical_layer,
    write_raw_catalog,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CATALOG_FIXTURE = FIXTURES_DIR / "models_dev_catalog.json"


@pytest.fixture()
def catalog() -> ModelsDevCatalog:
    raw = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    return ModelsDevCatalog(raw)


# ---------------------------------------------------------------------------
# Control derivation — all four branches
# ---------------------------------------------------------------------------


def test_derive_control_effort_yields_levels():
    # Arrange
    options = [{"type": "effort", "values": ["low", "medium", "high"]}]
    # Act
    result = derive_reasoning_control(options)
    # Assert
    assert result == {"control": "levels", "levels": ["low", "medium", "high"]}


def test_derive_control_budget_tokens_yields_budget_with_max():
    # Arrange
    options = [{"type": "budget_tokens", "min": 1024, "max": 32768}]
    # Act
    result = derive_reasoning_control(options)
    # Assert
    assert result == {"control": "budget", "budget_max": 32768}


def test_derive_control_budget_tokens_without_max_omits_budget_max():
    # Arrange — ~half of budget_tokens options carry no ``max`` (handoff).
    options = [{"type": "budget_tokens", "min": 1024}]
    # Act
    result = derive_reasoning_control(options)
    # Assert
    assert result == {"control": "budget"}


def test_derive_control_toggle_yields_on_off():
    # Arrange
    options = [{"type": "toggle"}]
    # Act
    result = derive_reasoning_control(options)
    # Assert
    assert result == {"control": "on_off"}


def test_derive_control_effort_wins_over_toggle_and_budget():
    # Arrange — Claude-style: effort + budget + toggle all present.
    options = [
        {"type": "toggle"},
        {"type": "budget_tokens", "min": 1024, "max": 64000},
        {"type": "effort", "values": ["high", "max"]},
    ]
    # Act
    result = derive_reasoning_control(options)
    # Assert — effort wins.
    assert result == {"control": "levels", "levels": ["high", "max"]}


def test_derive_control_empty_or_none_yields_none():
    assert derive_reasoning_control(None) is None
    assert derive_reasoning_control([]) is None


def test_derive_control_drops_unknown_effort_values():
    # Arrange — an effort value vBot does not know is dropped, not crashed on.
    options = [{"type": "effort", "values": ["low", "ludicrous", "high"]}]
    # Act
    result = derive_reasoning_control(options)
    # Assert
    assert result == {"control": "levels", "levels": ["low", "high"]}


def test_reasoning_capability_block_unsupported_is_bare():
    assert reasoning_capability_block(supported=False, reasoning_options=[{"type": "toggle"}]) == {
        "supported": False
    }


def test_reasoning_capability_block_supported_without_options_is_bare():
    # A supported model with no usable options serializes to bare supported —
    # no fabricated ladder.
    assert reasoning_capability_block(supported=True, reasoning_options=None) == {"supported": True}


# ---------------------------------------------------------------------------
# Ladder lift from the lab provider — no union
# ---------------------------------------------------------------------------


def test_lift_canonical_ladder_from_lab_provider(catalog: ModelsDevCatalog):
    # Arrange / Act — deepseek lab carries effort [high, max] for deepseek-v4-pro.
    lifted = lift_canonical_ladder(catalog, "deepseek/deepseek-v4-pro")
    # Assert — the lab spec, NOT OpenRouter's deviating [high, xhigh].
    assert lifted == {"control": "levels", "levels": ["high", "max"]}


def test_lift_canonical_ladder_no_lab_provider_section(catalog: ModelsDevCatalog):
    # deepseek-r1 is reasoning-capable but the deepseek provider does not key it
    # by wire-id ``deepseek-r1`` → hand path, no lift.
    assert lift_canonical_ladder(catalog, "deepseek/deepseek-r1") is None


def test_lift_canonical_ladder_lab_has_id_but_no_options(catalog: ModelsDevCatalog):
    # qwen3.5-plus is reasoning:true at the lab but carries no reasoning_options
    # → no fabricated ladder.
    assert lift_canonical_ladder(catalog, "alibaba/qwen3.5-plus") is None


# ---------------------------------------------------------------------------
# Canonical projection — matches the assembly contract
# ---------------------------------------------------------------------------


def test_project_canonical_models_worked_example(catalog: ModelsDevCatalog):
    # Act
    projected = project_canonical_models(catalog)
    # Assert — deepseek/deepseek-v4-pro matches the assembly file-format contract.
    record = projected["deepseek/deepseek-v4-pro"]
    assert record["name"] == "DeepSeek V4 Pro"
    assert record["family"] == "deepseek-thinking"
    assert "provider_id" not in record  # NOT a provider file
    caps = record["capabilities"]
    assert caps["vision"] is False
    assert caps["tools"] is True
    assert caps["json_mode"] is True
    assert caps["reasoning"] == {"supported": True, "control": "levels", "levels": ["high", "max"]}
    assert caps["input_modalities"] == ["text"]
    assert caps["output_modalities"] == ["text"]
    assert record["context_window"] == 1000000
    assert record["max_output_tokens"] == 384000


def test_project_canonical_stores_modalities_verbatim_incl_pdf_video(catalog: ModelsDevCatalog):
    # Act
    projected = project_canonical_models(catalog)
    # Assert — pdf and video kept verbatim, no normalization.
    caps = projected["google/gemini-2.5-flash"]["capabilities"]
    assert caps["input_modalities"] == ["text", "image", "audio", "video", "pdf"]
    assert caps["vision"] is True  # derived from "image" being present


def test_project_canonical_missing_structured_output_is_json_mode_false(catalog: ModelsDevCatalog):
    # qwen3.5-plus has no structured_output field in the fixture's canonical entry.
    projected = project_canonical_models(catalog)
    caps = projected["alibaba/qwen3.5-plus"]["capabilities"]
    assert caps["json_mode"] is False


def test_project_canonical_supported_no_options_is_bare_reasoning(catalog: ModelsDevCatalog):
    projected = project_canonical_models(catalog)
    assert projected["alibaba/qwen3.5-plus"]["capabilities"]["reasoning"] == {"supported": True}


def test_project_canonical_non_reasoning_two_id_mirror(catalog: ModelsDevCatalog):
    # xAI models the non-reasoning twin as its own id; mirror it 1:1 with
    # reasoning unsupported.
    projected = project_canonical_models(catalog)
    record = projected["xai/grok-4.20-0309-non-reasoning"]
    assert record["capabilities"]["reasoning"] == {"supported": False}


def test_project_canonical_temperature_false_is_a_signal(catalog: ModelsDevCatalog):
    # ``temperature: true`` contributes the supported parameter; a model with
    # temperature false does not. deepseek-v4-pro has temperature true.
    projected = project_canonical_models(catalog)
    caps = projected["deepseek/deepseek-v4-pro"]["capabilities"]
    assert "temperature" in caps["supported_parameters"]


# ---------------------------------------------------------------------------
# Per-provider enrichment: auto canonical pointer + deviating ladder
# ---------------------------------------------------------------------------


def test_auto_canonical_pointer_for_lab_section(catalog: ModelsDevCatalog):
    # deepseek lab section has wire-id deepseek-v4-pro → pointer to the canonical id.
    pointer = auto_canonical_pointer(
        catalog, models_dev_id="deepseek", wire_id="deepseek-v4-pro"
    )
    assert pointer == "deepseek/deepseek-v4-pro"


def test_auto_canonical_pointer_absent_when_no_section_match(catalog: ModelsDevCatalog):
    # opencode-go is not in the fixture providers → no pointer.
    assert auto_canonical_pointer(catalog, models_dev_id="opencode-go", wire_id="x") is None


def test_provider_reasoning_block_deviation(catalog: ModelsDevCatalog):
    # OpenRouter carries [high, xhigh] for deepseek/deepseek-v4-pro — deviates
    # from the lab spec [high, max] → a provider-layer block is returned.
    block = provider_reasoning_block(
        catalog,
        models_dev_id="openrouter",
        wire_id="deepseek/deepseek-v4-pro",
    )
    # ``supported`` is sourced from the OpenRouter section's OWN reasoning flag,
    # consistent with the derived control — never from a thin provider's bare
    # adapter normalization (the regression that produced supported:false+ladder).
    assert block == {"supported": True, "control": "levels", "levels": ["high", "xhigh"]}


def test_provider_reasoning_block_no_deviation_returns_none(catalog: ModelsDevCatalog):
    # The lab provider deepseek does not deviate from its own spec → None, so
    # the model inherits the canonical ladder at load.
    block = provider_reasoning_block(
        catalog,
        models_dev_id="deepseek",
        wire_id="deepseek-v4-pro",
    )
    assert block is None


def test_reasoning_response_field_from_interleaved(catalog: ModelsDevCatalog):
    # The OpenRouter section's deepseek-v4-pro carries interleaved:
    # {field: reasoning_content} → that field name is projected.
    field = reasoning_response_field(
        catalog,
        models_dev_id="openrouter",
        wire_id="deepseek/deepseek-v4-pro",
    )
    assert field == "reasoning_content"


def test_reasoning_response_field_absent_returns_none(catalog: ModelsDevCatalog):
    # google/gemini-2.5-flash carries no ``interleaved`` → None (graceful: the
    # adapter keeps its hardcoded default-key scan).
    field = reasoning_response_field(
        catalog,
        models_dev_id="openrouter",
        wire_id="google/gemini-2.5-flash",
    )
    assert field is None


def test_reasoning_response_field_unknown_model_returns_none(catalog: ModelsDevCatalog):
    field = reasoning_response_field(
        catalog,
        models_dev_id="openrouter",
        wire_id="does/not-exist",
    )
    assert field is None


# ---------------------------------------------------------------------------
# Raw dump + canonical refresh orchestration
# ---------------------------------------------------------------------------


def test_write_raw_catalog_writes_dump(tmp_path: Path, catalog: ModelsDevCatalog):
    models_dir = tmp_path / "models"
    raw_path = write_raw_catalog(catalog, models_dir)
    assert raw_path.name == RAW_CATALOG_FILE_NAME
    written = json.loads(raw_path.read_text(encoding="utf-8"))
    assert "models" in written and "providers" in written


@pytest.mark.asyncio
async def test_refresh_canonical_layer_writes_files(tmp_path: Path, catalog: ModelsDevCatalog):
    # Act — reuse the fixture catalog (no fetch).
    result = await refresh_canonical_layer(tmp_path, catalog=catalog)
    models_dir = tmp_path / "models"
    # Assert — models.json + raw dump + seeded overrides structure.
    assert (models_dir / "models.json").exists()
    assert (models_dir / RAW_CATALOG_FILE_NAME).exists()
    assert (models_dir / "models.overrides.json").exists()
    assert result["model_count"] == len(catalog.models)
    canonical = json.loads((models_dir / "models.json").read_text(encoding="utf-8"))
    assert "deepseek/deepseek-v4-pro" in canonical["models"]


@pytest.mark.asyncio
async def test_refresh_canonical_layer_does_not_clobber_existing_overrides(
    tmp_path: Path, catalog: ModelsDevCatalog
):
    # Arrange — a hand-edited overrides file must survive a refresh.
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    hand = {"models": {"meta/llama-4-scout-17b-instruct": {"capabilities": {"reasoning": {}}}}}
    (models_dir / "models.overrides.json").write_text(json.dumps(hand), encoding="utf-8")
    # Act
    await refresh_canonical_layer(tmp_path, catalog=catalog)
    # Assert — hand content untouched.
    after = json.loads((models_dir / "models.overrides.json").read_text(encoding="utf-8"))
    assert "meta/llama-4-scout-17b-instruct" in after["models"]


# ---------------------------------------------------------------------------
# fetch_catalog — mocked transport + shape verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_catalog_parses_mocked_response():
    # Arrange
    raw = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == MODELS_DEV_CATALOG_URL
        return httpx.Response(200, json=raw)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # Act
        catalog = await fetch_catalog(client=client)
    # Assert
    assert "deepseek/deepseek-v4-pro" in catalog.models


@pytest.mark.asyncio
async def test_fetch_catalog_aborts_on_diverged_shape():
    # Arrange — a response missing the top-level providers map.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": {"x/y": {}}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # Act / Assert
        with pytest.raises(ModelsDevError):
            await fetch_catalog(client=client)


def test_catalog_construction_rejects_missing_modalities():
    # Arrange — a model object without modalities.input diverges from the table.
    raw = {"models": {"x/y": {"reasoning": True}}, "providers": {"p": {"models": {}}}}
    # Act / Assert
    with pytest.raises(ModelsDevError):
        ModelsDevCatalog(raw)
