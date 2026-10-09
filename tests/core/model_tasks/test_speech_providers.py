"""Wire-contract tests for provider-backed speech clients.

Speech-to-text sends base64 JSON to OpenRouter and multipart form data to
OpenAI-compatible Providers; text-to-speech sends the same JSON body to both.
"""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import cast, override

import httpx
import pytest
import respx

from core.model_tasks.speech_providers import ProviderSpeechClient, audio_format_from
from core.model_tasks.speech_types import SpeechAudioChunk
from core.providers.errors import ProviderError, ProviderOutcomeUnknownError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENAI_BASE = "https://api.openai.com/v1"


def test_audio_format_from_prefers_browser_mime_type() -> None:
    assert audio_format_from(filename="clip.bin", media_type="audio/webm;codecs=opus") == "webm"
    assert audio_format_from(filename="clip.wav", media_type="") == "wav"


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_transcription_sends_base64_json() -> None:
    route = respx.post(f"{OPENROUTER_BASE}/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "hello", "usage": {"seconds": 1.2}})
    )

    result = await _client("openrouter", "openai/gpt-4o-transcribe").transcribe(
        b"abc",
        filename="clip.webm",
        media_type="audio/webm",
        options={"language": "auto", "temperature": 0, "extra_options": {"beam": 4}},
    )

    request = route.calls.last.request
    # "auto" language is left to the Provider; extra options join the body.
    assert json.loads(request.content) == {
        "model": "openai/gpt-4o-transcribe",
        "input_audio": {"data": "YWJj", "format": "webm"},
        "temperature": 0.0,
        "beam": 4,
    }
    assert request.headers["authorization"] == "Bearer sk-test"
    assert (result.text, result.usage) == ("hello", {"seconds": 1.2})


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(200, json={"text": "hallo"}), id="json-response"),
        # Text response formats come back as the body itself.
        pytest.param(
            httpx.Response(200, text="hallo", headers={"content-type": "text/plain"}),
            id="text-response",
        ),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_multipart_transcription_sends_every_field_as_a_string(
    response: httpx.Response,
) -> None:
    route = respx.post(f"{OPENAI_BASE}/audio/transcriptions").mock(return_value=response)

    result = await _client("openai", "whisper-1").transcribe(
        b"recording",
        filename="",
        media_type="audio/wav",
        options={
            "language": "de",
            "prompt": " Names: vBot ",
            "response_format": "text",
            "temperature": 0,
            "extra_options": {
                "chunking_strategy": {"type": "server_vad"},
                "stream": False,
                "temperature_boost": 1.5,
                "note": "hi",
                "empty": "",
            },
        },
    )

    assert _form_parts(route.calls.last.request) == {
        "model": "whisper-1",
        "language": "de",
        "prompt": "Names: vBot",
        "response_format": "text",
        "temperature": "0.0",
        "chunking_strategy": json.dumps({"type": "server_vad"}),
        "stream": "false",
        "temperature_boost": "1.5",
        "note": "hi",
        # A missing filename is derived from the media type.
        "file": ("recording.wav", "audio/wav", b"recording"),
    }
    assert result.text == "hallo"


@pytest.mark.parametrize(
    ("provider_id", "model_id", "field"),
    [
        pytest.param("openrouter", "openai/gpt-4o-transcribe", "model", id="json-model"),
        pytest.param("openai", "whisper-1", "model", id="multipart-model"),
        pytest.param("openai", "whisper-1", "file", id="multipart-file"),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_transcription_extra_options_cannot_override_request_fields(
    provider_id: str, model_id: str, field: str
) -> None:
    route = respx.post(url__regex=r".*/audio/transcriptions").respond(200, json={"text": "x"})

    with pytest.raises(ProviderError, match=field) as caught:
        await _client(provider_id, model_id).transcribe(
            b"recording",
            filename="recording.wav",
            media_type="audio/wav",
            options={"extra_options": {field: "replacement"}},
        )

    assert caught.value.retryable is False
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("model_id", "options", "headers", "body", "result"),
    [
        # OpenRouter forwards speaking instructions as OpenAI provider options.
        pytest.param(
            "openai/gpt-4o-mini-tts-2025-12-15",
            {
                "voice": "nova",
                "response_format": "mp3",
                "speed": 1,
                "instructions": "Warm tone.",
                "extra_options": {"sample_rate": 44100},
            },
            {"content-type": "audio/mpeg", "x-generation-id": "gen_1"},
            {
                "voice": "nova",
                "response_format": "mp3",
                "speed": 1.0,
                "provider": {"options": {"openai": {"instructions": "Warm tone."}}},
                "sample_rate": 44100,
            },
            ("audio/mpeg", "mp3", "gen_1"),
            id="instructions-and-extra-options",
        ),
        # A model-specific voice outside the OpenAI list is forwarded verbatim.
        pytest.param(
            "hexgrad/kokoro-82m",
            {"voice": "af_aoede", "response_format": "pcm", "speed": 1.25},
            {"content-type": "audio/mpeg"},
            {"voice": "af_aoede", "response_format": "pcm", "speed": 1.25},
            ("audio/mpeg", "pcm", None),
            id="model-specific-voice",
        ),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_openrouter_tts_sends_json_and_returns_audio_bytes(
    model_id: str,
    options: dict[str, object],
    headers: dict[str, str],
    body: dict[str, object],
    result: tuple[str, str, str | None],
) -> None:
    route = respx.post(f"{OPENROUTER_BASE}/audio/speech").mock(
        return_value=httpx.Response(200, content=b"audio", headers=headers)
    )

    synthesized = await _client("openrouter", model_id).synthesize("hello", options=options)

    assert json.loads(route.calls.last.request.content) == {
        "model": model_id,
        "input": "hello",
        **body,
    }
    assert synthesized.audio == b"audio"
    assert (synthesized.media_type, synthesized.format, synthesized.generation_id) == result


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(502, text="invalid upstream response"), id="ambiguous-502"),
        pytest.param(httpx.Response(200, content=b""), id="empty-success"),
        pytest.param(
            httpx.Response(200, content=b"\x00\x01", headers={"content-type": "audio/pcm"}),
            id="pcm-with-unknown-rate",
        ),
        pytest.param(
            httpx.Response(
                200,
                content=b"\x00",
                headers={"content-type": "audio/pcm;rate=22050;channels=1"},
            ),
            id="pcm-with-incomplete-sample",
        ),
    ],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streamed"])
@pytest.mark.asyncio
@respx.mock
async def test_openrouter_tts_never_replays_an_unknown_outcome(
    response: httpx.Response, streaming: bool
) -> None:
    if streaming:
        response = httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(response.content),
        )
    route = respx.post(f"{OPENROUTER_BASE}/audio/speech").mock(return_value=response)
    chunks: list[SpeechAudioChunk] = []

    async def on_audio(chunk: SpeechAudioChunk) -> None:
        chunks.append(chunk)

    with pytest.raises(ProviderOutcomeUnknownError) as raised:
        await _client("openrouter", "openai/gpt-4o-mini-tts").synthesize(
            "hello", options={"voice": "alloy"}, on_audio=on_audio if streaming else None
        )

    assert raised.value.retryable is False
    assert route.call_count == 1
    assert chunks == []


