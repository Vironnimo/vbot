"""Offline cross-wire conformance for Reasoning across Model route changes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from core.chat import ChatMessage, ToolCall
from core.chat.messages import _embed_notes_into_request
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.github_copilot_policy import RESPONSES_ENDPOINT, copilot_model_policy
from core.providers.github_copilot_responses import build_responses_payload
from core.providers.mistral import MistralAdapter
from core.providers.ollama import OllamaAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY

SOURCE_SCOPE = "source/reasoning-model::connection:account"
READABLE_REASONING = "The two Tool outputs must be compared."
OPAQUE_SENTINELS = (
    "foreign-signature",
    "foreign-redacted",
    "foreign-encrypted",
    "rs_foreign",
    SOURCE_SCOPE,
)


def _provider_config(
    provider_id: str,
    adapter: str,
    *,
    base_url: str,
    max_tokens: int | None = None,
) -> ProviderConfig:
    defaults = {"max_tokens": max_tokens} if max_tokens is not None else None
    return ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter=adapter,
        base_url=base_url,
        connections=[
            ConnectionConfig(
                id="test",
                type="api_key",
                label="Test",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="TEST_API_KEY",
                ),
            )
        ],
        defaults=defaults,
    )


def _responses_policy(model_id: str = "gpt-5.4"):
    return copilot_model_policy(
        model_id,
        {
            "github_copilot": {
                "vendor": "OpenAI",
                "family": model_id,
                "version": model_id,
                "supported_endpoints": [RESPONSES_ENDPOINT],
                "reasoning_efforts": ["low", "medium", "high", "xhigh"],
                "tool_calls": True,
                "parallel_tool_calls": True,
                "streaming": True,
                "structured_outputs": True,
            }
        },
    )


def _foreign_tool_history() -> list[ChatMessage]:
    return [
        ChatMessage.user("Compare both files."),
        ChatMessage.assistant(
            model="source/reasoning-model::connection",
            content=None,
            reasoning=READABLE_REASONING,
            reasoning_meta={
                "content_blocks": [
                    {"type": "thinking", "thinking": "hidden", "signature": "foreign-signature"},
                    {"type": "redacted_thinking", "data": "foreign-redacted"},
                ],
                "response_output": [
                    {
                        "type": "reasoning",
                        "id": "rs_foreign",
                        "encrypted_content": "foreign-encrypted",
                    }
                ],
            },
            reasoning_scope=SOURCE_SCOPE,
            tool_calls=[
                ToolCall(
                    id="source-call:alpha/123",
                    name="read",
                    arguments={"path": "a.py"},
                ),
                ToolCall(
                    id="source-call:beta/456",
                    name="read",
                    arguments={"path": "b.py"},
                ),
            ],
        ),
        ChatMessage.tool(
            tool_call_id="source-call:alpha/123",
            name="read",
            content='{"ok":true,"data":"alpha"}',
        ),
        ChatMessage.tool(
            tool_call_id="source-call:beta/456",
            name="read",
            content='{"ok":true,"data":"beta"}',
        ),
    ]


def _build_adapter_payload(
    adapter: OpenAICompatibleAdapter | AnthropicCompatibleAdapter | OllamaAdapter,
    messages: list[dict[str, Any]],
    *,
    model_id: str,
) -> dict[str, Any]:
    try:
        return adapter._build_payload(messages, model_id)
    finally:
        asyncio.run(adapter.aclose())


def _render_openai(messages: list[dict[str, Any]]) -> dict[str, Any]:
    config = _provider_config(
        "openai-compatible",
        "openai_compatible",
        base_url="https://openai-compatible.invalid/v1",
    )
    return _build_adapter_payload(
        OpenAICompatibleAdapter(config, "test-token"),
        messages,
        model_id="target-chat-model",
    )


def _render_anthropic(messages: list[dict[str, Any]]) -> dict[str, Any]:
    config = _provider_config(
        "anthropic",
        "anthropic",
        base_url="https://anthropic.invalid/v1",
        max_tokens=4096,
    )
    return _build_adapter_payload(
        AnthropicCompatibleAdapter(config, "test-token"),
        messages,
        model_id="target-messages-model",
    )


def _render_responses(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return build_responses_payload(
        messages,
        model_id="gpt-5.4",
        policy=_responses_policy(),
    )


def _render_mistral(messages: list[dict[str, Any]]) -> dict[str, Any]:
    config = _provider_config(
        "mistral",
        "mistral",
        base_url="https://mistral.invalid/v1",
        max_tokens=4096,
    )
    return _build_adapter_payload(
        MistralAdapter(config, "test-token"),
        messages,
        model_id="target-mistral-model",
    )


def _render_ollama(messages: list[dict[str, Any]]) -> dict[str, Any]:
    config = _provider_config(
        "ollama",
        "ollama",
        base_url="http://ollama.invalid",
    )
    return _build_adapter_payload(
        OllamaAdapter(config, "test-token"),
        messages,
        model_id="target-ollama-model",
    )


WIRE_PROFILES: tuple[tuple[str, str, Callable[[list[dict[str, Any]]], dict[str, Any]]], ...] = (
    ("chat-completions", "openai/target-chat-model::api-key", _render_openai),
    ("messages", "anthropic/target-messages-model::api-key", _render_anthropic),
    ("responses", "github-copilot/gpt-5.4::oauth", _render_responses),
    ("mistral", "mistral/target-mistral-model::api-key", _render_mistral),
    ("ollama", "ollama/target-ollama-model::local", _render_ollama),
)


@pytest.mark.parametrize(
    ("profile", "target_scope", "renderer"),
    WIRE_PROFILES,
    ids=[profile for profile, _, _ in WIRE_PROFILES],
)
def test_cross_route_history_is_safe_and_tool_correlated_on_every_wire(
    profile: str,
    target_scope: str,
    renderer: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> None:
    request_messages = _embed_notes_into_request(
        _foreign_tool_history(),
        replay_policy=REASONING_REPLAY_FULL_HISTORY,
        agent_model=target_scope,
    )

    payload = renderer(request_messages)

    serialized = json.dumps(payload, sort_keys=True)
    assert READABLE_REASONING in serialized
    for opaque_sentinel in OPAQUE_SENTINELS:
        assert opaque_sentinel not in serialized
    call_ids, result_ids = _wire_tool_ids(profile, payload)
    assert len(call_ids) == 2
    assert call_ids == result_ids
    assert len(set(call_ids)) == 2
    if profile == "mistral":
        assert all(len(tool_call_id) == 9 and tool_call_id.isalnum() for tool_call_id in call_ids)


def _wire_tool_ids(profile: str, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    if profile == "responses":
        inputs = payload["input"]
        call_ids = [item["call_id"] for item in inputs if item.get("type") == "function_call"]
        result_ids = [
            item["call_id"] for item in inputs if item.get("type") == "function_call_output"
        ]
        return call_ids, result_ids
    if profile == "messages":
        blocks = [
            block
            for message in payload["messages"]
            for block in message["content"]
            if isinstance(block, dict)
        ]
        call_ids = [block["id"] for block in blocks if block.get("type") == "tool_use"]
        result_ids = [
            block["tool_use_id"] for block in blocks if block.get("type") == "tool_result"
        ]
        return call_ids, result_ids

    messages = payload["messages"]
    call_ids = [
        tool_call["id"]
        for message in messages
        if message.get("role") == "assistant"
        for tool_call in message.get("tool_calls", [])
    ]
    result_ids = [message["tool_call_id"] for message in messages if message.get("role") == "tool"]
    return call_ids, result_ids


@pytest.mark.parametrize("profile", ["messages", "responses"])
def test_same_route_full_history_keeps_exact_provider_owned_reasoning(profile: str) -> None:
    target_scope = (
        "anthropic/claude-sonnet-4::api-key"
        if profile == "messages"
        else "github-copilot/gpt-5.4::oauth"
    )
    reasoning_meta = (
        {
            "content_blocks": [
                {
                    "type": "thinking",
                    "thinking": "native thinking",
                    "signature": "native-signature",
                }
            ]
        }
        if profile == "messages"
        else {
            "response_output": [
                {
                    "type": "reasoning",
                    "id": "rs_native",
                    "encrypted_content": "native-encrypted",
                }
            ]
        }
    )
    messages = [
        ChatMessage.user("Continue."),
        ChatMessage.assistant(
            model=target_scope,
            content=None,
            reasoning="Native readable reasoning.",
            reasoning_meta=reasoning_meta,
            reasoning_scope=target_scope,
        ),
    ]

    request_messages = _embed_notes_into_request(
        messages,
        replay_policy=REASONING_REPLAY_FULL_HISTORY,
        agent_model=target_scope,
    )
    payload = (
        _render_anthropic(request_messages)
        if profile == "messages"
        else _render_responses(request_messages)
    )

    serialized = json.dumps(payload, sort_keys=True)
    expected_opaque = "native-signature" if profile == "messages" else "native-encrypted"
    assert expected_opaque in serialized
    assert "provider-neutral context" not in serialized
