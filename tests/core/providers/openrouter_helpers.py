"""Shared fixtures and fakes for openrouter behavior tests."""

from __future__ import annotations

import pytest

from core.providers.openrouter import (
    OPENROUTER_RESPONSES_ENDPOINT,
    OpenRouterAdapter,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-openrouter-key"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

OPENROUTER_RESPONSES_URL = f"https://openrouter.ai/api/v1{OPENROUTER_RESPONSES_ENDPOINT}"

SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}

SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]


@pytest.fixture()
def openrouter_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
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
        defaults={"max_tokens": 8192},
        extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
    )


@pytest.fixture()
def openrouter_adapter(openrouter_config: ProviderConfig) -> OpenRouterAdapter:
    return OpenRouterAdapter(openrouter_config, API_KEY)
