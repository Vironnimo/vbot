"""Shared fixtures, constants, and imports for OpenAI-compatible provider tests."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _to_openai_assistant_message,
    _to_openai_user_content_part,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS

__all__ = [
    "API_KEY",
    "CANONICAL_MESSAGES_WITH_TOOL_LOOP",
    "HISTORY_TOOL_DESCRIPTION",
    "HISTORY_TOOL_NAME",
    "HISTORY_TOOL_PARAMETERS",
    "IMAGE_WIRE_MEDIA_TYPES",
    "MINIMAL_URL",
    "NO_DEFAULTS_CONFIG",
    "OPENAI_CONFIG",
    "OPENAI_MULTI_AUTH_CONFIG",
    "OPENAI_URL",
    "OPENROUTER_CONFIG",
    "OPENROUTER_URL",
    "READ_TOOL_DEFINITION",
    "SAMPLE_MESSAGES",
    "SAMPLE_TOOLS",
    "SUCCESS_RESPONSE",
    "Any",
    "AsyncMock",
    "AuthConfig",
    "Capabilities",
    "ConnectionConfig",
    "Model",
    "NetworkError",
    "OpenAICompatibleAdapter",
    "ProviderAuthError",
    "ProviderConfig",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ReasoningCapabilities",
    "_to_openai_assistant_message",
    "_to_openai_user_content_part",
    "httpx",
    "json",
    "logging",
    "patch",
    "pytest",
    "replace",
    "respx",
    "openai_adapter",
    "openrouter_adapter",
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OPENAI_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OPENAI_API_KEY",
            ),
        )
    ],
    defaults={"max_tokens": 4096, "temperature": 0.7},
)

OPENAI_MULTI_AUTH_CONFIG = ProviderConfig(
    id="openai",
    name="OpenAI",
    adapter="openai_compatible",
    base_url="https://api.openai.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OPENAI_API_KEY",
            ),
        ),
        ConnectionConfig(
            id="service-account",
            type="api_key",
            label="Service Account",
            auth=AuthConfig(
                header="x-service-token",
                prefix="Token ",
                credential_key="OPENAI_SERVICE_TOKEN",
            ),
        ),
    ],
)

OPENROUTER_CONFIG = ProviderConfig(
    id="openrouter",
    name="OpenRouter",
    adapter="openai_compatible",
    base_url="https://openrouter.ai/api/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="OPENROUTER_API_KEY",
            ),
        )
    ],
    defaults={"max_tokens": 4096},
    extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
)

NO_DEFAULTS_CONFIG = ProviderConfig(
    id="minimal",
    name="Minimal Provider",
    adapter="openai_compatible",
    base_url="https://api.minimal.example/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="x-api-key", prefix="", credential_key="MINIMAL_API_KEY"),
        )
    ],
)

API_KEY = "test-api-key-12345"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MINIMAL_URL = "https://api.minimal.example/v1/chat/completions"

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
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

CANONICAL_MESSAGES_WITH_TOOL_LOOP = [
    {"role": "system", "model": "openai/gpt-5.2", "content": "You are helpful."},
    {"role": "user", "content": "Weather?"},
    {
        "role": "assistant",
        "model": "openai/gpt-5.2",
        "content": None,
        "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
        "tool_calls": [
            {
                "id": "call_abc",
                "name": "get_weather",
                "arguments": {"city": "Berlin"},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_abc",
        "name": "get_weather",
        "content": '{"temp":22}',
    },
]

SAMPLE_TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]

READ_TOOL_DEFINITION = {
    "name": "read",
    "description": "Read a text file from disk. Relative paths resolve from the workspace.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


@pytest.fixture()
def openai_adapter():
    """OpenAI-compatible adapter with default OpenAI config."""
    return OpenAICompatibleAdapter(OPENAI_CONFIG, API_KEY)


@pytest.fixture()
def openrouter_adapter():
    """OpenAI-compatible adapter with OpenRouter config (extra headers)."""
    return OpenAICompatibleAdapter(OPENROUTER_CONFIG, API_KEY)
