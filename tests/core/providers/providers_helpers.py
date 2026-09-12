"""Shared fixtures and fakes for providers behavior tests.

Verifies loading from JSON fixtures, lookup by provider ID, immutability,
missing-provider errors, caching behaviour, and correct parsing of connection
fields, extra_headers, defaults, and models_endpoint.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from core.providers.providers import (
    _registry_cache,
)

# Fixtures
OPENAI_DATA: dict[str, Any] = {
    "id": "openai",
    "name": "OpenAI",
    "adapter": "openai_compatible",
    "base_url": "https://api.openai.com/v1",
    "connections": [
        {
            "id": "oauth",
            "type": "oauth",
            "label": "OAuth",
            "auth": {
                "header": "Authorization",
                "prefix": "Bearer ",
                "credential_key": "OPENAI_OAUTH_TOKEN",
            },
        },
        {
            "id": "api-key",
            "type": "api_key",
            "label": "API Key",
            "auth": {
                "header": "Authorization",
                "prefix": "Bearer ",
                "credential_key": "OPENAI_API_KEY",
            },
        },
    ],
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
}

OPENROUTER_DATA: dict[str, Any] = {
    "id": "openrouter",
    "name": "OpenRouter",
    "adapter": "openai_compatible",
    "base_url": "https://openrouter.ai/api/v1",
    "connections": [
        {
            "id": "api-key",
            "type": "api_key",
            "label": "API Key",
            "auth": {
                "header": "Authorization",
                "prefix": "Bearer ",
                "credential_key": "OPENROUTER_API_KEY",
            },
        }
    ],
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
    "extra_headers": {"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
    "models_endpoint": "/models",
}

ANTHROPIC_DATA: dict[str, Any] = {
    "id": "anthropic",
    "name": "Anthropic",
    "adapter": "anthropic",
    "base_url": "https://api.anthropic.com/v1",
    "connections": [
        {
            "id": "api-key",
            "type": "api_key",
            "label": "API Key",
            "auth": {
                "header": "x-api-key",
                "prefix": "",
                "credential_key": "ANTHROPIC_API_KEY",
            },
        }
    ],
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
}


@pytest.fixture()
def providers_dir(tmp_path: Path) -> Path:
    """Create a temporary resources directory with provider JSON files."""
    prov_dir = tmp_path / "providers"
    prov_dir.mkdir()
    for name, data in [
        ("openai.json", OPENAI_DATA),
        ("openrouter.json", OPENROUTER_DATA),
        ("anthropic.json", ANTHROPIC_DATA),
    ]:
        (prov_dir / name).write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    """Clear the module-level registry cache before and after each test."""
    _registry_cache.clear()
    yield
    _registry_cache.clear()
