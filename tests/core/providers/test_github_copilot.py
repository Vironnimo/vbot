"""Tests for GitHubCopilotAdapter."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.chat.chat import _assistant_message_from_response
from core.chat.streaming import StreamingAccumulator
from core.models.models import Model
from core.providers.errors import NetworkError, ProviderError, ProviderTimeoutError
from core.providers.github_copilot import (
    GitHubCopilotAdapter,
)
from core.providers.github_copilot_policy import CHAT_COMPLETIONS_ENDPOINT
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
RESPONSES_URL = "https://api.githubcopilot.com/responses"
MESSAGES_URL = "https://api.githubcopilot.com/v1/messages"
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
SYNTHETIC_COPILOT_METADATA_BY_MODEL_ID = {
    "claude-haiku-4.5": {
        "github_copilot": {
            "vendor": "Anthropic",
            "family": "claude-haiku-4.5",
            "version": "claude-haiku-4.5",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/v1/messages"],
            "adaptive_thinking": True,
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gemini-3.1-pro-preview": {
        "github_copilot": {
            "vendor": "Google",
            "family": "gemini-3.1-pro-preview",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT],
            "tool_calls": True,
            "streaming": True,
        }
    },
    "gpt-5.4": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "version": "gpt-5.4",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/responses", "ws:/responses"],
            "reasoning_efforts": ["low", "medium", "high"],
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gpt-5.4-mini": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4-mini",
            "version": "gpt-5.4-mini",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/responses", "ws:/responses"],
            "reasoning_efforts": ["low", "medium", "high"],
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gpt-5.4-partial": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "version": "gpt-5.4",
            "supported_endpoints": ["/responses"],
            "streaming": True,
            "tool_calls": True,
        }
    },
}


def _raw_copilot_models() -> dict[str, dict]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"]
    return {entry["id"]: entry for entry in data}


def _copilot_model(model_id: str) -> Model:
    raw_models = _raw_copilot_models()
    return GitHubCopilotAdapter.normalize_catalog_entry(raw_models[model_id], {})


def _copilot_model_with_metadata(model_id: str, metadata: dict) -> Model:
    base_model = GitHubCopilotAdapter.normalize_catalog_entry(
        {
            "id": model_id,
            "name": model_id,
            "capabilities": {"supports": {}},
        },
        {},
    )
    return replace(base_model, metadata=metadata)


def _copilot_metadata_lookup(model_id: str) -> Model | None:
    synthetic_metadata = SYNTHETIC_COPILOT_METADATA_BY_MODEL_ID.get(model_id)
    if synthetic_metadata is not None:
        return _copilot_model_with_metadata(model_id, synthetic_metadata)
    raw_models = _raw_copilot_models()
    if model_id not in raw_models:
        return None
    return _copilot_model(model_id)


class _BrokenLineIterator:
    def __init__(self, first_line: str, error: Exception) -> None:
        self._first_line = first_line
        self._error = error
        self._emitted_first_line = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._emitted_first_line:
            self._emitted_first_line = True
            return self._first_line
        raise self._error


class _BrokenStreamResponse:
    status_code = 200

    def __init__(self, first_line: str, error: Exception) -> None:
        self._iterator = _BrokenLineIterator(first_line, error)
        self.closed = False

    def aiter_lines(self):
        return self._iterator

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture()
def copilot_adapter() -> GitHubCopilotAdapter:
    return GitHubCopilotAdapter(COPILOT_CONFIG, API_KEY)


@pytest.fixture()
def metadata_copilot_adapter() -> GitHubCopilotAdapter:
    return GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=_copilot_metadata_lookup,
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


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_mini_to_responses_from_metadata(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-1",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5-mini",
        thinking_effort="high",
        response_format={"type": "json_object"},
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "gpt-5-mini"
    assert request_body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert request_body["max_output_tokens"] == 4096
    assert request_body["text"] == {"format": {"type": "json_object"}}
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Hi",
        "reasoning": None,
        "reasoning_meta": {"response_id": "resp-1"},
        "tool_calls": None,
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_claude_to_messages_from_metadata(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Claude reply"}],
                "usage": {"input_tokens": 5, "output_tokens": 6},
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-sonnet-4.6",
        thinking_effort="high",
        response_format={"type": "json_object"},
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "claude-sonnet-4.6"
    assert request_body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request_body["output_config"] == {"effort": "high"}
    assert "response_format" not in request_body
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Claude reply",
        "reasoning": None,
        "reasoning_meta": None,
        "tool_calls": None,
        "usage": {"input_tokens": 5, "output_tokens": 6},
    }


@pytest.mark.parametrize("model_id", ["claude-sonnet-4.6", "claude-haiku-4.5"])
@respx.mock
@pytest.mark.asyncio
async def test_messages_models_send_exact_on_wire_payload(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id=model_id,
        thinking_effort="high",
        response_format={"type": "json_object"},
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": model_id,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        **({"output_config": {"effort": "high"}} if model_id == "claude-sonnet-4.6" else {}),
        "max_tokens": 4096,
        **({} if model_id == "claude-sonnet-4.6" else {"temperature": 0.25}),
    }


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_responses_models_send_exact_on_wire_payload_without_temperature(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id=model_id,
        thinking_effort="high",
        response_format={"type": "json_object"},
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 4096,
    }


@respx.mock
@pytest.mark.asyncio
async def test_partial_openai_like_metadata_still_omits_temperature_on_responses(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gpt-5.4-partial",
        temperature=0.25,
        top_p=0.9,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "gpt-5.4-partial",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
        "max_output_tokens": 4096,
        "top_p": 0.9,
    }


@respx.mock
@pytest.mark.asyncio
async def test_messages_alias_override_wins_over_provider_default_on_wire(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        max_output_tokens=2048,
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 2048,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_messages_max_completion_tokens_alias_maps_to_max_tokens_on_wire(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        max_completion_tokens=1024,
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "max_tokens": 1024,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_gemini_3_1_preview_stays_chat_when_metadata_advertises_only_chat(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gemini-3.1-pro-preview",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "gemini-3.1-pro-preview"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_gemini_2_5_pro_without_endpoint_metadata_stays_conservative_chat(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="gemini-2.5-pro",
        thinking_budget=4096,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "gemini-2.5-pro"
    assert "thinking_budget" not in request_body
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_unknown_model_uses_chat_fallback_and_omits_optional_controls(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(COPILOT_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="unknown-copilot-model",
        thinking_effort="high",
        tools=[{"name": "search", "description": "Search", "parameters": {"type": "object"}}],
        response_format={"type": "json_object"},
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["model"] == "unknown-copilot-model"
    assert "reasoning_effort" not in request_body
    assert "tools" not in request_body
    assert "response_format" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_static_fallback_applies_only_when_metadata_missing() -> None:
    fallback_adapter = GitHubCopilotAdapter(COPILOT_CONFIG, API_KEY)
    metadata_adapter = GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=lambda model_id: _copilot_model(model_id),
    )
    chat_route = respx.post(COPILOT_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )
    responses_route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(200, json={"output": []})
    )

    await fallback_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-mini", thinking_effort="high")
    await metadata_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-mini", thinking_effort="high")

    chat_body = json.loads(chat_route.calls.last.request.content)
    responses_body = json.loads(responses_route.calls.last.request.content)
    assert chat_body["reasoning_effort"] == "high"
    assert responses_body["reasoning"] == {"effort": "high", "summary": "auto"}


@respx.mock
@pytest.mark.asyncio
async def test_headers_include_auth_and_extra_headers_for_all_endpoint_families() -> None:
    custom_config = ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
        base_url="https://api.githubcopilot.com",
        connections=COPILOT_CONFIG.connections,
        defaults={"max_tokens": 4096},
        extra_headers={"Editor-Version": "vBot/test"},
    )
    adapter = GitHubCopilotAdapter(
        custom_config,
        API_KEY,
        model_lookup=_copilot_metadata_lookup,
    )
    chat_route = respx.post(COPILOT_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
    )
    responses_route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(200, json={"output": []})
    )
    messages_route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"content": []})
    )

    await adapter.send(SAMPLE_MESSAGES, model_id="unknown-copilot-model")
    await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5-mini")
    await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4.6")

    for route in (chat_route, responses_route, messages_route):
        headers = route.calls.last.request.headers
        assert headers["Authorization"] == f"Bearer {API_KEY}"
        assert headers["Editor-Version"] == "vBot/test"


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_yields_normalized_deltas(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Hi"},
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_stream_responses_raises_network_error_on_mid_stream_read_error(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        "event: response.output_text.delta",
        httpx.ReadError("connection reset"),
    )

    with (
        patch.object(
            metadata_copilot_adapter,
            "_connect_stream",
            new=AsyncMock(return_value=broken_response),
        ),
        pytest.raises(NetworkError, match="Stream read failed: connection reset"),
    ):
        async for _ in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_raises_network_error_on_eof_without_completion(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Partial"}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    with pytest.raises(NetworkError, match="response completion event"):
        async for _ in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass


@pytest.mark.asyncio
async def test_stream_responses_raises_provider_timeout_error_on_mid_stream_timeout(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        "event: response.output_text.delta",
        httpx.TimeoutException("timed out"),
    )

    with (
        patch.object(
            metadata_copilot_adapter,
            "_connect_stream",
            new=AsyncMock(return_value=broken_response),
        ),
        pytest.raises(ProviderTimeoutError, match="timed out"),
    ):
        async for _ in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_preserves_tool_call_id_across_sse_events(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","call_id":"call_stable","name":"search"}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"delta":"{\\"q\\""}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"delta":":\\"docs\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed",'
        '"output":[{"type":"function_call","call_id":"call_stable"}]}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    tool_call_chunks = [chunk for chunk in chunks if chunk["type"] == "tool_call_delta"]
    assert tool_call_chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "",
            "arguments_delta": '{"q"',
        },
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "",
            "arguments_delta": ':"docs"}',
        },
    ]
    assert chunks[-1] == {"type": "finish", "reason": "tool_calls"}


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_does_not_duplicate_reasoning_from_completed_event(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.reasoning_summary_text.delta\n"
        'data: {"type":"response.reasoning_summary_text.delta","delta":"Need docs lookup."}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed",'
        '"output":[{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text",'
        '"text":"Need docs lookup."}],"encrypted_content":"opaque"}]}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    assert chunks == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp-1",
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    }
                ],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_4_responses_with_nested_tool_name_and_visible_reasoning(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-1",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    },
                ],
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        [
            {"role": "user", "content": "Look up docs"},
        ],
        model_id="gpt-5.4",
        thinking_effort="high",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                },
            }
        ],
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "gpt-5.4",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Look up docs"}]}],
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 4096,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": None,
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {
            "response_id": "resp-1",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                    "encrypted_content": "opaque",
                }
            ],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
    }


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_4_family_responses_with_nested_tool_name(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-1",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    },
                ],
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        [{"role": "user", "content": "Look up docs"}],
        model_id=model_id,
        thinking_effort="high",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                },
            }
        ],
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Look up docs"}]}],
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search docs",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 4096,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": None,
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {
            "response_id": "resp-1",
            "reasoning_items": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                    "encrypted_content": "opaque",
                }
            ],
            "encrypted_content": ["opaque"],
        },
        "tool_calls": [{"id": "call_1", "name": "search", "arguments": {"q": "docs"}}],
    }


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_routes_gpt_5_4_family_responses_with_blank_top_level_tool_name_and_arguments(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [{"role": "user", "content": "Look up docs"}],
        model_id=model_id,
        tools=[
            {
                "type": "function",
                "name": "",
                "function": {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                },
            }
        ],
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["tools"] == [
        {
            "type": "function",
            "name": "search",
            "description": "Search docs",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
    ]


@respx.mock
@pytest.mark.asyncio
async def test_send_replays_nested_tool_call_name_shape_for_gpt_5_4_responses(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search",
                "content": "result",
            },
        ],
        model_id="gpt-5.4",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_replays_nested_tool_call_arguments_when_top_level_values_are_blank(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "",
                        "arguments": "",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search",
                "content": "result",
            },
        ],
        model_id=model_id,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_send_replays_nested_tool_call_name_shape_for_gpt_5_4_family(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    route = respx.post(RESPONSES_URL).mock(return_value=httpx.Response(200, json={"output": []}))

    await metadata_copilot_adapter.send(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"docs"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search",
                "content": "result",
            },
        ],
        model_id=model_id,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["input"] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"docs"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_stream_gpt_5_4_family_responses_surfaces_nested_tool_name(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","call_id":"call_stable",'
        '"function":{"name":"search"}}}\n\n'
        "event: response.reasoning_summary_text.delta\n"
        'data: {"type":"response.reasoning_summary_text.delta","delta":"Need docs lookup."}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed",'
        '"output":[{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text",'
        '"text":"Need docs lookup."}],"encrypted_content":"opaque"}],'
        '"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id=model_id):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "response_id": "resp-1",
                "reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [{"type": "summary_text", "text": "Need docs lookup."}],
                        "encrypted_content": "opaque",
                    }
                ],
                "encrypted_content": ["opaque"],
            },
        },
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "finish", "reason": "tool_calls"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_stream_gpt_5_4_family_responses_deduplicates_replayed_arguments(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","id":"fc_1","call_id":"call_1",'
        '"name":"","arguments":"","function":{"name":"search","arguments":"{\\"q\\":\\"docs\\"}"}}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"item_id":"fc_1","call_id":"call_1","delta":"{\\"q\\":\\"docs\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id=model_id):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_1",
            "name_delta": "search",
            "arguments_delta": '{"q":"docs"}',
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@pytest.mark.parametrize("model_id", ["gpt-5.4", "gpt-5.4-mini"])
@respx.mock
@pytest.mark.asyncio
async def test_stream_gpt_5_4_family_item_id_only_delta_parses_into_valid_chat_tool_call(
    metadata_copilot_adapter: GitHubCopilotAdapter,
    model_id: str,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","id":"fc_1","call_id":"call_1",'
        '"name":"tool","arguments":"","function":{"name":"bash"}}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1",'
        '"delta":"{\\"command\\":\\"pwd\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    accumulator = StreamingAccumulator()
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id=model_id):
        accumulator.add_delta(chunk)

    assistant = _assistant_message_from_response(
        f"github-copilot/{model_id}",
        accumulator.finalize_assistant_fields().to_response_dict(),
    )

    assert assistant.tool_calls is not None
    assert [tool_call.to_dict() for tool_call in assistant.tool_calls] == [
        {"id": "call_1", "name": "bash", "arguments": {"command": "pwd"}}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_responses_backfills_only_missing_tool_argument_suffix(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        "event: response.output_item.added\n"
        'data: {"type":"response.output_item.added","output_index":0,'
        '"item":{"type":"function_call","call_id":"call_stable",'
        '"function":{"name":"search","arguments":"{\\"q\\""}}}\n\n'
        "event: response.function_call_arguments.delta\n"
        'data: {"type":"response.function_call_arguments.delta","output_index":0,'
        '"call_id":"call_stable","delta":"{\\"q\\":\\"docs\\"}"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    respx.post(RESPONSES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5-mini"):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "search",
            "arguments_delta": '{"q"',
        },
        {
            "type": "tool_call_delta",
            "id": "call_stable",
            "name_delta": "",
            "arguments_delta": ':"docs"}',
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_messages_visible_thinking_text_block(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                    {"type": "text", "text": "Claude reply"},
                ],
                "usage": {"input_tokens": 5, "output_tokens": 6},
            },
        )
    )

    response = await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Claude reply",
        "reasoning": "Need to inspect first.",
        "reasoning_meta": {
            "content_blocks": [
                {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"}
            ]
        },
        "tool_calls": None,
        "usage": {"input_tokens": 5, "output_tokens": 6},
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_requests_visible_thinking_controls(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                    {"type": "text", "text": "Claude reply"},
                ]
            },
        )
    )

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_omits_budget_and_output_config(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"content": []}))

    await metadata_copilot_adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        thinking_budget=2048,
        thinking={"type": "enabled", "budget_tokens": 2048},
        output_config={"effort": "high"},
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }


@respx.mock
@pytest.mark.asyncio
async def test_send_routes_haiku_visible_thinking_without_reasoning_effort_support(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"},
                    {"type": "text", "text": "Claude reply"},
                ]
            },
        )
    )

    adapter = GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=lambda model: (
            _copilot_model_with_metadata(
                model,
                {
                    "github_copilot": {
                        "vendor": "Anthropic",
                        "family": "claude-haiku-4.5",
                        "version": "claude-haiku-4.5",
                        "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/v1/messages"],
                        "reasoning_efforts": [],
                        "adaptive_thinking": True,
                        "streaming": True,
                        "tool_calls": True,
                    }
                },
            )
            if model == "claude-haiku-4.5"
            else _copilot_metadata_lookup(model)
        ),
    )

    response = await adapter.send(
        SAMPLE_MESSAGES,
        model_id="claude-haiku-4.5",
        thinking_effort="high",
        temperature=0.25,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body == {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "max_tokens": 4096,
        "temperature": 0.25,
    }
    assert metadata_copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Claude reply",
        "reasoning": "Need to inspect first.",
        "reasoning_meta": {
            "content_blocks": [
                {"type": "thinking", "text": "Need to inspect first.", "signature": "sig-1"}
            ]
        },
        "tool_calls": None,
    }


def test_normalize_response_extracts_gemini_visible_thinking_from_reasoning_details(
    copilot_adapter: GitHubCopilotAdapter,
) -> None:
    response = {
        "id": "chatcmpl-gemini-1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Gemini reply",
                    "reasoning_details": [{"type": "reasoning.text", "text": "Need docs lookup."}],
                },
                "finish_reason": "stop",
            }
        ],
    }

    assert copilot_adapter.normalize_response(response) == {
        "role": "assistant",
        "content": "Gemini reply",
        "reasoning": "Need docs lookup.",
        "reasoning_meta": {
            "reasoning_details": [{"type": "reasoning.text", "text": "Need docs lookup."}]
        },
        "tool_calls": None,
    }


@respx.mock
@pytest.mark.asyncio
async def test_stream_gemini_3_1_preview_extracts_visible_thinking_from_reasoning_details(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text",'
        '"text":"Need docs lookup."}]}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(COPILOT_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES,
        model_id="gemini-3.1-pro-preview",
        thinking_effort="high",
    ):
        chunks.append(chunk)

    assert chunks == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "reasoning_details": [{"type": "reasoning.text", "text": "Need docs lookup."}]
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_yields_normalized_deltas(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Hi"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-sonnet-4.6"
    ):
        chunks.append(chunk)

    assert chunks == [
        {"type": "content_delta", "text": "Hi"},
        {"type": "finish", "reason": "stop"},
    ]


@pytest.mark.asyncio
async def test_stream_messages_raises_network_error_on_mid_stream_read_error(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        httpx.ReadError("connection reset"),
    )

    with (
        patch.object(
            metadata_copilot_adapter,
            "_connect_stream",
            new=AsyncMock(return_value=broken_response),
        ),
        pytest.raises(NetworkError, match="Stream read failed: connection reset"),
    ):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_raises_network_error_on_eof_without_stop_reason(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Partial"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    with pytest.raises(NetworkError, match="message stop reason"):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_raises_provider_error_on_malformed_json(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: not-json\n\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    with pytest.raises(ProviderError, match="malformed JSON"):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass


@pytest.mark.asyncio
async def test_stream_messages_raises_provider_timeout_error_on_mid_stream_timeout(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    broken_response = _BrokenStreamResponse(
        '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        httpx.TimeoutException("timed out"),
    )

    with (
        patch.object(
            metadata_copilot_adapter,
            "_connect_stream",
            new=AsyncMock(return_value=broken_response),
        ),
        pytest.raises(ProviderTimeoutError, match="timed out"),
    ):
        async for _ in metadata_copilot_adapter.stream(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4.6",
        ):
            pass

    assert broken_response.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_surfaces_visible_thinking_text_block_variant(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"thinking","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Need docs lookup."}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"signature_delta","signature":"sig-stream"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-haiku-4.5"
    ):
        chunks.append(chunk)

    assert chunks == [
        {"type": "reasoning_delta", "text": "Need docs lookup."},
        {
            "type": "reasoning_meta",
            "reasoning_meta": {
                "content_blocks": [
                    {"type": "thinking", "text": "Need docs lookup.", "signature": "sig-stream"}
                ]
            },
        },
        {"type": "finish", "reason": "stop"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_normalizes_tool_use_finish_reason(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_1","name":"search"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-sonnet-4.6"
    ):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "toolu_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_stream_messages_falls_back_to_tool_calls_finish_when_tool_block_is_present(
    metadata_copilot_adapter: GitHubCopilotAdapter,
) -> None:
    sse_body = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"toolu_1","name":"search"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"copilot_tool_stop"}}\n\n'
    )
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    chunks = []
    async for chunk in metadata_copilot_adapter.stream(
        SAMPLE_MESSAGES, model_id="claude-sonnet-4.6"
    ):
        chunks.append(chunk)

    assert chunks == [
        {
            "type": "tool_call_delta",
            "id": "toolu_1",
            "name_delta": "search",
            "arguments_delta": "",
        },
        {"type": "finish", "reason": "tool_calls"},
    ]
