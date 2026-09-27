"""GitHub Copilot catalog normalization: capabilities, limits, runtime metadata, skips."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.models.models import Model
from core.providers.errors import CatalogEntrySkipped
from core.providers.github_copilot import GitHubCopilotAdapter
from tests.core.providers.github_copilot_test_support import raw_copilot_models


def _capability_facts(model: Model) -> dict[str, Any]:
    reasoning = model.capabilities.reasoning
    return {
        "vision": model.capabilities.vision,
        "tools": model.capabilities.tools,
        "json_mode": model.capabilities.json_mode,
        "reasoning": (reasoning.supported, reasoning.control, reasoning.levels),
        "budget_max": reasoning.budget_max,
    }


@pytest.mark.parametrize(
    ("model_id", "expected_facts", "expected_metadata"),
    [
        pytest.param(
            "gpt-4o",
            {
                "vision": True,
                "tools": True,
                "json_mode": False,
                "reasoning": (False, None, ()),
                "budget_max": None,
            },
            {"tool_calls": True},
            id="vision-and-tools",
        ),
        pytest.param(
            "gpt-5-mini",
            {
                "vision": True,
                "tools": True,
                "json_mode": True,
                "reasoning": (True, "levels", ("low", "medium", "high")),
                "budget_max": None,
            },
            {
                "reasoning_efforts": ("low", "medium", "high"),
                "supported_endpoints": ("/chat/completions", "/responses", "ws:/responses"),
            },
            id="effort-ladder-and-endpoints",
        ),
        pytest.param(
            "gemini-2.5-pro",
            {
                "vision": True,
                "tools": True,
                "json_mode": False,
                "reasoning": (True, "budget", ()),
                "budget_max": 32768,
            },
            {"min_thinking_budget": 128, "max_thinking_budget": 32768},
            id="thinking-budget",
        ),
    ],
)
def test_fixture_entries_map_capabilities_limits_and_runtime_metadata(
    model_id: str, expected_facts: dict[str, Any], expected_metadata: dict[str, Any]
) -> None:
    raw_model = raw_copilot_models()[model_id]

    # Provider defaults never replace the catalog's own limits.
    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

    limits = raw_model["capabilities"]["limits"]
    assert model.model_id == model_id
    assert model.name == raw_model["name"]
    assert _capability_facts(model) == expected_facts
    assert model.context_window == limits["max_context_window_tokens"]
    assert model.max_output_tokens == limits["max_output_tokens"]
    copilot_metadata = model.metadata["github_copilot"]
    assert {key: copilot_metadata[key] for key in expected_metadata} == expected_metadata
    assert "policy" not in copilot_metadata
    assert "model_picker_enabled" not in copilot_metadata


@pytest.mark.parametrize(
    ("capabilities", "expected_limits"),
    [
        pytest.param({"limits": {"max_output_tokens": 2048}}, (None, 2048), id="partial"),
        pytest.param(
            {"limits": {"max_context_window_tokens": None, "max_output_tokens": None}},
            (None, None),
            id="null-values",
        ),
        pytest.param({"limits": None}, (None, None), id="non-object-limits"),
    ],
)
def test_missing_or_invalid_limits_are_unknown_without_dropping_the_model(
    capabilities: dict[str, Any], expected_limits: tuple[int | None, int | None]
) -> None:
    raw_model = {
        "id": "partial-copilot-model",
        "name": "Partial Copilot Model",
        "capabilities": {**capabilities, "supports": {"tool_calls": True}},
    }

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

    assert model.model_id == "partial-copilot-model"
    assert (model.context_window, model.max_output_tokens) == expected_limits


def test_non_object_supports_disable_every_capability() -> None:
    raw_model = {
        "id": "supports-model",
        "name": "Supports Model",
        "capabilities": {
            "limits": {"max_context_window_tokens": 128000, "max_output_tokens": 4096},
            "supports": "invalid",
        },
    }

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {})

    assert _capability_facts(model) == {
        "vision": False,
        "tools": False,
        "json_mode": False,
        "reasoning": (False, None, ()),
        "budget_max": None,
    }


def test_non_object_capabilities_are_rejected() -> None:
    raw_model = {"id": "invalid-copilot-model", "name": "Invalid", "capabilities": None}

    with pytest.raises(ValueError, match="capabilities"):
        GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {})


@pytest.mark.parametrize(
    "raw_model",
    [
        pytest.param(
            {
                "id": "hidden-utility",
                "model_picker_enabled": False,
                "capabilities": {"type": "chat"},
            },
            id="hidden",
        ),
        pytest.param(
            {"id": "embedding-only", "capabilities": {"type": "embeddings"}}, id="not-chat"
        ),
        pytest.param(
            {
                "id": "websocket-only",
                "supported_endpoints": ["ws:/responses"],
                "capabilities": {"type": "chat"},
            },
            id="no-supported-endpoint",
        ),
    ],
)
def test_non_selectable_catalog_entries_are_skipped(raw_model: dict[str, Any]) -> None:
    with pytest.raises(CatalogEntrySkipped):
        GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {})


def test_catalog_preserves_prompt_and_image_limits_as_runtime_policy() -> None:
    raw_model = {
        "id": "vision-model",
        "name": "Vision Model",
        "model_picker_enabled": True,
        "supported_endpoints": ["/responses"],
        "capabilities": {
            "type": "chat",
            "limits": {
                "max_context_window_tokens": 128000,
                "max_prompt_tokens": 96000,
                "max_output_tokens": 32000,
                "vision": {
                    "max_prompt_image_size": 3145728,
                    "max_prompt_images": 5,
                    "supported_media_types": ["image/jpeg", "image/png"],
                },
            },
            "supports": {"vision": True},
        },
    }

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {})

    assert model.metadata["github_copilot"]["max_prompt_tokens"] == 96000
    assert model.metadata["github_copilot"]["vision"] == {
        "max_prompt_image_size": 3145728,
        "max_prompt_images": 5,
        "supported_media_types": ("image/jpeg", "image/png"),
    }


def test_bundled_copilot_catalog_omits_hidden_and_retired_entries() -> None:
    raw = json.loads(Path("resources/models/github-copilot.raw.json").read_text(encoding="utf-8"))
    generated = json.loads(Path("resources/models/github-copilot.json").read_text(encoding="utf-8"))
    provider = json.loads(
        Path("resources/providers/github-copilot.json").read_text(encoding="utf-8")
    )
    hidden_ids = {
        entry["id"]
        for entry in raw["raw_response"]["data"]
        if entry.get("model_picker_enabled") is False
    }
    generated_ids = set(generated["models"])

    assert generated_ids.isdisjoint(hidden_ids)
    assert generated_ids.isdisjoint(provider["catalog_exclusions"])
