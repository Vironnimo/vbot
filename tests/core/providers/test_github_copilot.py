"""Tests for GitHubCopilotAdapter."""

from __future__ import annotations

import json
from pathlib import Path

from core.providers.github_copilot import GitHubCopilotAdapter

FIXTURE_PATH = Path("tests/core/models/fixtures/github_copilot_models_raw.json")


def _raw_copilot_models() -> dict[str, dict]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"]
    return {entry["id"]: entry for entry in data}


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


def test_thinking_budget_marks_gemini_model_as_reasoning_capable() -> None:
    raw_models = _raw_copilot_models()

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_models["gemini-2.5-pro"], {})

    assert model.capabilities.reasoning.supported is True


def test_supported_flags_map_to_capabilities_from_captured_schema() -> None:
    raw_models = _raw_copilot_models()

    model = GitHubCopilotAdapter.normalize_catalog_entry(raw_models["gpt-4o"], {})

    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is False
    assert model.capabilities.reasoning.supported is False
