"""Models: at-load assembly of the canonical, provider, and override layers.

Table tests pin the join and merge rules of ``core.models.assembly``; the
end-to-end tests load the worked example under ``fixtures/assembly/`` (the
``deepseek-v4-pro`` Model on two Providers with two effective ladders).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from core.models.assembly import (
    assemble_provider_model,
    load_canonical_layer,
    merge_layers,
    resolve_canonical_id,
)
from core.models.models import ModelRegistry

ASSEMBLY_FIXTURES = Path(__file__).parent / "fixtures" / "assembly"


@pytest.mark.parametrize(
    ("wire_id", "provider_model", "override_model", "canonical_ids", "expected"),
    [
        pytest.param(
            "wire-id",
            {"canonical": "lab/auto"},
            {"canonical": "lab/manual"},
            ["lab/auto", "lab/manual"],
            "lab/manual",
            id="manual-pointer-beats-auto-pointer",
        ),
        pytest.param(
            "wire-id", {"canonical": "lab/auto"}, None, ["lab/auto"], "lab/auto", id="auto-pointer"
        ),
        pytest.param(
            "lab/model", {}, None, ["lab/model"], "lab/model", id="wire-id-is-a-canonical-id"
        ),
        pytest.param(
            "lab/model",
            {"canonical": ""},
            None,
            ["lab/model"],
            "lab/model",
            id="empty-pointer-falls-through-to-exact-match",
        ),
        # A dead target is the validator's finding, not silently dropped here.
        pytest.param(
            "wire-id", {"canonical": "lab/dead"}, None, [], "lab/dead", id="dead-pointer-is-kept"
        ),
        pytest.param("opaque-wire-id", {}, None, ["other/model"], None, id="no-join"),
    ],
)
def test_canonical_join_is_deterministic(
    wire_id: str,
    provider_model: dict[str, Any],
    override_model: dict[str, Any] | None,
    canonical_ids: list[str],
    expected: str | None,
) -> None:
    canonical_layer: dict[str, Any] = {canonical_id: {} for canonical_id in canonical_ids}

    assert resolve_canonical_id(wire_id, provider_model, override_model, canonical_layer) == (
        expected
    )


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        pytest.param(
            [
                {"name": "canonical", "context_window": 1000, "family": "base"},
                {"name": "provider", "context_window": 2000},
                {"name": "override"},
            ],
            {"name": "override", "context_window": 2000, "family": "base"},
            id="highest-layer-wins-per-field",
        ),
        # A higher null means "unknown": it fills an absent field but never erases a value.
        pytest.param(
            [
                {"context_window": 512000, "max_output_tokens": 128000},
                {"context_window": None, "max_output_tokens": None, "name": "provider"},
                {"family": None},
            ],
            {
                "context_window": 512000,
                "max_output_tokens": 128000,
                "name": "provider",
                "family": None,
            },
            id="null-fills-but-never-erases",
        ),
        # The reasoning control description is one unit: a higher block replaces
        # the lower ladder whole, while the independent ``mandatory`` fact survives.
        pytest.param(
            [
                {
                    "capabilities": {
                        "reasoning": {
                            "supported": True,
                            "control": "levels",
                            "levels": ["high"],
                            "mandatory": True,
                        },
                        "vision": False,
                        "tools": True,
                        "input_modalities": ["text", "image"],
                        "supported_voices": [],
                    }
                },
                {
                    "capabilities": {
                        "reasoning": {"supported": True, "control": "on_off"},
                        "vision": None,
                        "tools": False,
                        "input_modalities": ["text"],
                        "supported_voices": None,
                        "json_mode": None,
                    }
                },
            ],
            {
                "capabilities": {
                    "reasoning": {"supported": True, "control": "on_off", "mandatory": True},
                    "vision": False,
                    "tools": False,
                    "input_modalities": ["text"],
                    "supported_voices": [],
                    "json_mode": None,
                }
            },
            id="capabilities-merge-one-level-deep-with-the-reasoning-control-as-one-unit",
        ),
        # A provider block with only independent facts keeps the canonical ladder.
        pytest.param(
            [
                {
                    "capabilities": {
                        "reasoning": {"supported": True, "control": "levels", "levels": ["high"]}
                    }
                },
                {"capabilities": {"reasoning": {"mandatory": True}}},
            ],
            {
                "capabilities": {
                    "reasoning": {
                        "supported": True,
                        "control": "levels",
                        "levels": ["high"],
                        "mandatory": True,
                    }
                }
            },
            id="reasoning-facts-merge-under-an-inherited-ladder",
        ),
        pytest.param(
            [
                {
                    "metadata": {
                        "acme": {"protocol": "responses", "routes": ["a"], "remote": True},
                        "other": {"flag": True},
                        "note": "generated",
                    }
                },
                {
                    "metadata": {
                        "acme": {"routes": ["b"], "remote": None, "extra": 1},
                        "note": {"text": "hand"},
                    }
                },
            ],
            {
                "metadata": {
                    "acme": {"protocol": "responses", "routes": ["b"], "remote": True, "extra": 1},
                    "other": {"flag": True},
                    "note": {"text": "hand"},
                }
            },
            id="metadata-merges-per-provider-key-then-per-field",
        ),
    ],
)
def test_layers_merge_field_by_field(
    layers: list[Mapping[str, Any]], expected: dict[str, Any]
) -> None:
    untouched = copy.deepcopy(layers)

    assert merge_layers(layers) == expected
    assert layers == untouched


def test_canonical_layer_applies_its_overrides(tmp_path: Path) -> None:
    base_file = tmp_path / "models.json"
    overrides_file = tmp_path / "models.overrides.json"

    assert load_canonical_layer(base_file, overrides_file) == {}
    assert load_canonical_layer(None, None) == {}

    base_file.write_text(
        json.dumps({"models": {"lab/x": {"name": "Base", "family": "base"}}}), encoding="utf-8"
    )
    overrides_file.write_text(
        json.dumps({"models": {"lab/x": {"name": "Corrected"}, "lab/y": {"name": "Y"}}}),
        encoding="utf-8",
    )

    assert load_canonical_layer(base_file, overrides_file) == {
        "lab/x": {"name": "Corrected", "family": "base"},
        "lab/y": {"name": "Y"},
    }


@pytest.mark.parametrize(
    ("wire_id", "provider_model", "canonical_layer", "expected"),
    [
        pytest.param(
            "deepseek-v4-pro",
            {"name": "P", "canonical": "deepseek/deepseek-v4-pro"},
            {"deepseek/deepseek-v4-pro": {"name": "Canon", "family": "deepseek-v4"}},
            {"name": "P", "family": "deepseek-v4"},
            id="join-inherits-and-strips-the-pointer",
        ),
        pytest.param(
            "opaque",
            {"name": "Provider Only", "context_window": 8000},
            {},
            {"name": "Provider Only", "context_window": 8000},
            id="no-join-uses-provider-data-only",
        ),
    ],
)
def test_provider_model_assembles_from_its_layers(
    wire_id: str,
    provider_model: dict[str, Any],
    canonical_layer: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    assert assemble_provider_model(wire_id, provider_model, None, canonical_layer) == expected


def test_worked_example_loads_one_canonical_model_with_per_provider_ladders() -> None:
    """Canonical ladder ``[high, max]``; OpenRouter deviates to ``[high, xhigh]``
    through the exact-id join; opencode-go omits reasoning behind a pointer."""

    registry = ModelRegistry.load(ASSEMBLY_FIXTURES)
    openrouter = registry.get("openrouter", "deepseek/deepseek-v4-pro")
    opencode_go = registry.get("opencode-go", "deepseek-v4-pro")
    standalone = registry.get("openrouter", "vendor-x/standalone-model")
    hand_corrected = registry.get("mistral", "thin-deepseek")

    # The provider name wins; family comes from the canonical record.
    assert (openrouter.name, openrouter.family) == ("DeepSeek V4 Pro (OpenRouter)", "deepseek-v4")
    assert openrouter.capabilities.reasoning.levels == ("high", "xhigh")
    # The wire id stays the Provider's; the canonical id never lands on the Model.
    assert (opencode_go.model_id, opencode_go.family) == ("deepseek-v4-pro", "deepseek-v4")
    assert opencode_go.capabilities.reasoning.control == "levels"
    assert opencode_go.capabilities.reasoning.levels == ("high", "max")
    # A missed join is not an error: the Model loads on Provider data alone.
    assert (standalone.name, standalone.family, standalone.context_window) == (
        "Standalone Model",
        "",
        64000,
    )
    assert standalone.capabilities.reasoning.supported is False
    # The override (no provider_id, manual pointer) wins, and its ladder replaces
    # both the provider ladder [low, medium] and the canonical [high, max] as one unit.
    assert (hand_corrected.name, hand_corrected.family) == (
        "Thin DeepSeek (hand-corrected)",
        "deepseek-v4",
    )
    assert hand_corrected.capabilities.reasoning.levels == ("medium", "high", "max")
    assert (
        hand_corrected.context_window,
        hand_corrected.max_output_tokens,
        hand_corrected.capabilities.tools,
    ) == (128000, 16000, True)


def test_empty_provider_record_inherits_the_complete_canonical_model(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "thin.json").write_text(
        json.dumps({"provider_id": "thin", "models": {"lab/model": {}}}), encoding="utf-8"
    )
    canonical = {
        "name": "Canonical",
        "capabilities": {
            "vision": False,
            "tools": True,
            "json_mode": False,
            "reasoning": {"supported": False},
        },
    }
    (models_dir / "models.json").write_text(
        json.dumps({"models": {"lab/model": canonical}}), encoding="utf-8"
    )

    assert ModelRegistry.load(tmp_path).get("thin", "lab/model").name == "Canonical"
