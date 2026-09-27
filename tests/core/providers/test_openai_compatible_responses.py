"""OpenAI-compatible catalog entries, response normalization, and send() outcomes."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.providers.openai_compatible import (
    REASONING_RESPONSE_FIELD_METADATA_KEY,
    OpenAICompatibleAdapter,
)

from .openai_compatible_test_support import (
    MODEL_ID,
    OPENAI_URL,
    OPENROUTER_CONFIG,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    catalog_model,
    make_adapter,
)

_ADAPTER_LOGGER = "vbot.providers.openai_compatible"


def _response(message: dict[str, Any], **choice_fields: Any) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", **message}, **choice_fields}]}


# ---------------------------------------------------------------------------
# Catalog entries
# ---------------------------------------------------------------------------


def test_catalog_entry_maps_standard_fields_to_model() -> None:
    raw_model = {
        "id": "gpt-4.1",
        "name": "GPT-4.1",
        "context_window": 1047576,
        "max_output_tokens": 32768,
        "supported_parameters": ["response_format", "reasoning_effort"],
        "input_modalities": ["text", "image"],
        "output_modalities": ["text", "image"],
    }

    model = OpenAICompatibleAdapter.normalize_catalog_entry(raw_model, {"max_tokens": 8192})

    assert model.model_id == "gpt-4.1"
    assert model.name == "GPT-4.1"
    assert model.context_window == 1047576
    assert model.max_output_tokens == 32768
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is True
    assert model.capabilities.reasoning.supported is True
    assert model.capabilities.input_modalities == ("text", "image")
    assert model.capabilities.output_modalities == ("text", "image")
    assert model.capabilities.supported_parameters == ("reasoning_effort", "response_format")
    assert "image_generation" in model.capabilities.task_types


def test_catalog_entry_without_optional_fields_keeps_limits_unknown() -> None:
    model = OpenAICompatibleAdapter.normalize_catalog_entry({"id": "minimal-model"}, {})

    assert model.name == "minimal-model"
    # A window-less endpoint leaves the window unknown instead of inventing a 0.
    assert model.context_window is None
    assert model.max_output_tokens is None
    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is False
    assert model.capabilities.reasoning.supported is False
    assert model.capabilities.input_modalities == ("text",)
    assert model.capabilities.output_modalities == ("text",)
    assert model.capabilities.task_types == ("chat", "text_output")


# ---------------------------------------------------------------------------
# normalize_response(): message fields and reasoning sources
# ---------------------------------------------------------------------------


def test_normalize_response_maps_text_tool_calls_and_reasoning_state() -> None:
    reasoning_details = [{"type": "reasoning.text", "text": "opaque"}]
    response = _response(
        {
            "content": None,
            "reasoning": "Need weather.",
            "encrypted_content": "opaque",
            "reasoning_details": reasoning_details,
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'},
                }
            ],
        },
        finish_reason="stop",
    )

    assert make_adapter().normalize_response(response) == {
        "role": "assistant",
        "content": None,
        "reasoning": "Need weather.",
        "reasoning_meta": {"encrypted_content": "opaque", "reasoning_details": reasoning_details},
        "tool_calls": [{"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}],
        # ``stop`` with returned calls still continues the tool loop.
        "terminal_outcome": "tool_calls",
    }


@pytest.mark.parametrize(
    ("aliases", "expected"),
    [
        pytest.param(
            {"reasoning": "", "reasoning_content": "", "reasoning_text": "Visible fallback"},
            "Visible fallback",
            id="skips-empty",
        ),
        pytest.param(
            {"reasoning": "Primary", "reasoning_content": "Duplicate", "reasoning_text": "Dup"},
            "Primary",
            id="first-wins",
        ),
    ],
)
def test_normalize_response_reads_the_first_non_empty_reasoning_alias(aliases, expected) -> None:
    normalized = make_adapter().normalize_response(_response({"content": "Done", **aliases}))

    assert normalized["reasoning"] == expected


def _reasoning_field_model(provider_key: str, field: str):
    return catalog_model(metadata={provider_key: {REASONING_RESPONSE_FIELD_METADATA_KEY: field}})


@pytest.mark.parametrize(
    ("adapter_factory", "model_id", "message", "expected"),
    [
        pytest.param(
            lambda: make_adapter(
                OPENROUTER_CONFIG, model=_reasoning_field_model("openrouter", "deep_thoughts")
            ),
            MODEL_ID,
            {"deep_thoughts": "Catalog-named", "reasoning_content": "Default"},
            "Catalog-named",
            id="catalog-field-wins",
        ),
        pytest.param(
            lambda: make_adapter(
                replace(OPENROUTER_CONFIG, id="opencode-go"),
                model=_reasoning_field_model("opencode_go", "deep_thoughts"),
            ),
            MODEL_ID,
            {"deep_thoughts": "Catalog-named", "reasoning_content": "Default"},
            "Catalog-named",
            id="hyphenated-provider-metadata-key",
        ),
        pytest.param(
            lambda: make_adapter(
                OPENROUTER_CONFIG, model=_reasoning_field_model("openrouter", "encrypted_content")
            ),
            MODEL_ID,
            {"encrypted_content": "opaque", "reasoning_content": "Default"},
            "Default",
            id="meta-field-is-never-readable",
        ),
        pytest.param(
            lambda: make_adapter(
                OPENROUTER_CONFIG, model=_reasoning_field_model("openrouter", "deep_thoughts")
            ),
            None,
            {"deep_thoughts": "Catalog-named", "reasoning_content": "Default"},
            "Default",
            id="no-model-id-default-scan",
        ),
        pytest.param(
            lambda: make_adapter(OPENROUTER_CONFIG, model_lookup={}.get),
            MODEL_ID,
            {"deep_thoughts": "Catalog-named", "reasoning_content": "Default"},
            "Default",
            id="unknown-model-default-scan",
        ),
    ],
)
def test_catalog_named_reasoning_field_is_the_preferred_readable_source(
    adapter_factory, model_id, message, expected
) -> None:
    normalized = adapter_factory().normalize_response(
        _response({"content": "Done", **message}), model_id=model_id
    )

    assert normalized["reasoning"] == expected


# ---------------------------------------------------------------------------
# normalize_response(): terminal outcome and tool-call attempts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("finish_reason", "expected_outcome"),
    [
        ("stop", "stop"),
        ("tool_calls", "tool_calls"),
        ("length", "output_truncated"),
        ("content_filter", "content_filtered"),
        ("network_error", "error"),
        ("provider_added_reason", "unknown"),
    ],
)
def test_normalize_response_maps_the_terminal_outcome(finish_reason, expected_outcome) -> None:
    response = _response({"content": "partial"}, finish_reason=finish_reason)

    assert make_adapter().normalize_response(response)["terminal_outcome"] == expected_outcome


def test_stop_concealing_a_native_network_error_raises_network_error() -> None:
    response = _response(
        {"content": ""}, finish_reason="stop", native_finish_reason="network_error"
    )

    with pytest.raises(NetworkError):
        make_adapter().normalize_response(response)


def _raw_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_malformed_tool_arguments_become_a_rejected_attempt_beside_valid_siblings() -> None:
    response = _response(
        {
            "content": None,
            "tool_calls": [
                _raw_call("call_bad", "get_weather", '{"city":'),
                _raw_call("call_ok", "read_file", '{"path":"README.md"}'),
            ],
        }
    )

    bad, ok = make_adapter().normalize_response(response)["tool_calls"]

    assert (bad["id"], bad["name"], bad["arguments"]) == ("call_bad", "get_weather", {})
    assert bad["rejection"]["code"] == "malformed_tool_arguments"
    assert ok == {"id": "call_ok", "name": "read_file", "arguments": {"path": "README.md"}}


def test_collapsed_single_tool_call_object_is_accepted() -> None:
    response = _response(
        {
            "content": None,
            "tool_calls": {
                "id": "call_one",
                "function": {"name": "search", "arguments": '{"q":"docs"}'},
            },
        },
        finish_reason="tool_calls",
    )

    assert make_adapter().normalize_response(response)["tool_calls"] == [
        {"id": "call_one", "name": "search", "arguments": {"q": "docs"}}
    ]


def test_consecutive_argument_objects_recover_as_sequenced_calls() -> None:
    arguments = '{"command":"echo one"}{"command":"echo two"}'
    response = _response(
        {"content": None, "tool_calls": [_raw_call("call_batch", "bash", arguments)]},
        finish_reason="tool_calls",
    )

    calls = make_adapter().normalize_response(response)["tool_calls"]

    assert [call["arguments"]["command"] for call in calls] == ["echo one", "echo two"]
    assert [call["argument_sequence_index"] for call in calls] == [0, 1]


# ---------------------------------------------------------------------------
# normalize_response(): Usage
# ---------------------------------------------------------------------------

_NO_USAGE = None


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param(
            {"prompt_tokens": 42, "completion_tokens": 13, "total_tokens": 55},
            {"input_tokens": 42, "output_tokens": 13},
            id="primary-counters",
        ),
        pytest.param(
            {"prompt_tokens": 0, "completion_tokens": 0},
            {"input_tokens": 0, "output_tokens": 0},
            id="measured-zero",
        ),
        pytest.param({"prompt_tokens": 100}, {"input_tokens": 100}, id="prompt-only"),
        pytest.param({"completion_tokens": 7}, {"output_tokens": 7}, id="completion-only"),
        pytest.param(
            {"prompt_tokens": True, "completion_tokens": 4}, {"output_tokens": 4}, id="bool-dropped"
        ),
        pytest.param(
            {"prompt_tokens": 12, "completion_tokens": -1},
            {"input_tokens": 12},
            id="negative-dropped",
        ),
        pytest.param(
            {"prompt_tokens": "12", "completion_tokens": 4},
            {"output_tokens": 4},
            id="non-int-dropped",
        ),
        pytest.param(
            {"prompt_tokens": None, "completion_tokens": None}, _NO_USAGE, id="no-usable-counter"
        ),
        pytest.param("not-a-dict", _NO_USAGE, id="not-an-object"),
        pytest.param(
            {
                "prompt_tokens": 42,
                "completion_tokens": 13,
                "prompt_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 5},
                "completion_tokens_details": {"reasoning_tokens": 8},
            },
            {
                "input_tokens": 42,
                "output_tokens": 13,
                "cache_read_tokens": 30,
                "cache_write_tokens": 5,
                "reasoning_tokens": 8,
            },
            id="openai-details",
        ),
        pytest.param(
            {
                "prompt_tokens": 42,
                "completion_tokens": 13,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 5,
            },
            {
                "input_tokens": 42,
                "output_tokens": 13,
                "cache_read_tokens": 30,
                "cache_write_tokens": 5,
            },
            id="top-level-cache-fields",
        ),
        pytest.param(
            {"prompt_tokens": 42, "completion_tokens": 13, "prompt_cache_hit_tokens": 30},
            {"input_tokens": 42, "output_tokens": 13, "cache_read_tokens": 30},
            id="deepseek-cache-hit",
        ),
        pytest.param(
            {
                "prompt_tokens": 42,
                "completion_tokens": 13,
                "prompt_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 4},
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 5,
            },
            {
                "input_tokens": 42,
                "output_tokens": 13,
                "cache_read_tokens": 20,
                "cache_write_tokens": 4,
            },
            id="nested-details-win",
        ),
        pytest.param(
            {
                "prompt_tokens": 42,
                "completion_tokens": 13,
                "prompt_tokens_details": {"cached_tokens": None},
                "cache_read_input_tokens": True,
                "cache_creation_input_tokens": -1,
            },
            {"input_tokens": 42, "output_tokens": 13},
            id="unusable-cache-counters-dropped",
        ),
    ],
)
def test_normalize_response_usage_keeps_only_usable_counters(usage, expected) -> None:
    normalized = make_adapter().normalize_response({**_response({"content": "Hi"}), "usage": usage})

    assert normalized.get("usage") == expected


# ---------------------------------------------------------------------------
# send(): outcomes, error classification and retries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "error_type", "retryable", "attempts"),
    [
        pytest.param(401, "Invalid API key", ProviderAuthError, False, 1, id="401-auth"),
        pytest.param(
            400,
            "Unsupported parameter: 'top_k'",
            ProviderError,
            False,
            1,
            id="400-names-an-unsent-sampling-parameter",
        ),
        pytest.param(500, "Internal Server Error", ProviderError, False, 1, id="500-fatal"),
        pytest.param(429, "Rate limited", ProviderRateLimitError, True, 4, id="429-rate-limit"),
        pytest.param(502, "Bad Gateway", ProviderError, True, 4, id="502-transient"),
    ],
)
@pytest.mark.asyncio
async def test_send_classifies_http_errors_and_keeps_the_provider_detail(
    status, body, error_type, retryable, attempts
) -> None:
    with respx.mock:
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(status, text=body))
        with pytest.raises(ProviderError) as exc_info:
            await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)

    assert exc_info.type is error_type
    assert exc_info.value.retryable is retryable
    assert body in str(exc_info.value)
    assert route.call_count == attempts


@pytest.mark.asyncio
async def test_send_malformed_json_success_body_is_fatal_with_the_decode_error_chained() -> None:
    with respx.mock:
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, text="not-valid-json{"))
        with pytest.raises(ProviderError) as exc_info:
            await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)

    assert exc_info.value.retryable is False
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        pytest.param(httpx.TimeoutException("timed out"), ProviderTimeoutError, id="timeout"),
        pytest.param(httpx.ConnectError("connection failed"), NetworkError, id="connect"),
    ],
)
@pytest.mark.asyncio
async def test_send_transport_failures_are_retried_then_wrapped(failure, error_type) -> None:
    with respx.mock:
        route = respx.post(OPENAI_URL).mock(side_effect=failure)
        with pytest.raises(error_type):
            await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)

    assert route.call_count == 4


@pytest.mark.parametrize(
    "first_failure",
    [
        pytest.param(httpx.Response(503, text="Service Unavailable"), id="503"),
        pytest.param(httpx.ReadError("connection reset"), id="read-error"),
    ],
)
@pytest.mark.asyncio
async def test_send_returns_the_parsed_body_after_a_transient_failure(first_failure) -> None:
    with respx.mock:
        route = respx.post(OPENAI_URL).mock(
            side_effect=[first_failure, httpx.Response(200, json=SUCCESS_RESPONSE)]
        )
        result = await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID)

    assert result == SUCCESS_RESPONSE
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# send(): reasoning observability warnings (decision logic: test_reasoning.py)
# ---------------------------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


@pytest.mark.asyncio
async def test_rejected_effort_warns_and_still_raises_the_fatal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with respx.mock, caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(400, text="invalid value for 'reasoning_effort': 'max'")
        )
        with pytest.raises(ProviderError) as exc_info:
            await make_adapter().send(SAMPLE_MESSAGES, model_id=MODEL_ID, thinking_effort="max")

    assert exc_info.value.retryable is False
    [warning] = _warnings(caplog)
    assert MODEL_ID in warning
    assert "max" in warning


_ZERO_REASONING_USAGE = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "completion_tokens_details": {"reasoning_tokens": 0},
}


@pytest.mark.parametrize(
    ("adapter_factory", "returned", "sent_effort", "warns"),
    [
        pytest.param(make_adapter, {}, "high", True, id="swallowed"),
        pytest.param(
            lambda: make_adapter(model=catalog_model(reasoning=False)),
            {},
            None,
            False,
            id="catalog-non-reasoning-renders-no-effort",
        ),
        pytest.param(
            make_adapter,
            {"reasoning_content": "Thinking it through."},
            "high",
            False,
            id="returned-readable-reasoning",
        ),
        pytest.param(
            make_adapter,
            {"reasoning_details": [{"type": "reasoning.text", "text": "Thinking."}]},
            "high",
            False,
            id="returned-reasoning-details",
        ),
    ],
)
@pytest.mark.asyncio
async def test_zero_reasoning_tokens_warn_only_when_rendered_effort_returned_nothing(
    caplog: pytest.LogCaptureFixture, adapter_factory, returned, sent_effort, warns
) -> None:
    response = {**_response({"content": "Hello!", **returned}), "usage": _ZERO_REASONING_USAGE}
    with respx.mock, caplog.at_level(logging.WARNING, logger=_ADAPTER_LOGGER):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=response))
        await adapter_factory().send(SAMPLE_MESSAGES, model_id=MODEL_ID, thinking_effort="high")

    assert json.loads(route.calls.last.request.content).get("reasoning_effort") == sent_effort
    warnings = _warnings(caplog)
    assert len(warnings) == (1 if warns else 0)
    if warns:
        assert MODEL_ID in warnings[0]
        assert "high" in warnings[0]
