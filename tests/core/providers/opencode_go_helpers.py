"""Shared fixtures and fakes for opencode go behavior tests."""

from __future__ import annotations

import pytest

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.opencode_go import (
    OpenCodeGoAdapter,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-opencode-go-key"

OPENCODE_GO_URL = "https://opencode-go.example/v1/chat/completions"

OPENCODE_GO_MESSAGES_URL = "https://opencode-go.example/v1/messages"

OPENCODE_GO_RESPONSES_URL = "https://opencode-go.example/v1/responses"

CLOSED_TOOL = {
    "name": "inspect_probe",
    "description": "Inspect one synthetic value.",
    "parameters": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
}

# Per-model wire protocol is now DATA (metadata.opencode_go.protocol), not a
# hardcoded adapter set. These ids carry "anthropic" in the protocol map below
# and must route through the internal Messages adapter.
ANTHROPIC_MESSAGES_MODELS: tuple[str, ...] = (
    "minimax-m2.5",
    "minimax-m2.7",
    "minimax-m3",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
)

# Small per-model profiles mirroring the independent facts carried by
# ``metadata.opencode_go``. Models absent here are unknown to the adapter.
_PROFILE_BY_MODEL: dict[str, dict[str, object]] = {
    "minimax-m2.7": {"protocol": "anthropic"},
    "minimax-m2.5": {"protocol": "anthropic"},
    "minimax-m3": {"protocol": "anthropic"},
    "qwen3.7-plus": {"protocol": "anthropic"},
    "qwen3.7-max": {"protocol": "anthropic"},
    "qwen3.8-max": {"protocol": "anthropic"},
    "qwen3.8-flash": {"protocol": "anthropic"},
    "qwen3.6-plus": {"protocol": "anthropic"},
    "deepseek-v4-flash": {"protocol": "openai"},
    "deepseek-v4-flash-vision-exp": {"protocol": "openai"},
    "deepseek-v4-pro": {"protocol": "openai"},
    "glm-5.1": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "glm-5.2": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "glm-5.3": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "glm-5.3-flash": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning_content",
    },
    "longcat-2.0": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning_content",
    },
    "gpt-5.6-luna": {"protocol": "responses"},
    "muse-spark-1.2-contributor": {"protocol": "responses"},
    "muse-spark-1.3-contributor": {"protocol": "responses"},
    "grok-4.5": {
        "minimum_reasoning_effort": "low",
        "protocol": "responses",
    },
    "grok-4.6": {
        "minimum_reasoning_effort": "low",
        "protocol": "responses",
    },
    "hy3": {"protocol": "openai", "reasoning_response_field": "reasoning"},
    "hy4-preview": {"protocol": "openai", "reasoning_response_field": "reasoning"},
    "kimi-k2.5": {
        "protocol": "openai",
        "thinking_control": "toggle",
    },
    "kimi-k2.6": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning",
        "thinking_control": "toggle",
        "thinking_keep": "all",
    },
    "kimi-k2.7-code": {
        "protocol": "openai",
        "thinking_control": "always_enabled",
    },
    "kimi-k3": {
        "minimum_reasoning_effort": "low",
        "protocol": "openai",
        "reasoning_response_field": "reasoning",
    },
    "mimo-v2.5": {"protocol": "openai", "reasoning_response_field": "reasoning_content"},
    "mimo-v2.5-pro": {
        "protocol": "openai",
        "reasoning_response_field": "reasoning_content",
    },
    "qwen3.6-plus-openai": {
        "protocol": "openai",
    },
}


def _model_with_profile(
    model_id: str,
    profile: dict[str, object] | None,
) -> Model:
    metadata: dict[str, object] = {}
    if profile is not None:
        metadata = {"opencode_go": profile}
    reasoning = ReasoningCapabilities(supported=True)
    if model_id in {"kimi-k2.5", "kimi-k2.6"}:
        reasoning = ReasoningCapabilities(supported=True, control="on_off")
    elif model_id == "gpt-5.6-luna":
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("none", "low", "medium", "high", "xhigh", "max"),
        )
    elif model_id in {"muse-spark-1.2-contributor", "muse-spark-1.3-contributor"}:
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("minimal", "low", "medium", "high", "xhigh"),
        )
    elif model_id in {"grok-4.5", "grok-4.6"}:
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("low", "medium", "high"),
        )
    elif model_id == "kimi-k3":
        reasoning = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("low", "high", "max"),
        )
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=reasoning,
        ),
        context_window=1_000_000,
        max_output_tokens=131_072,
        metadata=metadata,
    )


def _protocol_lookup(model_id: str) -> Model | None:
    """Resolve the metadata-carrying model for one bare or vendor-prefixed id."""

    bare = model_id.split("::", 1)[0]
    candidates = [model_id, bare]
    if "/" in bare:
        candidates.append(bare.rsplit("/", 1)[-1])
    for candidate in candidates:
        if candidate in _PROFILE_BY_MODEL:
            return _model_with_profile(candidate, _PROFILE_BY_MODEL[candidate])
    return None


@pytest.fixture()
def opencode_go_config() -> ProviderConfig:
    return ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://opencode-go.example/v1",
        extra_headers={"User-Agent": "vBot"},
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_GO_API_KEY",
                ),
            )
        ],
    )


@pytest.fixture()
def opencode_go_adapter(opencode_go_config: ProviderConfig) -> OpenCodeGoAdapter:
    # The adapter routes on ``metadata.opencode_go.protocol`` resolved via
    # ``model_lookup``; inject the protocol map so routing is data-driven.
    return OpenCodeGoAdapter(opencode_go_config, API_KEY, model_lookup=_protocol_lookup)


RESPONSES_COMPLETED_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "model": "gpt-5.6-luna",
    "output": [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "enc-blob",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Done"}],
        },
    ],
    "usage": {"input_tokens": 11, "output_tokens": 5},
}
