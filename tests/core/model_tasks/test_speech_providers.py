"""Wire-contract tests for provider-backed speech clients.

Speech-to-text sends base64 JSON to OpenRouter and multipart form data to
OpenAI-compatible Providers; text-to-speech sends the same JSON body to both.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from collections.abc import AsyncIterator
from dataclasses import replace
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import Any, cast, override
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx

from core.model_tasks.speech_input import SpeechInputError
from core.model_tasks.speech_providers import ProviderSpeechClient, audio_format_from
from core.model_tasks.speech_types import SpeechAudioChunk
from core.providers.errors import ProviderError, ProviderOutcomeUnknownError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENAI_BASE = "https://api.openai.com/v1"
MISTRAL_BASE = "https://api.mistral.ai/v1"


def test_audio_format_from_prefers_browser_mime_type() -> None:
    assert audio_format_from(filename="clip.bin", media_type="audio/webm;codecs=opus") == "webm"
    assert audio_format_from(filename="clip.wav", media_type="") == "wav"


@pytest.mark.parametrize(
    ("provider_id", "formats", "max_request_bytes"),
    [
        ("openai", {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"}, None),
        ("openrouter", {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}, 36_000_000),
        ("custom", {"wav", "flac"}, None),
        ("mistral", {"wav", "flac"}, None),
    ],
)
def test_transcription_policy_preserves_only_the_targets_supported_formats(
    provider_id: str, formats: set[str], max_request_bytes: int | None
) -> None:
    policy = _client(provider_id, "test-stt").transcription_input_policy()
    assert policy.accepted_formats == formats
    assert policy.max_audio_bytes == 25_000_000
    assert policy.max_request_bytes == max_request_bytes


@pytest.mark.parametrize("provider_id", ["openai", "openrouter", "custom", "mistral"])
@pytest.mark.parametrize("audio_size", [8, 9], ids=["at-limit", "over-limit"])
@pytest.mark.asyncio
@respx.mock
async def test_transcription_payload_limit_is_checked_before_encoding_or_http(
    provider_id: str, audio_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(provider_id, "test-stt")
    policy = replace(client.transcription_input_policy(), max_audio_bytes=8)
    monkeypatch.setattr(ProviderSpeechClient, "transcription_input_policy", lambda self: policy)
    encode = Mock(wraps=base64.b64encode)
    monkeypatch.setattr("core.model_tasks.speech_providers.base64.b64encode", encode)
    route = respx.post(url__regex=r".*/audio/transcriptions").respond(200, json={"text": "hello"})
    transcription = client.transcribe(
        b"a" * audio_size, filename="recording.wav", media_type="audio/wav", options={}
    )
    if audio_size > policy.max_audio_bytes:
        with pytest.raises(SpeechInputError) as raised:
            await transcription
        assert raised.value.too_large is True
        assert route.call_count == encode.call_count == 0
    else:
        assert (await transcription).text == "hello"
        assert route.call_count == 1


@pytest.mark.parametrize("budget_delta", [0, -1], ids=["at-limit", "over-limit"])
@pytest.mark.asyncio
@respx.mock
async def test_openrouter_request_limit_includes_base64_padding_and_utf8_options(
    budget_delta: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client("openrouter", "test-stt")
    expected = {
        "model": "test-stt",
        "input_audio": {"data": "YWJjZA==", "format": "wav"},
        "note": {"context": "Sprachprüfung 🎙️"},
    }
    # Use the transport's actual encoding as the independent size oracle.
    request_size = len(httpx.Request("POST", OPENROUTER_BASE, json=expected).content)
    policy = replace(
        client.transcription_input_policy(), max_request_bytes=request_size + budget_delta
    )
    monkeypatch.setattr(ProviderSpeechClient, "transcription_input_policy", lambda self: policy)
    encode = Mock(wraps=base64.b64encode)
    monkeypatch.setattr("core.model_tasks.speech_providers.base64.b64encode", encode)
    route = respx.post(f"{OPENROUTER_BASE}/audio/transcriptions").respond(
        200, json={"text": "hello"}
    )
    transcription = client.transcribe(
        b"abcd",
        filename="recording.wav",
        media_type="audio/wav",
        options={"extra_options": {"note": expected["note"]}},
    )
    if budget_delta < 0:
        with pytest.raises(SpeechInputError) as raised:
            await transcription
        assert raised.value.too_large is True
        assert route.call_count == encode.call_count == 0
    else:
        assert (await transcription).text == "hello"
        assert len(route.calls.last.request.content) == request_size
        assert json.loads(route.calls.last.request.content) == expected


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


@pytest.mark.parametrize("diarize", [False, True])
@pytest.mark.asyncio
@respx.mock
async def test_mistral_transcription_sends_repeated_fields_and_normalizes_speaker_timestamps(
    diarize: bool,
) -> None:
    payload = {
        "text": "test transcript",
        "language": "en",
        "segments": [{"text": "test transcript", "start": 0, "end": 1, "speaker_id": "speaker_1"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    route = respx.post(MISTRAL_BASE + "/audio/transcriptions").respond(200, json=payload)
    result = await _client("mistral", "voxtral-mini-2602").transcribe(
        b"recording",
        filename="sample.flac",
        media_type="audio/flac",
        options={
            "language": "auto",
            "temperature": 0,
            "context_bias": " weather \n\nmeeting ",
            "timestamp_granularities": "word",
            "diarize": diarize,
            "extra_options": {"return_language": True},
        },
    )
    expected = {
        "model": "voxtral-mini-2602",
        "stream": "false",
        "temperature": "0.0",
        "context_bias": ["weather", "meeting"],
        "timestamp_granularities": "segment" if diarize else "word",
        "return_language": "true",
        "file": ("sample.flac", "audio/flac", b"recording"),
    }
    if diarize:
        expected["diarize"] = "true"
    assert _form_parts(route.calls.last.request) == expected
    assert result.to_dict() == payload


@pytest.mark.parametrize(
    ("provider_id", "model_id", "field"),
    [
        pytest.param("openrouter", "openai/gpt-4o-transcribe", "model", id="json-model"),
        pytest.param("openai", "whisper-1", "model", id="multipart-model"),
        pytest.param("openai", "whisper-1", "file", id="multipart-file"),
        pytest.param("mistral", "voxtral-mini-latest", "model", id="mistral-model"),
        pytest.param("mistral", "voxtral-mini-latest", "file_url", id="mistral-input"),
        pytest.param("mistral", "voxtral-mini-latest", "stream", id="mistral-stream"),
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


@pytest.mark.asyncio
@respx.mock
async def test_mistral_tts_decodes_json_audio_and_sends_its_own_voice_field() -> None:
    audio = _wav_audio(b"\x01\x00" * 128)
    route = respx.post(MISTRAL_BASE + "/audio/speech").respond(
        200, json={"audio_data": base64.b64encode(audio).decode()}
    )
    result = await _client("mistral", "voxtral-mini-tts-3-0").synthesize(
        "test speech",
        options={
            "voice": "voice_481",
            "response_format": "wav",
            "extra_options": {"expressiveness": 1.2},
        },
    )
    assert (result.audio, result.format, result.media_type) == (audio, "wav", "audio/wav")
    assert json.loads(route.calls.last.request.content) == {
        "model": "voxtral-mini-tts-3-0",
        "input": "test speech",
        "voice_id": "voice_481",
        "response_format": "wav",
        "stream": False,
        "expressiveness": 1.2,
    }


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-audio"),
        pytest.param({"audio_data": ""}, id="empty-audio"),
        pytest.param({"audio_data": "!invalid"}, id="invalid-base64"),
        pytest.param(
            b'data: {"type":"speech.audio.delta","audio_data":"AQABAA=="}\n\n',
            id="unfinished-stream",
        ),
        pytest.param(b'data: {"type":"error"}\n\n', id="stream-error"),
        pytest.param(b"data: []\n\n", id="non-object-event"),
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_mistral_tts_rejects_unusable_results_without_repeating_generation(body: Any) -> None:
    streaming = isinstance(body, bytes)
    response = (
        httpx.Response(
            200, stream=httpx.ByteStream(body), headers={"content-type": "text/event-stream"}
        )
        if streaming
        else httpx.Response(200, json=body)
    )
    route = respx.post(MISTRAL_BASE + "/audio/speech").mock(return_value=response)
    with pytest.raises(ProviderOutcomeUnknownError) as error:
        await _client("mistral", "test-tts").synthesize(
            "test speech",
            options={"response_format": "pcm"},
            on_audio=AsyncMock() if streaming else None,
        )
    assert not error.value.retryable
    assert route.call_count == 1


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
        ("mistral", "application/json", 24000),
    ],
    ids=["openai-pcm", "openai-generic", "openrouter-pcm", "openrouter-generic", "mistral-pcm"],
)
@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streamed"])
@pytest.mark.asyncio
@respx.mock
async def test_pcm_synthesis_returns_a_playable_wav_with_the_original_samples(
    provider_id: str, content_type: str, sample_rate: int, streaming: bool
) -> None:
    pcm = b"\x01\x00\xff\xff" * 128
    body = pcm
    if provider_id == "mistral":
        body = (
            (_speech_event(pcm) + _speech_done())
            if streaming
            else json.dumps({"audio_data": base64.b64encode(pcm).decode()}).encode()
        )
    route = respx.post(url__regex=r".*/audio/speech").mock(
        return_value=httpx.Response(
            200,
            stream=httpx.ByteStream(body),
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
        None if provider_id == "mistral" else "test-generation",
    )
    with wave.open(io.BytesIO(result.audio), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (sample_rate, 1, 2)
        assert wav.readframes(wav.getnframes()) == pcm
    assert route.call_count == 1
    if streaming:
        assert b"".join(chunk.audio for chunk in chunks) == pcm
        assert all(chunk.sample_rate_hz == sample_rate for chunk in chunks)


@pytest.mark.parametrize("outcome", ["completed", "read_failure", "cancelled"])
@pytest.mark.parametrize("provider_id", ["openai", "mistral"])
@pytest.mark.asyncio
@respx.mock
async def test_streamed_tts_plays_before_provider_completion_and_closes_without_replay(
    outcome: str,
    provider_id: str,
) -> None:
    first_pcm, last_pcm = b"\x01\x00" * 32768, b"\xfe\xff" * 1024

    class AudioStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.closed = False

        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield _speech_event(first_pcm) if provider_id == "mistral" else first_pcm
            await self.release.wait()
            if outcome == "read_failure":
                raise httpx.ReadError("test interrupted audio response")
            yield (
                (_speech_event(last_pcm) + _speech_done()) if provider_id == "mistral" else last_pcm
            )

        @override
        async def aclose(self) -> None:
            self.closed = True

    stream = AudioStream()
    route = respx.post(url__regex=r".*/audio/speech").mock(
        return_value=httpx.Response(200, stream=stream, headers={"content-type": "audio/pcm"})
    )
    first_received = asyncio.Event()
    chunks: list[SpeechAudioChunk] = []
    observer = AsyncMock()
    observer.start.return_value = "test-call"

    async def on_audio(chunk: SpeechAudioChunk) -> None:
        chunks.append(chunk)
        first_received.set()

    task = asyncio.create_task(
        _client(provider_id, "test-tts", usage_observer=observer).synthesize(
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
    if provider_id == "mistral" and outcome == "completed":
        observer.update_latest.assert_awaited_once_with(
            {"prompt_tokens": 3, "completion_tokens": 5}
        )


def _form_parts(request: httpx.Request) -> dict[str, object]:
    message = BytesParser(policy=default_policy).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    )
    parts: dict[str, object] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = cast(bytes, part.get_payload(decode=True))
        filename = part.get_filename()
        value = (filename, part.get_content_type(), payload) if filename else payload.decode()
        key = str(name)
        if key in parts:
            previous = parts[key]
            parts[key] = [*previous, value] if isinstance(previous, list) else [previous, value]
        else:
            parts[key] = value
    return parts


def _speech_event(audio: bytes) -> bytes:
    return (
        "data: "
        + json.dumps({"type": "speech.audio.delta", "audio_data": base64.b64encode(audio).decode()})
        + "\n\n"
    ).encode()


def _speech_done() -> bytes:
    return (
        b'data: {"type":"speech.audio.done","usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'
    )


def _wav_audio(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return output.getvalue()


def _client(provider_id: str, model_id: str, **kwargs: Any) -> ProviderSpeechClient:
    provider = ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter=provider_id,
        base_url={"openrouter": OPENROUTER_BASE, "mistral": MISTRAL_BASE}.get(
            provider_id, OPENAI_BASE
        ),
        connections=[],
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
    )
    return ProviderSpeechClient(
        provider=provider, connection=connection, credential="sk-test", model_id=model_id, **kwargs
    )
