"""Tests for GitHubCopilotAdapter behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.providers.errors import CatalogEntrySkipped
from core.providers.github_copilot import (
    GitHubCopilotAdapter,
)
from tests.core.providers.github_copilot_test_support import (
    _raw_copilot_models,
)


def test_gpt_4o_reads_vision_context_and_max_output_from_copilot_capabilities() -> None:
    raw_models = _raw_copilot_models()
    raw_model = raw_models["gpt-4o"]

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

    assert model.model_id == "gpt-4o"
    assert model.name == "GPT-4o"
    assert model.capabilities.vision is True
    assert model.context_window == raw_model["capabilities"]["limits"]["max_context_window_tokens"]
    assert model.max_output_tokens == raw_model["capabilities"]["limits"]["max_output_tokens"]
    assert model.max_output_tokens == 4096


def test_reasoning_effort_list_marks_o_series_model_as_reasoning_capable() -> None:
    raw_models = _raw_copilot_models()

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_models["gpt-5-mini"], {})

    assert model.capabilities.reasoning.supported is True
    assert model.capabilities.reasoning.control == "levels"
    assert model.capabilities.reasoning.levels == ("low", "medium", "high")
    assert model.metadata["github_copilot"]["reasoning_efforts"] == ("low", "medium", "high")
    assert model.metadata["github_copilot"]["supported_endpoints"] == (
        "/chat/completions",
        "/responses",
        "ws:/responses",
    )


def test_thinking_budget_marks_gemini_model_as_reasoning_capable() -> None:
    raw_models = _raw_copilot_models()

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_models["gemini-2.5-pro"], {})

    assert model.capabilities.reasoning.supported is True
    assert model.metadata["github_copilot"]["min_thinking_budget"] == 128
    assert model.metadata["github_copilot"]["max_thinking_budget"] == 32768


def test_supported_flags_map_to_capabilities_from_captured_schema() -> None:
    raw_models = _raw_copilot_models()

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_models["gpt-4o"], {})

    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is False
    assert model.capabilities.reasoning.supported is False
    assert "policy" not in model.metadata.get("github_copilot", {})
    assert "model_picker_enabled" not in model.metadata.get("github_copilot", {})


def test_missing_optional_copilot_limits_fall_back_without_dropping_model() -> None:
    raw_model = {
        "id": "partial-copilot-model",
        "name": "Partial Copilot Model",
        "capabilities": {
            "limits": {
                "max_output_tokens": 2048,
            },
            "supports": {
                "tool_calls": True,
            },
        },
    }

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

    assert model.model_id == "partial-copilot-model"
    # Absent context window → honest None, not a fake 0 (Phase 6).
    assert model.context_window is None
    assert model.max_output_tokens == 2048


def test_non_integer_optional_copilot_output_limit_is_unknown() -> None:
    raw_model = {
        "id": "partial-copilot-model",
        "name": "Partial Copilot Model",
        "capabilities": {
            "limits": {
                "max_context_window_tokens": None,
                "max_output_tokens": None,
            },
            "supports": {},
        },
    }

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

    assert model.context_window is None
    assert model.max_output_tokens is None


def test_missing_or_non_object_copilot_output_limits_are_unknown() -> None:
    raw_model_with_missing_limits = {
        "id": "missing-limits-model",
        "name": "Missing Limits Model",
        "capabilities": {
            "supports": {
                "tool_calls": True,
            },
        },
    }
    raw_model_with_null_limits = {
        "id": "null-limits-model",
        "name": "Null Limits Model",
        "capabilities": {
            "limits": None,
            "supports": {
                "tool_calls": True,
            },
        },
    }

    missing_limits_model = GitHubCopilotAdapter.normalize_catalog_entry(
        raw_model_with_missing_limits,
        {"max_tokens": 8192},
    )
    null_limits_model = GitHubCopilotAdapter.normalize_catalog_entry(
        raw_model_with_null_limits,
        {"max_tokens": 8192},
    )

    assert missing_limits_model.context_window is None
    assert missing_limits_model.max_output_tokens is None
    assert null_limits_model.context_window is None
    assert null_limits_model.max_output_tokens is None


def test_missing_or_non_object_copilot_supports_use_empty_mapping() -> None:
    raw_model_with_missing_supports = {
        "id": "missing-supports-model",
        "name": "Missing Supports Model",
        "capabilities": {
            "limits": {
                "max_context_window_tokens": 128000,
                "max_output_tokens": 4096,
            },
        },
    }
    raw_model_with_string_supports = {
        "id": "string-supports-model",
        "name": "String Supports Model",
        "capabilities": {
            "limits": {
                "max_context_window_tokens": 128000,
                "max_output_tokens": 4096,
            },
            "supports": "invalid",
        },
    }

    missing_supports_model = GitHubCopilotAdapter.normalize_catalog_entry(
        raw_model_with_missing_supports,
        {},
    )
    string_supports_model = GitHubCopilotAdapter.normalize_catalog_entry(
        raw_model_with_string_supports,
        {},
    )

    assert missing_supports_model.capabilities.vision is False
    assert missing_supports_model.capabilities.tools is False
    assert missing_supports_model.capabilities.json_mode is False
    assert missing_supports_model.capabilities.reasoning.supported is False
    assert string_supports_model.capabilities.vision is False
    assert string_supports_model.capabilities.tools is False
    assert string_supports_model.capabilities.json_mode is False
    assert string_supports_model.capabilities.reasoning.supported is False


def test_invalid_copilot_capabilities_shape_still_fails() -> None:
    raw_model = {
        "id": "invalid-copilot-model",
        "name": "Invalid Copilot Model",
        "capabilities": None,
    }

    try:
        GitHubCopilotAdapter.normalize_catalog_entry(raw_model, {})
    except ValueError as exc:
        assert str(exc) == "Expected 'capabilities' to be an object"
    else:
        raise AssertionError("Expected invalid capabilities shape to fail")


@pytest.mark.parametrize(
    "raw_model",
    [
        {
            "id": "hidden-utility",
            "model_picker_enabled": False,
            "capabilities": {"type": "chat"},
        },
        {
            "id": "embedding-only",
            "capabilities": {"type": "embeddings"},
        },
        {
            "id": "websocket-only",
            "supported_endpoints": ["ws:/responses"],
            "capabilities": {"type": "chat"},
        },
    ],
)
def test_non_selectable_copilot_catalog_entries_are_skipped(raw_model: dict) -> None:
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
