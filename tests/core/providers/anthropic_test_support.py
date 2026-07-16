"""Shared fixtures, constants, and helpers for Anthropic provider tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.anthropic import (
    ANTHROPIC_METADATA_KEY,
    SUPPORTS_TEMPERATURE_METADATA_FIELD,
    AnthropicAdapter,
)
from core.providers.anthropic_compatible import (
    AnthropicCompatibleAdapter,
    _to_anthropic_user_content_block,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY
from core.tools import HISTORY_TOOL_DESCRIPTION, HISTORY_TOOL_NAME, HISTORY_TOOL_PARAMETERS

__all__ = [
    "json",
    "AsyncMock",
    "patch",
    "httpx",
    "pytest",
    "respx",
    "REASONING_CONTROL_BUDGET",
    "REASONING_CONTROL_LEVELS",
    "Capabilities",
    "Model",
    "ReasoningCapabilities",
    "IMAGE_WIRE_MEDIA_TYPES",
    "ANTHROPIC_METADATA_KEY",
    "SUPPORTS_TEMPERATURE_METADATA_FIELD",
    "AnthropicAdapter",
    "AnthropicCompatibleAdapter",
    "_to_anthropic_user_content_block",
    "NetworkError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "AuthConfig",
    "ConnectionConfig",
    "ProviderConfig",
    "REASONING_REPLAY_FULL_HISTORY",
    "HISTORY_TOOL_DESCRIPTION",
    "HISTORY_TOOL_NAME",
    "HISTORY_TOOL_PARAMETERS",
    "_strip_cache_control",
    "_message_without_cache",
    "_block_without_cache",
    "ANTHROPIC_CONFIG",
    "ANTHROPIC_MULTI_AUTH_CONFIG",
    "CUSTOM_CONFIG",
    "NO_DEFAULTS_CONFIG",
    "API_KEY",
    "ANTHROPIC_URL",
    "CUSTOM_URL",
    "MINIMAL_URL",
    "SUCCESS_RESPONSE",
    "SAMPLE_MESSAGES",
    "CANONICAL_MESSAGES_WITH_TOOL_LOOP",
    "SAMPLE_TOOLS",
    "READ_TOOL_DEFINITION",
    "SAMPLE_MESSAGES_WITH_SYSTEM",
    "MULTITURN_MESSAGES",
    "anthropic_adapter",
    "custom_adapter",
    "_anthropic_test_model",
    "_anthropic_control_model",
]


def _strip_cache_control(payload: dict) -> dict:
    """Return ``payload`` with always-on prompt-caching markers removed.

    Prompt caching adds ``cache_control`` to the last block of the system field
    and recent messages on every request. Structural tests assert the underlying
    wire mapping and ignore those markers; their placement is verified directly
    by :class:`TestPromptCaching`.
    """

    cleaned = dict(payload)
    system = cleaned.get("system")
    if isinstance(system, list):
        cleaned["system"] = [_block_without_cache(block) for block in system]
    messages = cleaned.get("messages")
    if isinstance(messages, list):
        cleaned["messages"] = [_message_without_cache(message) for message in messages]
    return cleaned


def _message_without_cache(message: dict) -> dict:
    content = message.get("content")
    if isinstance(content, list):
        cleaned = dict(message)
        cleaned["content"] = [_block_without_cache(block) for block in content]
        return cleaned
    return message


def _block_without_cache(block):
    if isinstance(block, dict) and "cache_control" in block:
        cleaned = dict(block)
        del cleaned["cache_control"]
        return cleaned
    return block


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ANTHROPIC_CONFIG = ProviderConfig(
    id="anthropic",
    name="Anthropic",
    adapter="anthropic",
    base_url="https://api.anthropic.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="x-api-key", prefix="", credential_key="ANTHROPIC_API_KEY"),
        )
    ],
    defaults={"max_tokens": 4096},
)

ANTHROPIC_MULTI_AUTH_CONFIG = ProviderConfig(
    id="anthropic",
    name="Anthropic",
    adapter="anthropic",
    base_url="https://api.anthropic.com/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="x-api-key", prefix="", credential_key="ANTHROPIC_API_KEY"),
        ),
        ConnectionConfig(
            id="oauth",
            type="oauth",
            label="OAuth",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="ANTHROPIC_OAUTH_TOKEN",
            ),
        ),
    ],
    defaults={"max_tokens": 4096},
)

CUSTOM_CONFIG = ProviderConfig(
    id="anthropic-custom",
    name="Anthropic Custom",
    adapter="anthropic",
    base_url="https://custom.anthropic.example/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key="CUSTOM_ANTHROPIC_API_KEY",
            ),
        )
    ],
    defaults={"max_tokens": 8192, "temperature": 0.7},
    extra_headers={"X-Custom-Header": "custom-value"},
)

NO_DEFAULTS_CONFIG = ProviderConfig(
    id="anthropic-minimal",
    name="Anthropic Minimal",
    adapter="anthropic",
    base_url="https://minimal.anthropic.example/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key="MINIMAL_ANTHROPIC_API_KEY",
            ),
        )
    ],
)

API_KEY = "test-anthropic-key-12345"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CUSTOM_URL = "https://custom.anthropic.example/v1/messages"
MINIMAL_URL = "https://minimal.anthropic.example/v1/messages"

SUCCESS_RESPONSE = {
    "id": "msg_01XFDUDYJGAAC8998t2N3v",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello!"}],
    "model": "claude-sonnet-4-20250219",
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

SAMPLE_MESSAGES = [
    {"role": "user", "content": "Hello"},
]

CANONICAL_MESSAGES_WITH_TOOL_LOOP = [
    {
        "role": "system",
        "model": "anthropic/claude-sonnet-4-20250219",
        "content": "You are helpful.",
    },
    {"role": "user", "content": "Weather?"},
    {
        "role": "assistant",
        "model": "anthropic/claude-sonnet-4-20250219",
        "content": None,
        "reasoning": "Need weather.",
        "reasoning_meta": {
            "content_blocks": [
                {
                    "type": "thinking",
                    "thinking": "Need weather.",
                    "signature": "opaque-current-turn",
                }
            ]
        },
        "tool_calls": [
            {
                "id": "toolu_abc",
                "name": "get_weather",
                "arguments": {"city": "Berlin"},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "toolu_abc",
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

SAMPLE_MESSAGES_WITH_SYSTEM = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"},
]

MULTITURN_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {
        "role": "user",
        "content": [{"type": "text", "text": "What is 2+2?"}],
    },
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "2+2 equals 4."}],
    },
    {
        "role": "user",
        "content": [{"type": "text", "text": "And 3+3?"}],
    },
]


@pytest.fixture()
def anthropic_adapter():
    """Anthropic adapter with default Anthropic config."""
    return AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)


@pytest.fixture()
def custom_adapter():
    """Anthropic adapter with custom config (extra headers, overrides)."""
    return AnthropicAdapter(CUSTOM_CONFIG, API_KEY)


def _anthropic_test_model(model_id: str, *, reasoning: bool) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=reasoning),
        ),
        context_window=200000,
        max_output_tokens=8192,
    )


def _anthropic_control_model(
    model_id: str,
    *,
    control: str,
    budget_max: int | None = None,
    context_window: int = 200000,
    max_output_tokens: int = 64000,
) -> Model:
    """A reasoning Claude with a specific wire control (``budget`` / ``on_off``)."""
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control=control,
                budget_max=budget_max,
            ),
        ),
        context_window=context_window,
        # A realistic budget-Claude output ceiling: large enough that an effort's
        # thinking budget fits without the max_tokens clamp confounding the test.
        max_output_tokens=max_output_tokens,
    )


# ---------------------------------------------------------------------------
# Constructor contract
# ---------------------------------------------------------------------------
