"""Shared fixtures and fakes for image providers behavior tests."""

from __future__ import annotations

import base64

from core.model_tasks.image_providers import (
    ProviderImageClient,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"


def _unified_image_response(*image_bytes: bytes, usage: dict | None = None) -> dict:
    """Build an OpenRouter unified image API response body."""

    body: dict = {
        "created": 1,
        "data": [
            {"b64_json": base64.b64encode(payload).decode("ascii")} for payload in image_bytes
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _openrouter_image_client(model_id: str) -> ProviderImageClient:
    """Build a ProviderImageClient wired to a mockable OpenRouter endpoint."""

    provider = ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[],
        extra_headers={"X-Title": "vBot"},
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="OPENROUTER_API_KEY",
        ),
    )
    return ProviderImageClient(
        provider=provider,
        connection=connection,
        credential="sk-test",
        model_id=model_id,
    )
