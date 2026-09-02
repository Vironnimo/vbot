"""Target-wire Tool-call ID normalization tests."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

import pytest

from core.providers.adapter import (
    ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE,
    MISTRAL_TOOL_CALL_ID_PROFILE,
    RESPONSES_TOOL_CALL_ID_PROFILE,
    ToolCallIdProfile,
    normalize_tool_call_ids,
)
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.github_copilot_responses import build_responses_payload
from core.providers.mistral import MistralAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

_DASH_UNDERSCORE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_ALPHANUMERIC_ID = re.compile(r"^[A-Za-z0-9]+$")


class _ResponsesPolicy:
    supports_tools = True
    supports_parallel_tool_calls = True
    supports_structured_outputs = True
    allows_any_reasoning_controls = True
    supports_explicit_none_effort = False

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        return dict(kwargs)

    def closest_reasoning_effort(self, effort: Any) -> str | None:
        return effort if isinstance(effort, str) else None

    def supports_request_parameter(self, parameter_name: str) -> bool:
        return parameter_name == "temperature"


def _provider_config(provider_id: str, adapter: str) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter=adapter,
        base_url=f"https://{provider_id}.example.test/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key=f"{provider_id.upper()}_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 1024},
    )


def _tool_cycle(tool_call_id: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": tool_call_id, "name": "lookup", "arguments": {"query": "vBot"}}],
        },
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": '{"ok":true}',
        },
    ]


@pytest.mark.parametrize(
    ("profile", "tool_call_id", "expected_change"),
    [
        (ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE, "call_safe-1", False),
        (ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE, "call|unsafe/1", True),
        (ANTHROPIC_MESSAGES_TOOL_CALL_ID_PROFILE, "x" * 65, True),
        (MISTRAL_TOOL_CALL_ID_PROFILE, "Ab12Cd34E", False),
        (MISTRAL_TOOL_CALL_ID_PROFILE, "call-1", True),
        (RESPONSES_TOOL_CALL_ID_PROFILE, "call_safe-1", False),
        (RESPONSES_TOOL_CALL_ID_PROFILE, "call_safe_", True),
    ],
)
def test_target_profiles_are_safe_deterministic_request_only_transforms(
    profile: ToolCallIdProfile,
    tool_call_id: str,
    expected_change: bool,
) -> None:
    messages = _tool_cycle(tool_call_id)
    original = copy.deepcopy(messages)

    first = normalize_tool_call_ids(messages, profile)
    second = normalize_tool_call_ids(messages, profile)

    normalized_id = first[0]["tool_calls"][0]["id"]
    assert (normalized_id != tool_call_id) is expected_change
    assert first == second
    assert first[1]["tool_call_id"] == normalized_id
    assert messages == original
    assert first is not messages
    assert first[0] is not messages[0]
    if profile is MISTRAL_TOOL_CALL_ID_PROFILE:
        assert len(normalized_id) == 9
        assert _ALPHANUMERIC_ID.fullmatch(normalized_id)
    else:
        assert len(normalized_id) <= 64
        assert _DASH_UNDERSCORE_ID.fullmatch(normalized_id)


def test_transform_preserves_order_and_scopes_repeated_ids_to_each_tool_batch() -> None:
    repeated_id = "foreign|call"
    collision_id = "foreign/call"
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": repeated_id, "name": "first", "arguments": {}},
                {"id": collision_id, "name": "second", "arguments": {}},
            ],
        },
        {"role": "tool", "tool_call_id": repeated_id, "content": "first-result"},
        {"role": "tool", "tool_call_id": collision_id, "content": "second-result"},
        {"role": "assistant", "content": "Continue", "tool_calls": None},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": repeated_id, "name": "third", "arguments": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": repeated_id,
            "content": '{"ok":false,"error":{"code":"interrupted"}}',
        },
    ]

    transformed = normalize_tool_call_ids(messages, MISTRAL_TOOL_CALL_ID_PROFILE)

    first_batch_ids = [call["id"] for call in transformed[0]["tool_calls"]]
    later_batch_id = transformed[4]["tool_calls"][0]["id"]
    assert len({*first_batch_ids, later_batch_id}) == 3
    assert all(len(tool_call_id) == 9 for tool_call_id in [*first_batch_ids, later_batch_id])
    assert [transformed[1]["tool_call_id"], transformed[2]["tool_call_id"]] == first_batch_ids
    assert transformed[5]["tool_call_id"] == later_batch_id
    assert [call["name"] for call in transformed[0]["tool_calls"]] == ["first", "second"]
    assert [transformed[1]["content"], transformed[2]["content"]] == [
        "first-result",
        "second-result",
    ]


@pytest.mark.asyncio
async def test_anthropic_builder_normalizes_paired_cross_wire_ids_without_mutation() -> None:
    foreign_id = f"call|{'+/=' * 40}"
    messages = _tool_cycle(foreign_id)
    original = copy.deepcopy(messages)
    adapter = AnthropicCompatibleAdapter(
        _provider_config("anthropic", "anthropic"),
        "secret",
    )
    try:
        payload = adapter._build_payload(messages, "claude-test")
    finally:
        await adapter.aclose()

    tool_use = payload["messages"][0]["content"][0]
    tool_result = payload["messages"][1]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_result["type"] == "tool_result"
    assert tool_use["id"] == tool_result["tool_use_id"]
    assert len(tool_use["id"]) <= 64
    assert _DASH_UNDERSCORE_ID.fullmatch(tool_use["id"])
    assert messages == original


@pytest.mark.asyncio
async def test_mistral_builder_normalizes_cross_wire_ids_and_openai_chat_remains_unchanged() -> (
    None
):
    foreign_id = f"call|{'+/=' * 40}"
    messages = _tool_cycle(foreign_id)
    original = copy.deepcopy(messages)
    mistral = MistralAdapter(_provider_config("mistral", "mistral"), "secret")
    openai_chat = OpenAICompatibleAdapter(
        _provider_config("openrouter", "openai_compatible"),
        "secret",
    )
    try:
        mistral_payload = mistral._build_payload(messages, "mistral-test")
        openai_payload = openai_chat._build_payload(messages, "openai-test")
    finally:
        await mistral.aclose()
        await openai_chat.aclose()

    mistral_call = mistral_payload["messages"][0]["tool_calls"][0]
    mistral_result = mistral_payload["messages"][1]
    assert len(mistral_call["id"]) == 9
    assert _ALPHANUMERIC_ID.fullmatch(mistral_call["id"])
    assert mistral_result["tool_call_id"] == mistral_call["id"]
    assert openai_payload["messages"][0]["tool_calls"][0]["id"] == foreign_id
    assert openai_payload["messages"][1]["tool_call_id"] == foreign_id
    assert messages == original


def test_responses_builder_normalizes_call_ids_without_forging_response_item_ids() -> None:
    foreign_id = f"call|{'+/=' * 40}"
    response_item_id = "fc_foreign-provider-item"
    response_output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "id": response_item_id,
            "call_id": foreign_id,
            "name": "lookup",
            "arguments": '{"query":"vBot"}',
        },
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_meta": {"response_output": response_output},
            "tool_calls": [{"id": foreign_id, "name": "lookup", "arguments": {"query": "vBot"}}],
        },
        {"role": "tool", "tool_call_id": foreign_id, "content": '{"ok":true}'},
    ]
    original = copy.deepcopy(messages)

    payload = build_responses_payload(
        messages,
        model_id="gpt-test",
        policy=_ResponsesPolicy(),
    )

    reasoning_item, function_call, function_output = payload["input"]
    assert reasoning_item == response_output[0]
    assert function_call["type"] == "function_call"
    assert "id" not in function_call
    assert function_call["call_id"] == function_output["call_id"]
    assert len(function_call["call_id"]) <= 64
    assert _DASH_UNDERSCORE_ID.fullmatch(function_call["call_id"])
    assert messages == original


def test_responses_builder_preserves_valid_same_wire_item_identity() -> None:
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_meta": {
                "response_output": [
                    {
                        "type": "function_call",
                        "id": "fc_provider_item",
                        "call_id": "call_provider_1",
                        "name": "lookup",
                        "arguments": "{}",
                    }
                ]
            },
            "tool_calls": [{"id": "call_provider_1", "name": "lookup", "arguments": {}}],
        },
        {"role": "tool", "tool_call_id": "call_provider_1", "content": "{}"},
    ]

    payload = build_responses_payload(
        messages,
        model_id="gpt-test",
        policy=_ResponsesPolicy(),
    )

    assert payload["input"][0]["id"] == "fc_provider_item"
    assert payload["input"][0]["call_id"] == "call_provider_1"
    assert payload["input"][1]["call_id"] == "call_provider_1"
