"""Anthropic Adapter request construction: wire mapping, output allowance and caching."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.providers import AnthropicCompatibleAdapter
from core.providers._http_shared import PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES, TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.errors import ProviderError
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_MULTI_AUTH_CONFIG,
    ANTHROPIC_URL,
    API_KEY,
    CANONICAL_MESSAGES_WITH_TOOL_LOOP,
    CUSTOM_CONFIG,
    CUSTOM_URL,
    MINIMAL_URL,
    MODEL_ID,
    NO_DEFAULTS_CONFIG,
    SAMPLE_MESSAGES,
    SAMPLE_TOOLS,
    SUCCESS_RESPONSE,
    THINKING_BLOCK,
    claude_model,
    make_adapter,
    sampling_model,
    send_request,
    sent_payload,
    sse,
    sse_response,
)

EPHEMERAL = {"type": "ephemeral"}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _image(data: str, media_type: str = "image/png") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


# ---------------------------------------------------------------------------
# Transport, headers and Adapter composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_posts_to_configured_messages_endpoint_with_native_headers() -> None:
    request = await send_request(make_adapter(CUSTOM_CONFIG), url=CUSTOM_URL)

    assert request.headers["x-api-key"] == API_KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["x-custom-header"] == "custom-value"
    assert json.loads(request.content)["model"] == MODEL_ID
    assert request.extensions["timeout"]["read"] == PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_selected_connection_auth_replaces_the_api_key_header() -> None:
    oauth = ANTHROPIC_MULTI_AUTH_CONFIG.get_connection("oauth")
    adapter = make_adapter(ANTHROPIC_MULTI_AUTH_CONFIG, auth_config=oauth.auth)

    request = await send_request(adapter)

    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert "x-api-key" not in request.headers


@pytest.mark.asyncio
async def test_compatible_wire_keeps_native_policy_opt_in_and_borrowed_client_open() -> None:
    """Only the native Adapter sends the version header; only the anthropic wire profile adds
    PDF input and cache markers."""
    borrowed = httpx.AsyncClient(base_url="https://minimal.anthropic.example/v1")
    compatible = AnthropicCompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY, client=borrowed)

    request = await send_request(compatible, url=MINIMAL_URL)
    await compatible.aclose()

    body = json.loads(request.content)
    assert request.headers["x-api-key"] == API_KEY
    assert "anthropic-version" not in request.headers
    assert "cache_control" not in body["messages"][-1]["content"][-1]
    assert compatible.wire_media_support(MODEL_ID) == IMAGE_WIRE_MEDIA_TYPES
    assert borrowed.is_closed is False
    await borrowed.aclose()

    native = make_adapter()
    assert native.wire_media_support(MODEL_ID) == IMAGE_WIRE_MEDIA_TYPES | {"application/pdf"}


@pytest.mark.asyncio
async def test_adapter_closes_its_own_client_on_context_exit() -> None:
    async with make_adapter() as adapter:
        assert not adapter._client.is_closed  # noqa: SLF001 - owned client has no public view

    assert adapter._client.is_closed  # noqa: SLF001


# ---------------------------------------------------------------------------
# Provider defaults, caller kwargs and the output allowance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("temperature", "expected"), [(None, 0.7), (0.0, 0.0), (0.3, 0.3)])
@pytest.mark.asyncio
async def test_caller_kwargs_override_provider_defaults_unless_none(temperature, expected) -> None:
    payload = await sent_payload(
        make_adapter(CUSTOM_CONFIG), url=CUSTOM_URL, temperature=temperature
    )

    assert payload["temperature"] == expected


@pytest.mark.parametrize(
    ("config", "url", "model", "kwargs", "expected"),
    [
        pytest.param(NO_DEFAULTS_CONFIG, MINIMAL_URL, claude_model(), {}, 64000, id="ceiling"),
        pytest.param(
            NO_DEFAULTS_CONFIG,
            MINIMAL_URL,
            claude_model(),
            {"max_tokens": 1234},
            1234,
            id="explicit",
        ),
        pytest.param(
            NO_DEFAULTS_CONFIG,
            MINIMAL_URL,
            claude_model(),
            {"max_tokens": 0},
            64000,
            id="non-positive",
        ),
        pytest.param(ANTHROPIC_CONFIG, None, None, {}, 4096, id="config-fallback"),
        pytest.param(NO_DEFAULTS_CONFIG, MINIMAL_URL, None, {}, None, id="unknown"),
    ],
)
@pytest.mark.asyncio
async def test_output_allowance_prefers_caller_then_model_ceiling_then_config(
    config, url, model, kwargs, expected
) -> None:
    adapter = make_adapter(config, model=model)

    payload = await sent_payload(adapter, **({"url": url} if url else {}), **kwargs)

    assert payload.get("max_tokens") == expected


@pytest.mark.parametrize(
    ("config", "url", "kwargs"),
    [
        (ANTHROPIC_CONFIG, None, {"max_tokens": 8192}),
        (CUSTOM_CONFIG, CUSTOM_URL, {}),
    ],
)
@pytest.mark.asyncio
async def test_unknown_model_output_allowance_is_clamped_to_the_context(
    config, url, kwargs
) -> None:
    payload = await sent_payload(make_adapter(config), **({"url": url} if url else {}), **kwargs)

    assert 4096 < payload["max_tokens"] < 8192


def test_request_image_estimate_receives_active_model() -> None:
    adapter = make_adapter()
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": (
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
                        "QVR42mP8/x8AAwMCAO+aXfcAAAAASUVORK5CYII="
                    ),
                }
            ],
        }
    ]

    known = adapter.estimate_request_input_tokens(messages, model_id="claude-sonnet-4-6")
    fallback = adapter.estimate_request_input_tokens(messages, model_id="unknown")

    assert fallback - known == 4096 - 1


# ---------------------------------------------------------------------------
# Message mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_content_maps_to_messages_blocks_in_order() -> None:
    messages = [
        {"role": "user", "content": "Plain text."},
        {"role": "assistant", "content": "Noted."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image:"},
                {"type": "media", "base64": "aW1n", "media_type": "image/jpeg"},
                {
                    "type": "document",
                    "base64": "JVBERi0=",
                    "media_type": "application/pdf",
                    "filename": "report.pdf",
                },
                {"type": "text", "text": "Use one sentence."},
            ],
        },
    ]

    payload = await sent_payload(make_adapter(), messages)

    assert payload["messages"] == [
        {"role": "user", "content": [_text("Plain text.")]},
        {"role": "assistant", "content": [_text("Noted.")]},
        {
            "role": "user",
            "content": [
                _text("Describe this image:"),
                _image("aW1n", "image/jpeg"),
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": "JVBERi0=",
                    },
                },
                _text("Use one sentence."),
            ],
        },
    ]


@pytest.mark.parametrize(
    "block",
    [
        pytest.param({"type": "media", "base64": None, "media_type": "image/png"}, id="no-data"),
        pytest.param({"type": "media", "base64": "aW1n", "media_type": ""}, id="no-media-type"),
        pytest.param(
            {"type": "media", "base64": "YXVkaW8=", "media_type": "audio/wav"}, id="audio"
        ),
        pytest.param(
            {"type": "document", "base64": None, "media_type": "application/pdf"}, id="doc"
        ),
    ],
)
@pytest.mark.asyncio
async def test_malformed_or_unsupported_media_fails_before_the_request(block) -> None:
    with respx.mock:
        route = respx.post(ANTHROPIC_URL)
        with pytest.raises(ProviderError) as exc_info:
            await make_adapter().send([{"role": "user", "content": [block]}], model_id=MODEL_ID)

    assert exc_info.value.retryable is False
    assert not route.called


@pytest.mark.parametrize(
    ("system_contents", "expected"),
    [
        pytest.param([], None, id="none"),
        pytest.param(["Follow the rules."], [_text("Follow the rules.")], id="string"),
        pytest.param(
            ["Follow the rules.", "Keep answers concise."],
            [_text("Follow the rules.\n\nKeep answers concise.")],
            id="strings-joined",
        ),
        pytest.param(
            [[_text("Follow the rules.")], [_text("Keep answers concise.")]],
            [_text("Follow the rules."), _text("Keep answers concise.")],
            id="blocks-concatenated",
        ),
        pytest.param(
            ["Follow the rules.", [_text("Keep answers concise.")]],
            [_text("Follow the rules."), _text("Keep answers concise.")],
            id="mixed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_system_messages_merge_into_top_level_system(system_contents, expected) -> None:
    messages = [{"role": "system", "content": content} for content in system_contents]

    payload = await sent_payload(make_adapter(), [*messages, {"role": "user", "content": "Hi"}])

    assert payload.get("system") == expected
    assert [message["role"] for message in payload["messages"]] == ["user"]


@pytest.mark.asyncio
async def test_native_messages_blocks_pass_through_unchanged() -> None:
    tool_use = {"type": "tool_use", "id": "toolu_01A", "name": "get_weather", "input": {}}
    tool_result = {"type": "tool_result", "tool_use_id": "toolu_01A", "content": "72F"}
    messages = [
        {"role": "assistant", "content": [_text("Let me check."), tool_use]},
        {"role": "user", "content": [tool_result]},
    ]

    payload = await sent_payload(make_adapter(), messages)

    assert payload["messages"] == [
        {"role": "assistant", "content": [_text("Let me check."), tool_use]},
        {"role": "user", "content": [tool_result]},
    ]


# ---------------------------------------------------------------------------
# Tools and Tool Results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_tool_loop_maps_to_messages_wire() -> None:
    payload = await sent_payload(
        make_adapter(),
        CANONICAL_MESSAGES_WITH_TOOL_LOOP,
        tools=SAMPLE_TOOLS,
        thinking_effort="high",
    )

    assert payload["system"] == [_text("You are helpful.")]
    assert payload["messages"] == [
        {"role": "user", "content": [_text("Weather?")]},
        {
            "role": "assistant",
            "content": [
                THINKING_BLOCK,
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "get_weather",
                    "input": {"city": "Berlin"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_abc", "content": '{"temp":22}'}
            ],
        },
    ]
    assert payload["tools"] == [
        {
            "name": "get_weather",
            "description": "Get current weather",
            "input_schema": SAMPLE_TOOLS[0]["parameters"],
        }
    ]
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_tool_input_schema_reaches_the_wire_unchanged() -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "tag": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["query"],
    }

    payload = await sent_payload(
        make_adapter(), tools=[{"name": "search", "description": "Search", "parameters": schema}]
    )

    assert payload["tools"] == [{"name": "search", "description": "Search", "input_schema": schema}]


@pytest.mark.asyncio
async def test_tool_results_share_one_user_message_and_mark_failures() -> None:
    failure = json.dumps(
        {
            "ok": False,
            "error": {"code": "lookup_failed", "message": "Lookup failed."},
            "data": None,
            "artifacts": [],
        },
        separators=(",", ":"),
    )
    success = json.dumps(
        {"ok": True, "error": None, "data": {"value": 1}, "artifacts": []}, separators=(",", ":")
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "toolu_ok", "name": "lookup", "arguments": {}},
                {"id": "toolu_failed", "name": "lookup", "arguments": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_ok", "content": success},
        {"role": "tool", "tool_call_id": "toolu_failed", "content": failure},
        {"role": "user", "content": "Next?"},
    ]

    payload = await sent_payload(make_adapter(), messages)

    assert payload["messages"][1:] == [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_ok", "content": "value: 1"},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_failed",
                    "content": "Error (lookup_failed): Lookup failed.",
                    "is_error": True,
                },
            ],
        },
        {"role": "user", "content": [_text("Next?")]},
    ]


@pytest.mark.asyncio
async def test_tool_result_media_renders_inside_the_tool_result() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "toolu_image",
            "content": '{"ok":true}',
            TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
                {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
                {"type": "text", "text": "[Image path: C:/diagram.png]"},
            ],
        }
    ]

    payload = await sent_payload(make_adapter(), messages)

    assert payload["messages"][0]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_image",
            "content": [
                _text('{"ok":true}'),
                _image("aW1hZ2U="),
                _text("[Image path: C:/diagram.png]"),
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Prompt caching: cache_control breakpoint placement
# ---------------------------------------------------------------------------


def _history(turns: int) -> list[dict]:
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": [_text(f"m{index}")]}
        for index in range(turns)
    ]


def _marked(message: dict) -> bool:
    return "cache_control" in message["content"][-1]


@pytest.mark.asyncio
async def test_prompt_cache_marks_system_and_three_most_recent_messages() -> None:
    """Tools render before system, so the system marker caches both; four markers at most."""
    request = await send_request(
        make_adapter(), [{"role": "system", "content": "Sys"}, *_history(8)], tools=SAMPLE_TOOLS
    )

    body = json.loads(request.content)
    assert body["system"] == [{"type": "text", "text": "Sys", "cache_control": EPHEMERAL}]
    assert all("cache_control" not in tool for tool in body["tools"])
    assert [_marked(message) for message in body["messages"]] == [False] * 5 + [True] * 3


@pytest.mark.asyncio
async def test_prompt_cache_marks_a_short_history_without_system() -> None:
    body = json.loads((await send_request(make_adapter(), _history(2))).content)

    assert "system" not in body
    assert [message["content"][-1].get("cache_control") for message in body["messages"]] == [
        EPHEMERAL,
        EPHEMERAL,
    ]


@pytest.mark.asyncio
async def test_prompt_cache_marker_skips_replayed_thinking_block() -> None:
    request = await send_request(
        make_adapter(), CANONICAL_MESSAGES_WITH_TOOL_LOOP, thinking_effort="high"
    )

    assistant = json.loads(request.content)["messages"][1]
    blocks_by_type = {block["type"]: block for block in assistant["content"]}
    assert "cache_control" not in blocks_by_type["thinking"]
    assert blocks_by_type["tool_use"]["cache_control"] == EPHEMERAL


# ---------------------------------------------------------------------------
# Reasoning, sampling and thinking replay
# ---------------------------------------------------------------------------

ABSENT = object()


def _assert_payload_fields(payload: dict, expected: dict) -> None:
    assert {key: payload.get(key, ABSENT) for key in expected} == expected


@pytest.mark.parametrize(
    ("model", "kwargs", "expected"),
    [
        pytest.param(
            None,
            {"thinking_effort": "high"},
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "high"},
            },
            id="effort-adaptive",
        ),
        pytest.param(
            None,
            {"thinking_effort": "none"},
            {"thinking": {"type": "disabled"}, "output_config": ABSENT},
            id="effort-off",
        ),
        pytest.param(
            claude_model(control="levels", levels=("low", "medium", "high")),
            {"thinking_effort": "max"},
            {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "high"},
            },
            id="effort-snapped-to-model-ladder",
        ),
        pytest.param(
            claude_model(control="budget"),
            {"thinking_effort": "high"},
            {"thinking": {"type": "enabled", "budget_tokens": 16384}, "output_config": ABSENT},
            id="budget",
        ),
        pytest.param(
            claude_model(control="budget"),
            {"thinking_effort": "medium"},
            {"thinking": {"type": "enabled", "budget_tokens": 8192}, "max_tokens": 64000},
            id="budget-keeps-output-headroom",
        ),
        pytest.param(
            claude_model(control="budget", budget_max=40000),
            {"thinking_effort": "medium"},
            {"thinking": {"type": "enabled", "budget_tokens": 20000}},
            id="budget-scaled-by-budget-max",
        ),
        pytest.param(
            claude_model(control="budget"),
            {"thinking_effort": "high", "max_tokens": 4096},
            {"thinking": {"type": "enabled", "budget_tokens": 4095}, "max_tokens": 4096},
            id="budget-under-explicit-max-tokens",
        ),
        pytest.param(
            claude_model(control="budget"),
            {"thinking_effort": "none"},
            {"thinking": {"type": "disabled"}},
            id="budget-off",
        ),
        pytest.param(
            claude_model(control="on_off"),
            {"thinking_effort": "high"},
            {"thinking": {"type": "enabled", "budget_tokens": 1024}},
            id="on-off-floor",
        ),
        pytest.param(
            claude_model(control="on_off"),
            {"thinking_effort": "high", "max_tokens": 1000},
            {"thinking": ABSENT},
            id="on-off-floor-does-not-fit",
        ),
        pytest.param(
            # The wire profile lists the adaptive-only Claude Models by id.
            claude_model(control="levels"),
            {"thinking_effort": "none", "model_id": "claude-opus-4-7"},
            {"thinking": ABSENT},
            id="adaptive-required-off",
        ),
        pytest.param(
            claude_model(reasoning=False),
            {
                "thinking_effort": "high",
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": "high"},
                "include_reasoning": True,
            },
            {"thinking": ABSENT, "output_config": ABSENT, "include_reasoning": ABSENT},
            id="catalog-non-reasoning",
        ),
    ],
)
@pytest.mark.asyncio
async def test_thinking_effort_renders_the_models_wire_control(model, kwargs, expected) -> None:
    payload = await sent_payload(make_adapter(model=model), **kwargs)

    _assert_payload_fields(payload, expected)


@pytest.mark.asyncio
async def test_context_clamped_output_allowance_bounds_the_reasoning_budget() -> None:
    model = claude_model(control="budget", context_window=10_000, max_output_tokens=10_000)
    messages = [{"role": "user", "content": "x" * 8_000}]

    payload = await sent_payload(make_adapter(model=model), messages, thinking_effort="high")

    assert 0 < payload["max_tokens"] < 10_000
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": payload["max_tokens"] - 1}


@pytest.mark.parametrize(
    ("config", "model", "kwargs", "expected"),
    [
        pytest.param(
            ANTHROPIC_CONFIG,
            None,
            {"temperature": 0.5, "thinking_effort": "high"},
            {"temperature": ABSENT},
            id="effort-active",
        ),
        pytest.param(
            ANTHROPIC_CONFIG,
            None,
            {"temperature": 0.5, "thinking": {"type": "enabled", "budget_tokens": 10000}},
            {"temperature": ABSENT, "thinking": {"type": "enabled", "budget_tokens": 10000}},
            id="raw-thinking-active",
        ),
        pytest.param(
            CUSTOM_CONFIG,
            None,
            {"thinking_effort": "high"},
            {"temperature": ABSENT},
            id="default-not-refilled",
        ),
        pytest.param(
            ANTHROPIC_CONFIG,
            None,
            {"temperature": 0.5, "thinking_effort": "none"},
            {"temperature": 0.5},
            id="thinking-disabled",
        ),
        pytest.param(
            ANTHROPIC_CONFIG,
            sampling_model(supports_temperature=False),
            {"temperature": 0.5, "top_p": 0.9, "top_k": 10},
            {"temperature": ABSENT, "top_p": ABSENT, "top_k": ABSENT, "thinking": ABSENT},
            id="model-rejects-sampling",
        ),
        pytest.param(
            ANTHROPIC_CONFIG,
            sampling_model(supports_temperature=True),
            {"temperature": 0.5},
            {"temperature": 0.5},
            id="model-accepts-sampling",
        ),
        pytest.param(
            ANTHROPIC_CONFIG, None, {"temperature": 0.5}, {"temperature": 0.5}, id="unknown-model"
        ),
    ],
)
@pytest.mark.asyncio
async def test_sampling_is_dropped_while_thinking_or_when_the_model_rejects_it(
    config, model, kwargs, expected
) -> None:
    url = CUSTOM_URL if config is CUSTOM_CONFIG else ANTHROPIC_URL

    payload = await sent_payload(make_adapter(config, model=model), url=url, **kwargs)

    _assert_payload_fields(payload, expected)


@pytest.mark.parametrize("transport", ["send", "stream"])
@pytest.mark.asyncio
async def test_rejected_sampling_parameter_is_retried_once_without_it(transport) -> None:
    rejection = httpx.Response(
        400,
        json={
            "error": {
                "type": "invalid_request_error",
                "message": "temperature is not supported for this model",
            }
        },
    )
    success = (
        httpx.Response(200, json=SUCCESS_RESPONSE) if transport == "send" else sse_response(sse())
    )
    adapter = make_adapter()
    kwargs: dict[str, Any] = {"temperature": 0.5, "thinking_effort": "none"}

    with respx.mock:
        route = respx.post(ANTHROPIC_URL).mock(side_effect=[rejection, success])
        if transport == "send":
            await adapter.send(SAMPLE_MESSAGES, model_id=MODEL_ID, **kwargs)
        else:
            [chunk async for chunk in adapter.stream(SAMPLE_MESSAGES, model_id=MODEL_ID, **kwargs)]

    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content)["temperature"] == 0.5
    assert "temperature" not in json.loads(route.calls[1].request.content)


PRIOR_RUN_REDACTED_BLOCK = {"type": "redacted_thinking", "data": "opaque-prior-run-redacted"}


def _prior_run(content: str | None = "A1") -> list[dict]:
    return [
        {"role": "user", "content": "Q1"},
        {
            "role": "assistant",
            "model": f"anthropic/{MODEL_ID}",
            "content": content,
            "reasoning": "Need weather.",
            "reasoning_meta": {"content_blocks": [THINKING_BLOCK, PRIOR_RUN_REDACTED_BLOCK]},
        },
        {"role": "user", "content": "Q2"},
    ]


@pytest.mark.parametrize(
    ("model", "kwargs", "replayed"),
    [
        pytest.param(None, {"thinking_effort": "high"}, True, id="thinking-on"),
        pytest.param(None, {}, True, id="thinking-omitted"),
        pytest.param(None, {"thinking_effort": "none"}, False, id="thinking-disabled"),
        pytest.param(
            claude_model(reasoning=False), {"thinking_effort": "high"}, False, id="non-reasoning"
        ),
    ],
)
@pytest.mark.asyncio
async def test_prior_run_thinking_replays_byte_identical_unless_thinking_is_ruled_out(
    model, kwargs, replayed
) -> None:
    adapter = make_adapter(model=model)

    payload = await sent_payload(adapter, _prior_run(), **kwargs)

    assert adapter.reasoning_replay_policy(MODEL_ID) == REASONING_REPLAY_FULL_HISTORY
    reasoning_blocks = [THINKING_BLOCK, PRIOR_RUN_REDACTED_BLOCK] if replayed else []
    assert payload["messages"][1]["content"] == [*reasoning_blocks, _text("A1")]


@pytest.mark.asyncio
async def test_reasoning_only_turn_is_dropped_when_thinking_is_disabled() -> None:
    """Stripping must not leave an empty assistant content array on the wire."""
    payload = await sent_payload(make_adapter(), _prior_run(content=None), thinking_effort="none")

    assert payload["messages"] == [
        {"role": "user", "content": [_text("Q1")]},
        {"role": "user", "content": [_text("Q2")]},
    ]


@pytest.mark.asyncio
async def test_readable_reasoning_is_never_sent_as_a_thinking_block() -> None:
    messages = [
        {"role": "user", "content": "Previous question"},
        {"role": "assistant", "content": "Previous answer", "reasoning": "Old readable reasoning"},
        {"role": "user", "content": "Fresh follow-up"},
    ]

    payload = await sent_payload(make_adapter(), messages, thinking_effort="high")

    assert payload["messages"][1]["content"] == [_text("Previous answer")]
