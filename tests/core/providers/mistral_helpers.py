"""Shared fixtures and fakes for mistral behavior tests."""

from __future__ import annotations

import pytest

from core.providers.mistral import MistralAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-mistral-key"

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]


@pytest.fixture()
def mistral_config() -> ProviderConfig:
    return ProviderConfig(
        id="mistral",
        name="Mistral AI",
        adapter="mistral",
        base_url="https://api.mistral.ai/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="MISTRAL_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


@pytest.fixture()
def mistral_adapter(mistral_config: ProviderConfig) -> MistralAdapter:
    return MistralAdapter(mistral_config, API_KEY)
