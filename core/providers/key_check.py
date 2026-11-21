"""Minimal read-only authentication checks for the Connect dialog."""

from __future__ import annotations

import asyncio
from typing import Literal

import httpx

from core.providers._messages_constants import ANTHROPIC_VERSION
from core.providers._usage_types import (
    OLLAMA_BALANCE_PATH,
    OPENCODE_GO_USAGE_PATH,
    OPENROUTER_KEY_PATH,
)
from core.providers.http import build_async_client
from core.providers.providers import ConnectionConfig, ProviderConfig

type KeyCheck = Literal["verified", "rejected", "unavailable", "unsupported"]

# These services expose account metadata without creating a Chat or generating tokens.
# Bind each override to the exact base URL. Custom gateways use their own origin.
_ACCOUNT_ENDPOINTS = {
    "https://openrouter.ai/api/v1": (OPENROUTER_KEY_PATH, "data", dict),
    "https://ollama.com": (OLLAMA_BALANCE_PATH, "included", dict),
    "https://opencode.ai/inference/go/v1": (OPENCODE_GO_USAGE_PATH, "usage", dict),
    "https://opencode.ai/inference/v1": (
        "https://opencode.ai/console/api/v1/models",
        "opencode",
        dict,
    ),
    "https://integrate.api.nvidia.com/v1": (
        "https://api.nvcf.nvidia.com/v2/nvcf/functions/ids",
        "functionIds",
        list,
    ),
    "https://api.x.ai/v1": ("/api-key", "api_key_id", str),
}


async def check_api_key(
    provider: ProviderConfig, connection: ConnectionConfig, value: str
) -> KeyCheck:
    """Verify this exact candidate without generating tokens or publishing credentials."""
    base = (connection.base_url or provider.base_url).rstrip("/")
    metadata = _ACCOUNT_ENDPOINTS.get(base)
    endpoint = (
        metadata[0]
        if metadata
        else connection.models_endpoint or provider.models_endpoint or "/models"
    )
    url = (
        endpoint
        if metadata and endpoint.startswith("https://")
        else f"{base}/{endpoint.lstrip('/')}"
    )
    headers = {**(provider.extra_headers or {}), "Accept": "application/json"}
    if provider.adapter == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    if not value.isascii() or not value.isprintable():
        return "rejected"
    auth = connection.auth

    try:
        async with asyncio.timeout(8), build_async_client(base_url=base) as client:
            response = await client.get(url, headers={**headers, auth.header: auth.prefix + value})
            if _key_rejected(provider.adapter, response):
                return "rejected"
            if response.status_code == 404:
                return "unsupported"
            if not response.is_success:
                return "unavailable"

            body = response.json()
            if (
                not isinstance(body, dict)
                or body.get("error")
                or body.get("base_resp", {}).get("status_code", 0)
            ):
                return "unavailable"
            if (
                metadata
                and endpoint == "/api-key"
                and any(
                    body.get(flag)
                    for flag in ("api_key_disabled", "api_key_blocked", "team_blocked")
                )
            ):
                return "rejected"
            if metadata:
                return (
                    "verified" if isinstance(body.get(metadata[1]), metadata[2]) else "unavailable"
                )
            # A custom gateway can expose a public Model list even when its
            # Connection requires a key for inference. Only a protected read
            # distinguishes the candidate from a placeholder.
            if not any(isinstance(body.get(field), list) for field in ("data", "models")):
                return "unsupported"
            control = await client.get(
                url, headers={**headers, auth.header: auth.prefix + "vbot-invalid-key"}
            )
            if _key_rejected(provider.adapter, control):
                return "verified"
            return "unsupported" if control.is_success else "unavailable"
    except TimeoutError, httpx.HTTPError, ValueError, AttributeError, TypeError:
        # Neither the remote response nor a transport exception is safe for display.
        return "unavailable"


def _key_rejected(adapter: str, response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True
    if adapter == "google" and response.status_code == 400:
        details = response.json().get("error", {}).get("details", [])
        return any(isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details)
    if adapter == "xai" and response.status_code in {400, 403}:
        body = response.json()
        return body.get("code") == "unauthenticated:bad-credentials" or (
            body.get("code") == "invalid-argument"
            and str(body.get("error", "")).startswith("Incorrect API key provided")
        )
    return False
