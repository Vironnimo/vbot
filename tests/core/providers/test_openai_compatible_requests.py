"""OpenAI-compatible Adapter request construction: headers, output allowance and wire mapping."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from core.providers._http_shared import PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
    request_input_budget,
)
from core.providers.errors import ProviderError
from core.providers.openai_compatible import REASONING_PARAMETER_NAMES
from core.providers.providers import AuthConfig, ConnectionConfig, resolve_request_output_limit
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY
from core.tools import tool_failure, tool_success
from core.utils.tokens import estimate_structured_tokens

from .openai_compatible_test_support import (
    API_KEY,
    MINIMAL_URL,
    MODEL_ID,
    NO_DEFAULTS_CONFIG,
    OPENAI_CONFIG,
    OPENAI_MULTI_AUTH_CONFIG,
    OPENAI_URL,
    OPENROUTER_CONFIG,
    OPENROUTER_URL,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    catalog_model,
    chat_url,
    make_adapter,
    send_request,
    sent_payload,
)

# ---------------------------------------------------------------------------
# Transport, headers and declared wire capabilities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_posts_to_configured_endpoint_with_auth_and_extra_headers() -> None:
    request = await send_request(make_adapter(OPENROUTER_CONFIG), url=OPENROUTER_URL)

    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["http-referer"] == "https://vbot.app"
    assert request.headers["x-title"] == "vBot"
    payload = json.loads(request.content)
    assert payload["model"] == MODEL_ID
    assert payload["messages"] == SAMPLE_MESSAGES
    timeout = request.extensions["timeout"]
    assert timeout["read"] == PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
    assert None not in timeout.values()


_KEYLESS_CONFIG = replace(
    NO_DEFAULTS_CONFIG,
    connections=[
        ConnectionConfig(
            id="local",
            type="none",
            label="Local",
            auth=AuthConfig(header="", prefix="", credential_key=""),
        )
    ],
    extra_headers={"X-Client": "vBot"},
)
_SERVICE_ACCOUNT = OPENAI_MULTI_AUTH_CONFIG.get_connection("service-account")


@pytest.mark.parametrize(
    ("adapter_factory", "url", "expected_headers"),
    [
        pytest.param(
            lambda: make_adapter(NO_DEFAULTS_CONFIG),
            MINIMAL_URL,
            {"x-api-key": API_KEY},
            id="unprefixed-key-header",
        ),
        pytest.param(
            lambda: make_adapter(OPENAI_MULTI_AUTH_CONFIG, auth_config=_SERVICE_ACCOUNT.auth),
            OPENAI_URL,
            {"x-service-token": f"Token {API_KEY}"},
            id="selected-connection",
        ),
        pytest.param(
            lambda: make_adapter(_KEYLESS_CONFIG, token_getter=""),
            MINIMAL_URL,
            {"x-client": "vBot"},
            id="keyless-keeps-extra-headers",
        ),
    ],
)
@pytest.mark.asyncio
async def test_connection_auth_config_selects_the_auth_header(
    adapter_factory, url, expected_headers
) -> None:
    request = await send_request(adapter_factory(), url=url)

    assert {name: request.headers.get(name) for name in expected_headers} == expected_headers
    assert "authorization" not in request.headers


def test_wire_declares_images_and_openai_audio_and_full_history_replay() -> None:
    """The generic wire carries images plus WAV/MP3 but no PDF; subclasses inherit this."""

    adapter = make_adapter(NO_DEFAULTS_CONFIG)  # no wire file refines the protocol default

    assert adapter.wire_media_support(MODEL_ID) == IMAGE_WIRE_MEDIA_TYPES | {
        "audio/wav",
        "audio/mpeg",
    }
    assert adapter.reasoning_replay_policy(MODEL_ID) == REASONING_REPLAY_FULL_HISTORY


@pytest.mark.asyncio
async def test_adapter_closes_its_own_client_on_context_exit() -> None:
    async with make_adapter() as adapter:
        assert not adapter._client.is_closed  # noqa: SLF001 - owned client has no public view

    assert adapter._client.is_closed  # noqa: SLF001


# ---------------------------------------------------------------------------
# Provider defaults, caller kwargs and the output allowance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("temperature", "expected"), [(None, 0.7), (0.0, 0.0), (1.2, 1.2)])
@pytest.mark.asyncio
async def test_caller_kwargs_override_provider_defaults_unless_none(temperature, expected) -> None:
    payload = await sent_payload(make_adapter(), temperature=temperature)

    assert payload["temperature"] == expected
    assert payload["max_tokens"] == 4096


_CEILING_MODEL = catalog_model(max_output_tokens=128_000, context_window=200_000)


@pytest.mark.parametrize(
    ("config", "model", "kwargs", "expected"),
    [
        pytest.param(
            OPENROUTER_CONFIG, _CEILING_MODEL, {}, {"max_tokens": 128_000}, id="model-ceiling"
        ),
        pytest.param(
            OPENROUTER_CONFIG,
            _CEILING_MODEL,
            {"max_tokens": 512},
            {"max_tokens": 512},
            id="explicit",
        ),
        pytest.param(
            OPENROUTER_CONFIG,
            _CEILING_MODEL,
            {"max_tokens": 0},
            {"max_tokens": 128_000},
            id="non-positive-explicit-ignored",
        ),
        pytest.param(
            OPENROUTER_CONFIG,
            _CEILING_MODEL,
            {"max_completion_tokens": 777},
            {"max_completion_tokens": 777},
            id="explicit-alias-suppresses-ceiling-and-config-default",
        ),
        pytest.param(
            OPENROUTER_CONFIG,
            catalog_model(max_output_tokens=None),
            {},
            {"max_tokens": 4096},
            id="config-fallback",
        ),
        pytest.param(NO_DEFAULTS_CONFIG, None, {}, {}, id="unknown-sends-no-limit"),
    ],
)
@pytest.mark.asyncio
async def test_output_allowance_prefers_caller_then_model_ceiling_then_config(
    config, model, kwargs, expected
) -> None:
    payload = await sent_payload(make_adapter(config, model=model), url=chat_url(config), **kwargs)

    assert {key: value for key, value in payload.items() if key not in ("model", "messages")} == (
        expected
    )


@pytest.mark.asyncio
async def test_output_allowance_leaves_room_for_the_tool_inclusive_request() -> None:
    """Regression: Nemo's 256K output ceiling must not consume its whole context."""
    model_id = "nvidia/nemotron-nano-9b-v2:free"
    adapter = make_adapter(
        NO_DEFAULTS_CONFIG,
        model=catalog_model(model_id, max_output_tokens=256_000, context_window=256_000),
    )
    messages = [
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": "x" * 8_000},
    ]
    tools = [
        {
            "name": "large_tool",
            "description": "y" * 24_000,
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    payload = await sent_payload(adapter, messages, url=MINIMAL_URL, model_id=model_id, tools=tools)

    estimated_input = adapter.estimate_request_input_tokens(
        messages, model_id=model_id, tools=tools
    )
    assert estimated_input > adapter.estimate_request_input_tokens(messages, model_id=model_id)
    assert payload["max_tokens"] == resolve_request_output_limit(
        explicit_limit=None,
        model_output_limit=256_000,
        provider_default=None,
        effective_context_window=256_000,
        estimated_input_tokens=estimated_input,
    )
    assert 0 < payload["max_tokens"] < 256_000


@pytest.mark.asyncio
async def test_output_allowance_uses_the_scoped_input_projection_and_separate_reserve() -> None:
    model_id = "nvidia/nemotron-nano-9b-v2:free"
    adapter = make_adapter(
        NO_DEFAULTS_CONFIG,
        model=catalog_model(model_id, max_output_tokens=256_000, context_window=256_000),
    )

    with request_input_budget(model_id, 150_000):
        payload = await sent_payload(
            adapter, [{"role": "user", "content": "x" * 8_000}], url=MINIMAL_URL, model_id=model_id
        )

    # 256k window minus measured input minus the existing 25% output reserve.
    assert payload["max_tokens"] == 68_500
    assert set(payload) == {"model", "messages", "max_tokens"}


def test_request_image_estimate_receives_active_model() -> None:
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
    adapter = make_adapter()

    known = adapter.estimate_request_input_tokens(messages, model_id="gpt-4o")
    fallback = adapter.estimate_request_input_tokens(messages, model_id="unknown")

    assert fallback - known == 4096 - 255


# ---------------------------------------------------------------------------
# Message mapping
# ---------------------------------------------------------------------------


def _wire_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.mark.asyncio
async def test_canonical_history_maps_to_chat_completions_messages() -> None:
    """Canonical-only fields stay local; recovered argument sequences replay as siblings."""
    history: list[dict[str, Any]] = [
        {"role": "system", "model": f"openai/{MODEL_ID}", "content": "You are helpful."},
        {"role": "user", "content": "Weather?"},
        {
            "role": "assistant",
            "model": f"openai/{MODEL_ID}",
            "content": None,
            "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
            "tool_calls": [
                {
                    "id": "call_batch",
                    "name": "get_weather",
                    "arguments": {"city": "Berlin"},
                    "argument_sequence_index": 0,
                    "argument_sequence_length": 2,
                },
                {
                    "id": "tool_call_recovered_1",
                    "name": "get_weather",
                    "arguments": {"city": "Paris"},
                    "argument_sequence_index": 1,
                    "argument_sequence_length": 2,
                },
            ],
        },
        # Non-envelope content (legacy JSON or text) passes through unchanged.
        {
            "role": "tool",
            "tool_call_id": "call_batch",
            "name": "get_weather",
            "content": '{"temp_c": 22}',
        },
        {"role": "tool", "tool_call_id": "tool_call_recovered_1", "content": "18"},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "Done.", "reasoning": "Checked both cities."},
        {"role": "user", "content": "Thanks"},
    ]

    payload = await sent_payload(make_adapter(), history)

    assert payload["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _wire_call("call_batch", "get_weather", '{"city":"Berlin"}'),
                _wire_call("tool_call_recovered_1", "get_weather", '{"city":"Paris"}'),
            ],
            "encrypted_content": "opaque-current-turn",
        },
        {"role": "tool", "tool_call_id": "call_batch", "content": '{"temp_c": 22}'},
        {"role": "tool", "tool_call_id": "tool_call_recovered_1", "content": "18"},
        {"role": "assistant", "content": ""},
        # Without captured metadata the readable text is the replay fallback.
        {"role": "assistant", "content": "Done.", "reasoning_content": "Checked both cities."},
        {"role": "user", "content": "Thanks"},
    ]