@pytest.mark.parametrize(
    ("provider_id", "content_type", "sample_rate"),
    [
        ("openai", "audio/pcm", 24000),
        ("openai", "application/octet-stream", 24000),
        ("openrouter", "audio/pcm;rate=22050;channels=1", 22050),
        ("openrouter", "application/octet-stream;rate=22050;channels=1", 22050),
    ],
    ids=["openai-pcm", "openai-generic", "openrouter-pcm", "openrouter-generic"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streamed"])
@pytest.mark.asyncio
@respx.mock
async def test_pcm_synthesis_returns_a_playable_wav_with_the_original_samples(
    provider_id: str, content_type: str, sample_rate: int, streaming: bool
) -> None:
    pcm = b"\x01\x00\xff\xff" * 128
    route = respx.post(url__regex=r".*/audio/speech").mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(pcm),
            headers={"content-type": content_type, "x-generation-id": "test-generation"},
        )
    )
    chunks: list[SpeechAudioChunk] = []

    async def on_audio(chunk: SpeechAudioChunk) -> None:
        chunks.append(chunk)

    result = await _client(provider_id, "test-tts").synthesize(
        "hello", options={"response_format": "pcm"}, on_audio=on_audio if streaming else None
    )

    assert (result.media_type, result.format, result.generation_id) == (
        "audio/wav",
        "wav",
        "test-generation",
    )
    with wave.open(io.BytesIO(result.audio), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (sample_rate, 1, 2)
        assert wav.readframes(wav.getnframes()) == pcm
    assert route.call_count == 1
    if streaming:
        assert b"".join(chunk.audio for chunk in chunks) == pcm
        assert all(chunk.sample_rate_hz == sample_rate for chunk in chunks)


@pytest.mark.parametrize("outcome", ["completed", "read_failure", "cancelled"])
@pytest.mark.asyncio
@respx.mock
async def test_streamed_tts_plays_before_provider_completion_and_closes_without_replay(
    outcome: str,
) -> None:
    first_pcm, last_pcm = b"\x01\x00" * 32768, b"\xfe\xff" * 1024

    class AudioStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.closed = False

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield first_pcm
            await self.release.wait()
            if outcome == "read_failure":
                raise httpx.ReadError("test interrupted audio response")
            yield last_pcm

        @override
        async def aclose(self) -> None:
            self.closed = True

    stream = AudioStream()
    route = respx.post(f"{OPENAI_BASE}/audio/speech").mock(
        return_value=httpx.Response(200, stream=stream, headers={"content-type": "audio/pcm"})
    )
    first_received = asyncio.Event()
    chunks: list[SpeechAudioChunk] = []

    async def on_audio(chunk: SpeechAudioChunk) -> None:
        chunks.append(chunk)
        first_received.set()

    task = asyncio.create_task(
        _client("openai", "test-tts").synthesize(
            "hello", options={"response_format": "pcm"}, on_audio=on_audio
        )
    )
    try:
        await first_received.wait()
        assert not task.done() and not stream.closed
        assert chunks[0].audio and chunks[0].sample_rate_hz == 24000
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            stream.release.set()
            if outcome == "read_failure":
                with pytest.raises(ProviderOutcomeUnknownError) as raised:
                    await task
                assert raised.value.retryable is False
            else:
                result = await task
                with wave.open(io.BytesIO(result.audio), "rb") as wav:
                    assert wav.readframes(wav.getnframes()) == first_pcm + last_pcm
                assert b"".join(chunk.audio for chunk in chunks) == first_pcm + last_pcm
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stream.closed and route.call_count == 1


def _form_parts(request: httpx.Request) -> dict[str, object]:
    message = BytesParser(policy=default_policy).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    )
    parts: dict[str, object] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = cast(bytes, part.get_payload(decode=True))
        filename = part.get_filename()
        parts[str(name)] = (
            (filename, part.get_content_type(), payload) if filename else payload.decode()
        )
    return parts


def _client(provider_id: str, model_id: str) -> ProviderSpeechClient:
    provider = ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter=provider_id,
        base_url=OPENROUTER_BASE if provider_id == "openrouter" else OPENAI_BASE,
        connections=[],
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
    )
    return ProviderSpeechClient(
        provider=provider, connection=connection, credential="sk-test", model_id=model_id
    )
