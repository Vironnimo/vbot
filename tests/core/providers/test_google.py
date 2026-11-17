"""Google AI Studio Provider: catalog, native Gemini routing and Google error semantics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.chat.streaming import StreamingAccumulator
from core.model_tasks.decision_profile import decision_profile
from core.model_tasks.image_profile import build_image_profile
from core.model_tasks.options import option_schema_for, validate_task_model_options
from core.providers.adapter_types import ADAPTER_TYPES
from core.providers.errors import (
    CatalogEntrySkipped,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from core.providers.google import GoogleAdapter
from core.providers.providers import ProviderRegistry
from core.providers.wire_profile import DecisionWire
from core.utils.retry import caller_owns_retries

_RESOURCES = Path(__file__).resolve().parents[3] / "resources"
_BASE = "https://generativelanguage.googleapis.com/v1beta"
_MODEL = "gemini-4-hydrogen"
_STREAM_URL = f"{_BASE}/models/{_MODEL}:streamGenerateContent?alt=sse"
_URL = f"{_BASE}/models/{_MODEL}:generateContent"


def _adapter() -> GoogleAdapter:
    config = ProviderRegistry.load(_RESOURCES).get("google")
    assert ADAPTER_TYPES[config.adapter] is GoogleAdapter
    model = GoogleAdapter.normalize_catalog_entry(_entry(_MODEL), config.defaults)
    return GoogleAdapter(config, "google-secret", model_lookup={_MODEL: model}.get)


def _entry(model_id: str, methods: tuple[str, ...] = ("generateContent",)) -> dict[str, Any]:
    return {
        "name": f"models/{model_id}",
        "displayName": "Gemini 4 Hydrogen",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "supportedGenerationMethods": list(methods),
        "thinking": True,
    }


def test_catalog_entry_becomes_a_chat_model_with_listed_limits() -> None:
    model = GoogleAdapter.normalize_catalog_entry(_entry(_MODEL))

    assert model.model_id == _MODEL
    assert model.name == "Gemini 4 Hydrogen"
    assert (model.context_window, model.max_output_tokens) == (1048576, 65536)
    assert model.capabilities.reasoning.supported is True
    assert model.capabilities.tools is True
    assert GoogleAdapter.discovery_params() == {"pageSize": "1000"}


@pytest.mark.parametrize(
    ("model_id", "methods", "task"),
    [
        ("gemini-embedding-2", ("embedContent", "countTokens"), "text_embedding"),
        ("gemini-decision-1", ("createDecision",), "decision"),
        ("gemini-3.8-live", ("bidiGenerateContent",), "live_voice"),
        ("gemini-2.5-flash-native-audio-latest", ("bidiGenerateContent",), "live_voice"),
        ("gemini-3.8-flash-tts", ("generateContent",), "text_to_speech"),
        ("gemini-3.5-transcribe", ("generateContent",), "speech_to_text"),
        ("gemini-3.1-flash-image", ("generateContent",), "image_generation"),
        ("nano-banana-pro-preview", ("generateContent",), "image_generation"),
        ("lyria-3.5", ("generateContent",), "music_generation"),
        ("gemini-omni-1.2-flash", ("generateContent",), "video_generation"),
    ],
)
def test_catalog_classifies_task_models_by_method_and_family(
    model_id: str, methods: tuple[str, ...], task: str
) -> None:
    model = GoogleAdapter.normalize_catalog_entry(_entry(model_id, methods))

    assert task in model.capabilities.task_types
    assert "chat" not in model.capabilities.task_types


@pytest.mark.parametrize(
    ("model_id", "task", "fields"),
    [
        ("gemini-3.8-flash-tts", "text_to_speech", ["voice", "style"]),
        # Older TTS generations reject speech metadata, so they offer no style.
        ("gemini-2.5-flash-preview-tts", "text_to_speech", ["voice"]),
        (
            "gemini-3.5-transcribe",
            "speech_to_text",
            ["language", "mode", "context_bias", "word_timestamps", "diarize"],
        ),
    ],
)
def test_speech_models_publish_their_option_schema(
    model_id: str, task: str, fields: list[str]
) -> None:
    model = GoogleAdapter.normalize_catalog_entry(_entry(model_id))

    schema = option_schema_for(task, "google", f"google/{model_id}::api-key", model=model)

    by_name = {field.name: field for field in schema.fields}
    assert list(by_name) == [*fields, "extra_options"]
    validate_task_model_options(schema, schema.default_options())
    if task == "text_to_speech":
        assert len(model.capabilities.supported_voices) == 30
        assert by_name["voice"].type == "select"
        assert by_name["voice"].default == "kore"
    else:
        assert by_name["language"].default == "auto"
        assert "de-DE" in [choice.value for choice in by_name["language"].options]
        assert [choice.value for choice in by_name["mode"].options] == ["verbatim", "smart"]


@pytest.mark.parametrize(
    ("model_id", "values", "max_sources"),
    [
        pytest.param(
            "gemini-nano-banana-2.1",
            {"aspect_ratio": 14, "resolution": ["1K", "2K", "4K"], "thinking_level": 3},
            14,
            id="nano-banana-2.1",
        ),
        pytest.param(
            "gemini-3.1-flash-image-preview",
            {"aspect_ratio": 14, "resolution": ["512", "1K", "2K", "4K"], "thinking_level": 2},
            14,
            id="flash-image",
        ),
        # One size only, and no documented reference maximum.
        pytest.param(
            "gemini-3.1-flash-lite-image",
            {"aspect_ratio": 10, "thinking_level": 2},
            None,
            id="flash-lite-image",
        ),
        # Thinking is always on and has no level.
        pytest.param(
            "nano-banana-pro-preview",
            {"aspect_ratio": 10, "resolution": ["1K", "2K", "4K"]},
            14,
            id="pro-image",
        ),
        pytest.param("gemini-2.5-flash-image", {"aspect_ratio": 10}, 3, id="2.5-flash-image"),
    ],
)
def test_image_models_publish_the_controls_of_their_family(
    model_id: str, values: dict[str, int | list[str]], max_sources: int | None
) -> None:
    model = GoogleAdapter.normalize_catalog_entry(_entry(model_id))

    schema = option_schema_for(
        "image_generation", "google", f"google/{model_id}::api-key", model=model
    )
    profile = build_image_profile(model, "google")

    offered = {
        field.name: [choice.value for choice in field.options if choice.value]
        for field in schema.fields
        if field.type == "select"
    }
    assert {
        name: len(choices) if isinstance(values[name], int) else choices
        for name, choices in offered.items()
    } == values
    validate_task_model_options(schema, schema.default_options())
    assert profile.max_source_images == max_sources
    assert set(profile.call_choices) <= {"aspect_ratio", "resolution"}


def test_decision_model_publishes_its_documented_limits_and_input_price() -> None:
    model = GoogleAdapter.normalize_catalog_entry(_entry("gemini-decision-1", ("createDecision",)))

    profile = decision_profile(model, DecisionWire(protocol="google_decisions"), "api-key")

    assert (
        profile.max_input_tokens,
        profile.max_questions,
        profile.max_choices,
        profile.max_levels,
    ) == (262144, 1000, 255, 255)
    assert profile.pricing is not None
    assert profile.pricing.rates.input == 0.1
    assert model.max_output_tokens is None


@pytest.mark.parametrize(
    ("model_id", "methods"),
    [
        ("aqa", ("generateAnswer",)),
        ("lyria-realtime-exp", ("bidiGenerateMusic",)),
        ("gemini-3.5-transcribe-live", ("bidiGenerateContent",)),
        ("gemini-3.5-live-translate-preview", ("bidiGenerateContent",)),
        ("gemini-robotics-er-2-streaming-preview", ("bidiGenerateContent",)),
        ("antigravity-preview-latest", ("generateContent",)),
        ("deep-research-preview-04-2026", ("generateContent",)),
    ],
)
def test_catalog_skips_entries_without_a_vbot_task(model_id: str, methods: tuple[str, ...]) -> None:
    with pytest.raises(CatalogEntrySkipped):
        GoogleAdapter.normalize_catalog_entry(_entry(model_id, methods))


@pytest.mark.asyncio
async def test_stream_uses_native_gemini_with_the_google_key_header() -> None:
    chunk = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
    }
    with respx.mock:
        route = respx.post(_STREAM_URL).mock(
            return_value=httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")
        )
        deltas = [
            delta
            async for delta in _adapter().stream(
                [{"role": "user", "content": "hello"}], model_id=_MODEL, reasoning_effort="high"
            )
        ]

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "google-secret"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert body["generationConfig"]["thinkingConfig"] == {
        "includeThoughts": True,
        "thinkingLevel": "high",
    }
    accumulator = StreamingAccumulator()
    for delta in deltas:
        accumulator.add_delta(delta)
    assert accumulator.finalize_assistant_fields().content == "hi"


def _error(status: int, message: str, *details: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": status, "message": message, "details": list(details)}}
    )


_ZERO_QUOTA = _error(
    429,
    "You exceeded your current quota.\n* Quota exceeded for metric: "
    "generate_content_free_tier_requests, limit: 0, model: gemini-4-hydrogen",
)
_DAILY_QUOTA = _error(
    429,
    "You exceeded your current quota.",
    {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
    },
)


@pytest.mark.parametrize(
    ("response", "error_type", "retryable", "retry_after"),
    [
        (
            _error(
                400,
                "API key not valid. Please pass a valid API key.",
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"},
            ),
            ProviderAuthError,
            False,
            None,
        ),
        (_ZERO_QUOTA, ProviderError, False, None),
        (_DAILY_QUOTA, ProviderError, False, None),
        (
            _error(
                429,
                "Resource exhausted.",
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17.25s"},
            ),
            ProviderRateLimitError,
            True,
            17.25,
        ),
        (
            _error(429, "Resource exhausted. Please retry in 41.25s."),
            ProviderRateLimitError,
            True,
            41.25,
        ),
    ],
    ids=["invalid-key", "zero-quota", "daily-quota", "retry-info", "retry-message"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.asyncio
async def test_google_errors_separate_bad_keys_and_exhausted_quota_from_throttling(
    response: httpx.Response,
    error_type: type[ProviderError],
    retryable: bool,
    retry_after: float | None,
    streaming: bool,
) -> None:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hello"}]
    with respx.mock, caller_owns_retries():
        respx.post(_STREAM_URL if streaming else _URL).mock(return_value=response)
        with pytest.raises(error_type) as caught:
            if streaming:
                async for _ in _adapter().stream(messages, model_id=_MODEL):
                    pass
            else:
                await _adapter().send(messages, model_id=_MODEL)

    assert type(caught.value) is error_type
    assert caught.value.retryable is retryable
    assert getattr(caught.value, "retry_after", None) == retry_after
