"""Shared configuration, Models and wire helpers for the OpenCode Zen Adapter tests."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers import OpenCodeZenAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

BASE_URL = "https://opencode.ai/zen/v1"
RESPONSES_URL = f"{BASE_URL}/responses"
MESSAGES_URL = f"{BASE_URL}/messages"
CHAT_URL = f"{BASE_URL}/chat/completions"
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"{BASE_URL}/models/{GEMINI_MODEL}:generateContent"
GEMINI_STREAM_URL = f"{BASE_URL}/models/{GEMINI_MODEL}:streamGenerateContent?alt=sse"

# One reviewed Model per Zen wire protocol.
RESPONSES_MODEL = "gpt-5.6-sol"
MESSAGES_MODEL = "claude-sonnet-5"
CHAT_MODEL = "deepseek-v4-flash"
WIRE_MODELS = (RESPONSES_MODEL, MESSAGES_MODEL, CHAT_MODEL, GEMINI_MODEL)


def zen_config() -> ProviderConfig:
    return ProviderConfig(
        id="opencode-zen",
        name="OpenCode Zen",
        adapter="opencode_zen",
        base_url=BASE_URL,
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
    )


def zen_model(model_id: str) -> Model:
    """A reviewed catalog Model with a levels ladder and Gemini-only vision."""

    normalized = OpenCodeZenAdapter.normalize_catalog_entry({"id": model_id}, {})
    gemini = model_id.startswith("gemini")
    return replace(
        normalized,
        capabilities=Capabilities(
            vision=gemini,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control="levels",
                levels=("minimal", "low", "medium", "high"),
            ),
            input_modalities=("text", "image") if gemini else ("text",),
            output_modalities=("text",),
        ),
        context_window=1_048_576,
        max_output_tokens=65_536,
    )


def zen_adapter(token_getter: Any = "zen-secret") -> OpenCodeZenAdapter:
    """An Adapter whose lookup knows only the four reviewed wire Models."""

    models = {model_id: zen_model(model_id) for model_id in WIRE_MODELS}
    return OpenCodeZenAdapter(zen_config(), token_getter, model_lookup=models.get)


def gemini_sse(*chunks: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)


async def gemini_stream(adapter: OpenCodeZenAdapter, *chunks: dict[str, Any]) -> list[dict]:
    """Stream Gemini chunks through the Adapter and return its deltas."""

    with respx.mock:
        respx.post(GEMINI_STREAM_URL).mock(
            return_value=httpx.Response(200, text=gemini_sse(*chunks))
        )
        return [
            delta
            async for delta in adapter.stream(
                [{"role": "user", "content": "hello"}], model_id=GEMINI_MODEL
            )
        ]
