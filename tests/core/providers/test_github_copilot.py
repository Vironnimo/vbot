"""Tests for GitHubCopilotAdapter."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.providers.github_copilot import (
    OPENAI_REASONING_COPILOT_MODEL_POLICY,
    GitHubCopilotAdapter,
    _copilot_model_policy,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

FIXTURE_PATH = Path("tests/core/models/fixtures/github_copilot_models_raw.json")
API_KEY = "test-api-key-12345"
COPILOT_CONFIG = ProviderConfig(
    id="github-copilot",
    name="GitHub Copilot",
    adapter="github_copilot",
    base_url="https://api.githubcopilot.com",
    connections=[
        ConnectionConfig(
            id="oauth",
            type="oauth",
            label="Sign in with GitHub",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="",
            ),
        )
    ],
    defaults={"max_tokens": 4096},
)
COPILOT_URL = "https://api.githubcopilot.com/chat/completions"
SUCCESS_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
}
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]


def _raw_copilot_models() -> dict[str, dict]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"]
    return {entry["id"]: entry for entry in data}


@pytest.fixture()
def copilot_adapter() -> GitHubCopilotAdapter:
    return GitHubCopilotAdapter(COPILOT_CONFIG, API_KEY)


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
    assert model.context_window == 0
    assert model.max_output_tokens == 2048


def test_non_integer_optional_copilot_limits_use_provider_defaults() -> None:
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

    assert model.context_window == 0
    assert model.max_output_tokens == 8192


def test_missing_or_non_object_copilot_limits_use_defaults() -> None:
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

    assert missing_limits_model.context_window == 0
    assert missing_limits_model.max_output_tokens == 8192
    assert null_limits_model.context_window == 0
    assert null_limits_model.max_output_tokens == 8192


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


def test_unknown_copilot_model_policy_defaults_to_safe_reasoning_behavior() -> None:
    policy = _copilot_model_policy("claude-haiku-4.5")

    assert policy.allows_openai_reasoning_effort("high") is False
    assert policy.endpoint_path == "/chat/completions"
    assert policy.supports_tools is False


def test_gpt_5_mini_copilot_policy_allows_openai_reasoning_efforts() -> None:
    policy = _copilot_model_policy("gpt-5-mini")

    assert policy == OPENAI_REASONING_COPILOT_MODEL_POLICY
    assert policy.allows_openai_reasoning_effort("high") is True
    assert policy.allows_openai_reasoning_effort("xhigh") is False


@respx.mock
@pytest.mark.asyncio
async def test_send_omits_reasoning_effort_for_safe_default_copilot_model(
    copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "claude-haiku-4.5"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_send_preserves_reasoning_effort_for_allowed_copilot_model(
    copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-mini",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"