@pytest.mark.asyncio
async def test_user_content_parts_map_to_chat_completions_parts_in_order() -> None:
    parts = [
        {"type": "text", "text": "Before"},
        {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/jpeg"},
        {"type": "media", "base64": "d2F2", "media_type": "audio/wav"},
        {"type": "media", "base64": "bXAz", "media_type": "audio/mpeg"},
        {
            "type": "document",
            "base64": "JVBERi0=",
            "media_type": "application/pdf",
            "filename": "report.pdf",
        },
        {"type": "text", "text": "After"},
    ]

    # Unknown-Model media estimates exceed the default window; a catalog window fits them.
    adapter = make_adapter(model=catalog_model(context_window=200_000))

    payload = await sent_payload(adapter, [{"role": "user", "content": parts}])

    assert payload["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Before"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW1hZ2U="}},
                {"type": "input_audio", "input_audio": {"data": "d2F2", "format": "wav"}},
                {"type": "input_audio", "input_audio": {"data": "bXAz", "format": "mp3"}},
                {
                    "type": "file",
                    "file": {
                        "filename": "report.pdf",
                        "file_data": "data:application/pdf;base64,JVBERi0=",
                    },
                },
                {"type": "text", "text": "After"},
            ],
        }
    ]


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            {"type": "media", "base64": None, "media_type": "image/png"}, id="media-without-data"
        ),
        pytest.param(
            {"type": "media", "base64": "aW1n", "media_type": ""}, id="media-without-type"
        ),
        pytest.param(
            {"type": "media", "base64": "b2dn", "media_type": "audio/ogg"},
            id="unsupported-media-type",
        ),
        pytest.param(
            {
                "type": "document",
                "base64": "JVBERi0=",
                "media_type": "application/pdf",
                "filename": "",
            },
            id="document-without-filename",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_content_parts_fail_before_anything_is_sent(part) -> None:
    """Malformed or unsupported parts never degrade into empty or partial wire parts."""
    with respx.mock(assert_all_called=False) as router:
        route = router.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        with pytest.raises(ProviderError) as exc_info:
            await make_adapter().send([{"role": "user", "content": [part]}], model_id=MODEL_ID)

    assert exc_info.value.retryable is False
    assert route.call_count == 0


_ENVELOPE_SHAPED_LITERAL = json.dumps(
    tool_failure("inner", "Literal file content."), separators=(",", ":")
)


@pytest.mark.parametrize("rich", [False, True], ids=["text-only", "media-and-text-blocks"])
@pytest.mark.asyncio
async def test_tool_results_render_once_and_move_media_after_the_result_batch(rich) -> None:
    first_result: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": json.dumps(tool_success({"content": _ENVELOPE_SHAPED_LITERAL})),
    }
    if rich:
        first_result[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = [
            {"type": "media", "base64": "aW1hZ2U=", "media_type": "image/png"},
            {"type": "text", "text": "[Image path: C:/diagram.png]"},
        ]
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "name": "read", "arguments": {}},
                {"id": "call_2", "name": "status", "arguments": {}},
            ],
        },
        first_result,
        {"role": "tool", "tool_call_id": "call_2", "content": json.dumps(tool_success({}))},
    ]
    canonical = json.dumps(messages)
    adapter = make_adapter()

    wire_messages = (await sent_payload(adapter, messages))["messages"]

    expected: list[dict[str, Any]] = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": _ENVELOPE_SHAPED_LITERAL
            + ("\n\n[Image path: C:/diagram.png]" if rich else ""),
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
    ]
    if rich:
        expected.append(
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}}
                ],
            }
        )
    assert wire_messages[1:] == expected
    estimated, _ = estimate_structured_tokens(wire_messages, model_id=MODEL_ID)
    assert adapter.estimate_request_input_tokens(messages, model_id=MODEL_ID) == estimated
    assert json.dumps(messages) == canonical


