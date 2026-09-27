"""MiniMax: direct Chat Completions and subscription Messages wires."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import ProviderError
from core.providers.minimax import MiniMaxAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-minimax-key"
CHAT_URL = "https://api.minimax.io/v1/chat/completions"
MESSAGES_URL = "https://api.minimax.io/anthropic/v1/messages"
CHAT_SUCCESS = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
MESSAGES_SUCCESS = {
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
}
HELLO = [{"role": "user", "content": "Hello"}]
ABSENT = object()

CONFIG = ProviderConfig(
    id="minimax",
    name="MiniMax",
    adapter="minimax",
    base_url="https://api.minimax.io/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API / Token Plan Key",
            auth=AuthConfig(
                header="Authorization", prefix="Bearer ", credential_key="MINIMAX_API_KEY"
            ),
        )
    ],
    defaults={"max_tokens": 8192},
)


def _catalog_lookup(model_id: str) -> Model:
    return MiniMaxAdapter.normalize_catalog_entry({"id": model_id})


def _adapter(wire: str, *, catalog: bool = False) -> MiniMaxAdapter:
    if wire == "messages":
        return MiniMaxAdapter(
            CONFIG,
            API_KEY,
            base_url="https://api.minimax.io/anthropic/v1",
            auth_config=AuthConfig(header="Authorization", prefix="Bearer "),
            model_lookup=_catalog_lookup,
            connection_mode="anthropic_messages",
        )
    return MiniMaxAdapter(CONFIG, API_KEY, model_lookup=_catalog_lookup if catalog else None)


async def _sent_request(
    adapter: MiniMaxAdapter, url: str, messages: list[dict[str, Any]], **kwargs: Any
) -> httpx.Request:
    """Send once and return the request that reached ``url``."""

    with respx.mock:
        route = respx.post(url).mock(
            return_value=httpx.Response(
                200, json=MESSAGES_SUCCESS if url == MESSAGES_URL else CHAT_SUCCESS
            )
        )
        try:
            await adapter.send(messages, **kwargs)
        finally:
            await adapter.aclose()
    return route.calls.last.request


@pytest.mark.parametrize(
    ("raw_id", "expected"),
    [
        pytest.param(
            "MiniMax-M3",
            Model(
                model_id="MiniMax-M3",
                name="MiniMax M3",
                capabilities=Capabilities(
                    vision=True,
                    tools=True,
                    json_mode=False,
                    reasoning=ReasoningCapabilities(supported=True),
                    input_modalities=("text", "image", "video"),
                    output_modalities=("text",),
                    supported_parameters=(
                        "max_completion_tokens",
                        "max_tokens",
                        "reasoning_split",
                        "stream_options",
                        "temperature",
                        "thinking",
                        "tools",
                        "top_p",
                    ),
                    task_types=(
                        "chat",
                        "text_output",
                        "image_input",
                        "image_understanding",
                        "video_input",
                        "video_understanding",
                    ),
                ),
                context_window=1000000,
                max_output_tokens=131072,
            ),
            id="m3-multimodal",
        ),
        pytest.param(
            "MiniMax-M2.7",
            Model(
                model_id="MiniMax-M2.7",
                name="MiniMax M2.7",
                capabilities=Capabilities(
                    vision=False,
                    tools=True,
                    json_mode=False,
                    reasoning=ReasoningCapabilities(supported=True),
                    input_modalities=("text",),
                    output_modalities=("text",),
                    supported_parameters=(
                        "max_tokens",
                        "reasoning_split",
                        "temperature",
                        "tools",
                        "top_p",
                    ),
                ),
                context_window=204800,
                max_output_tokens=65536,
            ),
            id="m2-text-chat",
        ),
        # A Model without reviewed facts keeps the generic, conservative projection.
        pytest.param(
            "MiniMax-Future",
            Model(
                model_id="MiniMax-Future",
                name="MiniMax-Future",
                capabilities=Capabilities(
                    vision=False,
                    tools=True,
                    json_mode=False,
                    reasoning=ReasoningCapabilities(supported=False),
                ),
                context_window=None,
                max_output_tokens=None,
            ),
            id="unreviewed-model",
        ),
    ],
)
def test_catalog_entry_carries_the_current_model_facts(raw_id: str, expected: Model) -> None:
    assert MiniMaxAdapter.normalize_catalog_entry({"id": raw_id}, {"max_tokens": 8192}) == expected


@pytest.mark.parametrize(
    ("model_id", "effort", "thinking", "reasoning_split"),
    [
        pytest.param("MiniMax-M3", "high", {"type": "adaptive"}, True, id="m3-active-is-adaptive"),
        pytest.param("MiniMax-M3", "none", {"type": "disabled"}, ABSENT, id="m3-none-disables"),
        pytest.param("MiniMax-M2.7", "high", ABSENT, True, id="m2-reasons-without-control"),
    ],
)
@pytest.mark.asyncio
async def test_direct_wire_renders_thinking_and_never_openai_reasoning_controls(
    model_id: str, effort: str, thinking: Any, reasoning_split: Any
) -> None:
    request = await _sent_request(
        _adapter("chat"), CHAT_URL, HELLO, model_id=model_id, thinking_effort=effort
    )
    body = json.loads(request.content)

    for key, value in (("thinking", thinking), ("reasoning_split", reasoning_split)):
        if value is ABSENT:
            assert key not in body, key
        else:
            assert body[key] == value, key
    for key in ("reasoning_effort", "reasoning", "include_reasoning"):
        assert key not in body, key


@pytest.mark.parametrize(
    ("model_id", "request_kwargs", "max_tokens"),
    [
        pytest.param("MiniMax-M2.7", {}, 65536, id="m2-recommended-ceiling"),
        pytest.param("MiniMax-M3", {}, 131072, id="m3-recommended-ceiling"),
        pytest.param("MiniMax-M2.7", {"max_tokens": 1024}, 1024, id="explicit-limit-wins"),
    ],
)
@pytest.mark.asyncio
async def test_output_limit_defaults_to_the_recommended_ceiling_not_the_flat_default(
    model_id: str, request_kwargs: dict[str, Any], max_tokens: int
) -> None:
    request = await _sent_request(
        _adapter("chat", catalog=True), CHAT_URL, HELLO, model_id=model_id, **request_kwargs
    )

    assert json.loads(request.content)["max_tokens"] == max_tokens


@pytest.mark.asyncio
async def test_direct_wire_round_trips_reasoning_details() -> None:
    normalized = _adapter("chat").normalize_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Final answer",
                        "reasoning_details": [{"text": "Reasoning trace"}],
                    }
                }
            ]
        }
    )
    assert normalized["content"] == "Final answer"
    assert normalized["reasoning"] == "Reasoning trace"
    assert normalized["reasoning_meta"] == {"reasoning_details": [{"text": "Reasoning trace"}]}

    request = await _sent_request(
        _adapter("chat"),
        CHAT_URL,
        [
            {"role": "user", "content": "Hi"},
            {
                "role": "assistant",
                "content": normalized["content"],
                "reasoning": normalized["reasoning"],
                "reasoning_meta": normalized["reasoning_meta"],
            },
            {"role": "user", "content": "Follow-up"},
        ],
        model_id="MiniMax-M3",
        thinking_effort="high",
    )

    assert json.loads(request.content)["messages"][1]["reasoning_details"] == [
        {"text": "Reasoning trace"}
    ]


@pytest.mark.asyncio
async def test_subscription_messages_wire_round_trips_signed_reasoning_without_controls() -> None:
    adapter = _adapter("messages")
    thinking_block = {"type": "thinking", "thinking": "Reasoning trace", "signature": "signed"}
    normalized = adapter.normalize_response(
        {
            "role": "assistant",
            "content": [thinking_block, {"type": "text", "text": "Final answer"}],
            "stop_reason": "end_turn",
        },
        model_id="MiniMax-M2.7",
    )
    assert normalized["content"] == "Final answer"
    assert normalized["reasoning"] == "Reasoning trace"
    assert normalized["reasoning_meta"] == {"content_blocks": [thinking_block]}
    assert normalized["terminal_outcome"] == "stop"
    # The Messages wire is text-only.
    assert adapter.wire_media_support("MiniMax-M2.7") == frozenset()

    request = await _sent_request(
        adapter,
        MESSAGES_URL,
        [
            {"role": "user", "content": "Hi"},
            {
                "role": "assistant",
                "content": normalized["content"],
                "reasoning": normalized["reasoning"],
                "reasoning_meta": normalized["reasoning_meta"],
            },
            {"role": "user", "content": "Follow-up"},
        ],
        model_id="MiniMax-M2.7",
        thinking_effort="none",
        reasoning_split=True,
    )

    body = json.loads(request.content)
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert body["max_tokens"] == 65536
    for key in ("thinking", "output_config", "reasoning_split"):
        assert key not in body, key
    assert body["messages"][1]["content"][0] == thinking_block
    assert "cache_control" in json.dumps(body)


@pytest.mark.parametrize(
    ("wire", "temperature"),
    [
        pytest.param("chat", 0, id="chat-zero"),
        pytest.param("chat", 1.1, id="chat-above-one"),
        pytest.param("chat", float("nan"), id="chat-not-finite"),
        pytest.param("messages", 0, id="messages-zero"),
    ],
)
@pytest.mark.asyncio
async def test_unsupported_temperature_is_rejected_before_io(wire: str, temperature: float) -> None:
    adapter = _adapter(wire)

    with respx.mock:
        route = respx.post(MESSAGES_URL if wire == "messages" else CHAT_URL)
        with pytest.raises(ProviderError) as caught:
            await adapter.send(HELLO, model_id="MiniMax-M2.7", temperature=temperature)
    await adapter.aclose()

    assert caught.value.retryable is False
    assert route.call_count == 0
