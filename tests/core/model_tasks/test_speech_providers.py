"""Tests for provider-backed speech HTTP clients."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.model_tasks.speech_providers import ProviderSpeechClient, audio_format_from
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


def test_audio_format_from_prefers_browser_mime_type() -> None:
    assert audio_format_from(filename="clip.bin", media_type="audio/webm;codecs=opus") == "webm"
    assert audio_format_from(filename="clip.wav", media_type="") == "wav"


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_transcription_sends_base64_json() -> None:
    route = respx.post("https://openrouter.ai/api/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "hello", "usage": {"seconds": 1.2}})
    )
    client = _openrouter_client("openai/gpt-4o-transcribe")

    result = await client.transcribe(
        b"abc",
        filename="clip.webm",
        media_type="audio/webm",
        options={"language": "auto", "temperature": 0},
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "model": "openai/gpt-4o-transcribe",
        "input_audio": {"data": "YWJj", "format": "webm"},
        "temperature": 0.0,
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"
    assert result.text == "hello"
    assert result.usage == {"seconds": 1.2}


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_tts_returns_audio_bytes() -> None:
    route = respx.post("https://openrouter.ai/api/v1/audio/speech").mock(
        return_value=httpx.Response(
            200,
            content=b"audio",
            headers={"content-type": "audio/mpeg", "x-generation-id": "gen_1"},
        )
    )
    client = _openrouter_client("openai/gpt-4o-mini-tts-2025-12-15")

    result = await client.synthesize(
        "hello",
        options={
            "voice": "nova",
            "response_format": "mp3",
            "speed": 1,
            "instructions": "Warm tone.",
        },
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["model"] == "openai/gpt-4o-mini-tts-2025-12-15"
    assert payload["input"] == "hello"
    assert payload["voice"] == "nova"
    assert payload["provider"]["options"]["openai"]["instructions"] == "Warm tone."
    assert result.audio == b"audio"
    assert result.media_type == "audio/mpeg"
    assert result.generation_id == "gen_1"


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_tts_forwards_model_specific_voice_verbatim() -> None:
    # Kokoro advertises 54 voice ids via ``supported_voices``; ``af_aoede`` is
    # not in the OpenAI canonical list and is the kind of value an OpenRouter
    # TTS target would surface through the model-aware schema. The wire must
    # forward whatever the user/model picked without rewriting it.
    route = respx.post("https://openrouter.ai/api/v1/audio/speech").mock(
        return_value=httpx.Response(
            200,
            content=b"audio",
            headers={"content-type": "audio/mpeg"},
        )
    )
    client = _openrouter_client("hexgrad/kokoro-82m")

    result = await client.synthesize(
        "hello",
        options={
            "voice": "af_aoede",
            "response_format": "pcm",
            "speed": 1.25,
        },
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["model"] == "hexgrad/kokoro-82m"
    assert payload["input"] == "hello"
    assert payload["voice"] == "af_aoede"
    assert payload["response_format"] == "pcm"
    assert payload["speed"] == 1.25
    # No provider-options wrapper should leak in when no ``instructions`` are
    # set — the model-specific voice path must not be polluted by other keys.
    assert "provider" not in payload
    assert result.audio == b"audio"
    assert result.media_type == "audio/mpeg"
    assert result.format == "pcm"


def _openrouter_client(model_id: str) -> ProviderSpeechClient:
    provider = ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[],
        extra_headers={"X-Title": "vBot"},
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization", prefix="Bearer ", credential_key="OPENROUTER_API_KEY"
        ),
    )
    return ProviderSpeechClient(
        provider=provider,
        connection=connection,
        credential="sk-test",
        model_id=model_id,
    )