# ---------------------------------------------------------------------------
# Tool definitions and reasoning controls
# ---------------------------------------------------------------------------

_OPTIONAL_NULLABLE_TOOL = {
    "name": "inspect",
    "description": "Inspect one key.",
    "parameters": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "note": {"type": ["string", "null"]}},
        "required": ["key"],
        "additionalProperties": False,
    },
}


@pytest.mark.parametrize(
    ("config", "strict_field"),
    [
        pytest.param(OPENAI_CONFIG, {"strict": False}, id="openai-explicit-non-strict"),
        pytest.param(NO_DEFAULTS_CONFIG, {}, id="generic-omits-strict"),
    ],
)
@pytest.mark.asyncio
async def test_tool_definitions_keep_the_canonical_schema_and_never_enable_strict(
    config, strict_field
) -> None:
    payload = await sent_payload(
        make_adapter(config), url=chat_url(config), tools=[_OPTIONAL_NULLABLE_TOOL]
    )

    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "inspect",
                "description": "Inspect one key.",
                "parameters": _OPTIONAL_NULLABLE_TOOL["parameters"],
                **strict_field,
            },
        }
    ]


_REASONING_WIRE_FIELDS = (*sorted(REASONING_PARAMETER_NAMES), "thinking", "budget_tokens")


@pytest.mark.parametrize(
    ("config", "model", "kwargs", "expected_effort"),
    [
        pytest.param(OPENAI_CONFIG, None, {"thinking_effort": "minimal"}, "low", id="snaps-up"),
        pytest.param(OPENAI_CONFIG, None, {"thinking_effort": "medium"}, "medium", id="exact"),
        pytest.param(OPENAI_CONFIG, None, {"thinking_effort": "max"}, "high", id="snaps-down"),
        pytest.param(OPENAI_CONFIG, None, {"reasoning_effort": "max"}, "high", id="raw-kwarg"),
        pytest.param(
            OPENAI_CONFIG,
            catalog_model(control="levels", levels=("high", "xhigh")),
            {"thinking_effort": "low"},
            "high",
            id="model-ladder-snaps-up",
        ),
        pytest.param(
            OPENAI_CONFIG,
            catalog_model(control="levels", levels=("high", "xhigh")),
            {"thinking_effort": "max"},
            "xhigh",
            id="model-ladder-beyond-floor",
        ),
        pytest.param(
            OPENAI_CONFIG,
            catalog_model(control="budget"),
            {"thinking_effort": "high"},
            "high",
            id="budget-degrades-to-effort",
        ),
        pytest.param(
            OPENAI_CONFIG,
            catalog_model(),
            {"thinking_effort": "none"},
            "none",
            id="openai-explicit-none",
        ),
        pytest.param(
            NO_DEFAULTS_CONFIG,
            catalog_model(),
            {"thinking_effort": "none"},
            None,
            id="generic-omits-none",
        ),
        pytest.param(
            OPENAI_CONFIG,
            catalog_model(reasoning=False),
            {
                "thinking_effort": "high",
                "reasoning_effort": "high",
                "reasoning": {"effort": "high"},
                "include_reasoning": True,
            },
            None,
            id="catalog-non-reasoning-strips-controls",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reasoning_renders_only_a_snapped_reasoning_effort(
    config, model, kwargs, expected_effort
) -> None:
    payload = await sent_payload(make_adapter(config, model=model), url=chat_url(config), **kwargs)

    sent = {key: payload[key] for key in _REASONING_WIRE_FIELDS if key in payload}
    assert sent == ({"reasoning_effort": expected_effort} if expected_effort else {})
