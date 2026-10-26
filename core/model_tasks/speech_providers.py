"""HTTP speech clients for provider-backed task-model targets."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Collection, Mapping
from pathlib import Path
from typing import Any

import httpx

from core.model_tasks.speech_audio import MAX_SPEECH_BYTES, decode_speech_audio, pcm_to_wav
from core.model_tasks.speech_input import SpeechInputError, TranscriptionInputPolicy
from core.model_tasks.speech_types import (
    SpeechAudioCallback,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.providers.errors import ProviderError
from core.providers.http import iter_sse_data, parse_sse_json_data
from core.providers.task_client import (
    EXTRA_OPTIONS_KEY,
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    is_omittable_option,
    merge_extra_options,
)

JsonObject = dict[str, Any]
OPENROUTER_TRANSCRIPTIONS_ENDPOINT = "/audio/transcriptions"
OPENAI_TRANSCRIPTIONS_ENDPOINT = "/audio/transcriptions"
SPEECH_ENDPOINT = "/audio/speech"
DEFAULT_SPEECH_TIMEOUT = 120.0


class ProviderSpeechClient(ProviderTaskClient):
    """Small OpenAI-compatible speech HTTP client bound to one target."""

    def transcription_input_policy(self) -> TranscriptionInputPolicy:
        """Return preparation formats and conservative vBot request ceilings.

        OpenAI documents a 25 MB transcription upload limit. OpenRouter's
        larger JSON uploads depend on its selected upstream, which vBot does
        not pin; unknown compatible endpoints have no verified higher limit.
        Both therefore keep the same 25 MB file ceiling. OpenRouter's 36 MB
        JSON ceiling is vBot's bound, including base64 and request options,
        not a claim about every upstream's maximum request size.
        """
        if self._provider.id == "openai":
            # OpenAI Audio Transcriptions API reference, reviewed 2026-10-09.
            return TranscriptionInputPolicy(
                accepted_formats=frozenset(
                    {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"}
                ),
            )
        if self._provider.id == "openrouter":
            # OpenRouter's STT guide lists these input formats; individual
            # upstreams may still reject a format they do not support.
            return TranscriptionInputPolicy(
                accepted_formats=frozenset({"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}),
                max_request_bytes=36_000_000,
            )
        # Keep the existing WAV/FLAC conversion path for unverified compatible
        # Providers rather than inferring OpenAI's full media contract.
        return TranscriptionInputPolicy(accepted_formats=frozenset({"wav", "flac"}))

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: JsonObject,
    ) -> SpeechTranscriptionResult:
        """Call the selected provider's speech-to-text endpoint."""

        policy = self.transcription_input_policy()
        if len(audio) > policy.max_audio_bytes:
            raise SpeechInputError(
                f"Transcription audio exceeds the target limit of {policy.max_audio_bytes} bytes",
                too_large=True,
            )
        if self._provider.id == "openrouter":
            return await self._transcribe_openrouter(
                audio,
                filename=filename,
                media_type=media_type,
                options=options,
            )
        if self._provider.id == "mistral":
            return await self._transcribe_mistral(
                audio, filename=filename, media_type=media_type, options=options
            )
        return await self._transcribe_openai_compatible(
            audio,
            filename=filename,
            media_type=media_type,
            options=options,
        )

    async def synthesize(
        self, text: str, *, options: JsonObject, on_audio: SpeechAudioCallback | None = None
    ) -> SpeechSynthesisResult:
        """Synthesize speech through the selected Provider's audio endpoint."""

        if self._provider.id == "mistral":
            return await self._synthesize_mistral(text, options=options, on_audio=on_audio)
        return await self._synthesize_openai_compatible(text, options=options, on_audio=on_audio)

    async def _transcribe_mistral(
        self, audio: bytes, *, filename: str, media_type: str, options: JsonObject
    ) -> SpeechTranscriptionResult:
        data: dict[str, str | list[str]] = {"model": self._model_id, "stream": "false"}
        data.update(_mistral_stt_options(options))
        extras = _multipart_extra_options(
            options, protected_fields={*data, "file", "file_id", "file_url"}
        )
        data.update(extras)
        # These are repeated multipart fields, not JSON-encoded arrays.
        extra_values = options.get(EXTRA_OPTIONS_KEY)
        if isinstance(extra_values, dict):
            for key in ("context_bias", "timestamp_granularities"):
                value = extra_values.get(key)
                if key in extras and isinstance(value, list):
                    if not all(isinstance(item, str) for item in value):
                        raise ProviderError(f"{key} must contain strings", retryable=False)
                    data[key] = value
        return await self.post_and_parse(
            OPENAI_TRANSCRIPTIONS_ENDPOINT,
            timeout=DEFAULT_SPEECH_TIMEOUT,
            parse=lambda response: _transcription_result(response.json()),
            data=data,
            files={"file": (filename or "recording.wav", audio, media_type or "audio/wav")},
        )

    async def _synthesize_mistral(
        self, text: str, *, options: JsonObject, on_audio: SpeechAudioCallback | None
    ) -> SpeechSynthesisResult:
        response_format = _response_format(options)
        if response_format not in {"mp3", "wav", "pcm", "flac", "opus"}:
            raise ProviderError("Unsupported Mistral speech format", retryable=False)
        payload: JsonObject = {
            "model": self._model_id,
            "input": text,
            "voice_id": options.get("voice") or "gb_jane_neutral",
            "response_format": response_format,
            "stream": on_audio is not None,
        }
        if instructions := options.get("instructions"):
            payload["instruction"] = instructions
        merge_extra_options(payload, options)

        def result(audio: bytes) -> SpeechSynthesisResult:
            if not audio:
                raise ValueError("Mistral speech response contains no audio")
            if response_format == "pcm":
                # Both Voxtral generations return mono PCM16 at 24 kHz.
                return SpeechSynthesisResult(pcm_to_wav(audio, 24000), "audio/wav", "wav")
            return SpeechSynthesisResult(
                audio, _media_type_for_format(response_format), response_format
            )

        async def audio_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
            async for raw in iter_sse_data(response):
                event = parse_sse_json_data(raw, context="Mistral speech")
                if not isinstance(event, dict):
                    raise ValueError("Mistral speech event must be an object")
                if event.get("type") == "speech.audio.delta":
                    yield _mistral_audio(event)
                elif event.get("type") == "speech.audio.done":
                    usage = event.get("usage")
                    if self._usage_observer is not None and isinstance(usage, Mapping):
                        await self._usage_observer.update_latest(usage)
                    return
                elif event.get("type") == "error" or "error" in event:
                    raise ValueError("Mistral speech stream reported an error")
            raise ValueError("Mistral speech stream ended before completion")

        async def consume(response: httpx.Response) -> SpeechSynthesisResult:
            assert on_audio is not None
            audio = await decode_speech_audio(
                audio_chunks(response),
                on_audio,
                audio_format=response_format,
                pcm_sample_rate=24000 if response_format == "pcm" else None,
            )
            return result(audio)

        return await self.post_and_parse(
            SPEECH_ENDPOINT,
            timeout=DEFAULT_SPEECH_TIMEOUT,
            parse=lambda response: result(_mistral_audio(response.json())),
            consume=consume if on_audio is not None else None,
            json=payload,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    async def _transcribe_openrouter(
        self,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: JsonObject,
    ) -> SpeechTranscriptionResult:
        audio_format = audio_format_from(filename=filename, media_type=media_type)
        payload: JsonObject = {
            "model": self._model_id,
            "input_audio": {
                "data": "",
                "format": audio_format,
            },
        }
        payload.update(_normalized_stt_options(options, provider_id=self._provider.id))
        merge_extra_options(payload, options)
        max_request_bytes = self.transcription_input_policy().max_request_bytes
        if max_request_bytes is not None:
            # Match httpx's JSON encoding, counting every option and UTF-8
            # byte before allocating the base64 string. Base64 needs no JSON
            # escaping, so its exact contribution is known from the byte count.
            encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            request_bytes = sum(len(part.encode("utf-8")) for part in encoder.iterencode(payload))
            request_bytes += 4 * ((len(audio) + 2) // 3)
            if request_bytes > max_request_bytes:
                raise SpeechInputError(
                    f"Transcription request exceeds the limit of {max_request_bytes} bytes",
                    too_large=True,
                )
        payload["input_audio"]["data"] = base64.b64encode(audio).decode("ascii")

        return await self.post_and_parse(
            OPENROUTER_TRANSCRIPTIONS_ENDPOINT,
            timeout=DEFAULT_SPEECH_TIMEOUT,
            parse=lambda response: _transcription_result(response.json()),
            json=payload,
        )

    async def _transcribe_openai_compatible(
        self,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: JsonObject,
    ) -> SpeechTranscriptionResult:
        normalized_filename = filename or f"recording.{audio_format_from(media_type=media_type)}"
        data = {"model": self._model_id}
        data.update(_multipart_stt_options(options))
        data.update(_multipart_extra_options(options, protected_fields={*data, "file"}))
        files = {"file": (normalized_filename, audio, media_type or "application/octet-stream")}

        def _parse(response: httpx.Response) -> SpeechTranscriptionResult:
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/"):
                return SpeechTranscriptionResult(text=response.text)
            return _transcription_result(response.json())

        return await self.post_and_parse(
            OPENAI_TRANSCRIPTIONS_ENDPOINT,
            timeout=DEFAULT_SPEECH_TIMEOUT,
            parse=_parse,
            data=data,
            files=files,
        )

    async def _synthesize_openai_compatible(
        self,
        text: str,
        *,
        options: JsonObject,
        on_audio: SpeechAudioCallback | None = None,
    ) -> SpeechSynthesisResult:
        response_format = _response_format(options)
        payload: JsonObject = {
            "model": self._model_id,
            "input": text,
        }
        payload.update(_normalized_tts_options(options, provider_id=self._provider.id))
        merge_extra_options(payload, options)

        def _result(response: httpx.Response, audio: bytes) -> SpeechSynthesisResult:
            if not audio:
                raise ProviderError(
                    "Speech synthesis response contains no audio",
                    retryable=True,
                )
            media_type = _speech_content_type(response, response_format)
            result_format = response_format
            if media_type.split(";", 1)[0].strip().lower() == "audio/pcm":
                rate, channels = _pcm_format(media_type, self._provider.id)
                audio = pcm_to_wav(audio, rate, channels)
                media_type, result_format = "audio/wav", "wav"
            return SpeechSynthesisResult(
                audio=audio,
                media_type=media_type.split(";", 1)[0],
                format=result_format,
                generation_id=response.headers.get("x-generation-id"),
            )

        async def _consume(response: httpx.Response) -> SpeechSynthesisResult:
            assert on_audio is not None
            content_type = _speech_content_type(response, response_format)
            media_type = content_type.split(";", 1)[0].strip().lower()
            audio_format = {
                "audio/mpeg": "mp3",
                "audio/mp3": "mp3",
                "audio/wav": "wav",
                "audio/x-wav": "wav",
                "audio/ogg": "opus",
                "audio/opus": "opus",
                "audio/aac": "aac",
                "audio/flac": "flac",
                "audio/pcm": "pcm",
            }.get(media_type, response_format)
            rate, channels = (
                _pcm_format(content_type, self._provider.id) if audio_format == "pcm" else (None, 1)
            )
            audio = await decode_speech_audio(
                response.aiter_bytes(),
                on_audio,
                audio_format=audio_format,
                pcm_sample_rate=rate,
                pcm_channels=channels,
            )
            return _result(response, audio)

        return await self.post_and_parse(
            SPEECH_ENDPOINT,
            timeout=DEFAULT_SPEECH_TIMEOUT,
            parse=lambda response: _result(response, response.content),
            consume=_consume if on_audio is not None else None,
            json=payload,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )


def _speech_content_type(response: httpx.Response, response_format: str) -> str:
    content_type: str = response.headers.get("content-type", "")
    media_type, separator, parameters = content_type.partition(";")
    if media_type.strip().lower() in {"", "application/octet-stream", "binary/octet-stream"}:
        return _media_type_for_format(response_format) + (separator + parameters)
    return content_type


def _pcm_format(content_type: str, provider_id: str) -> tuple[int, int]:
    """PCM has no header: use response metadata, or OpenAI's documented format."""
    parameters = {}
    for part in content_type.split(";")[1:]:
        key, separator, value = part.partition("=")
        if separator:
            parameters[key.strip().lower()] = value.strip().strip('"')
    try:
        rate = int(parameters.get("rate", "24000" if provider_id == "openai" else ""))
        channels = int(parameters.get("channels", "1"))
    except ValueError as error:
        raise ValueError(
            "Speech PCM response does not identify its sample rate. "
            "Choose a model and output format that provide audio metadata."
        ) from error
    if not 8000 <= rate <= 192000 or channels not in (1, 2):
        raise ValueError("Speech PCM response has an unsupported audio format")
    return rate, channels


def audio_format_from(filename: str = "", media_type: str = "") -> str:
    """Infer the audio format string expected by provider STT endpoints."""

    media_type_lower = media_type.split(";", 1)[0].lower().strip()
    media_type_formats = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/flac": "flac",
        "audio/mp4": "m4a",
        "audio/m4a": "m4a",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "audio/aac": "aac",
    }
    if media_type_lower in media_type_formats:
        return media_type_formats[media_type_lower]

    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}:
        return suffix
    return "webm"


def _normalized_stt_options(options: JsonObject, *, provider_id: str) -> JsonObject:
    normalized: JsonObject = {}
    language = options.get("language")
    if isinstance(language, str) and language.strip() and language.strip().lower() != "auto":
        normalized["language"] = language.strip()
    temperature = options.get("temperature")
    if isinstance(temperature, int | float) and not isinstance(temperature, bool):
        normalized["temperature"] = float(temperature)
    provider_options = options.get("provider")
    if provider_id == "openrouter" and isinstance(provider_options, dict):
        normalized["provider"] = dict(provider_options)
    return normalized


def _mistral_audio(payload: Any) -> bytes:
    encoded = payload.get("audio_data") if isinstance(payload, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("Mistral speech response is missing audio_data")
    if len(encoded) > 4 * ((MAX_SPEECH_BYTES + 2) // 3):
        raise ValueError("Speech response exceeds the audio size limit")
    audio = base64.b64decode(encoded, validate=True)
    if not audio or len(audio) > MAX_SPEECH_BYTES:
        raise ValueError("Mistral speech response has empty or oversized audio")
    return audio


def _mistral_stt_options(options: JsonObject) -> dict[str, str | list[str]]:
    data: dict[str, str | list[str]] = {}
    language = options.get("language")
    if isinstance(language, str) and language.strip() and language.strip().lower() != "auto":
        data["language"] = language.strip()
    temperature = options.get("temperature")
    if isinstance(temperature, int | float) and not isinstance(temperature, bool):
        data["temperature"] = str(float(temperature))
    context = options.get("context_bias")
    if isinstance(context, str) and context.strip():
        data["context_bias"] = [line.strip() for line in context.splitlines() if line.strip()]
    granularity = options.get("timestamp_granularities")
    granularities = [granularity] if isinstance(granularity, str) and granularity else []
    if options.get("diarize") is True:
        data["diarize"] = "true"
        # Mistral permits exactly one granularity, and diarization requires segment.
        granularities = ["segment"]
    if granularities:
        data["timestamp_granularities"] = granularities
    return data


def _multipart_stt_options(options: JsonObject) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key in ("language", "prompt", "response_format"):
        value = options.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "auto":
            normalized[key] = value.strip()
    temperature = options.get("temperature")
    if isinstance(temperature, int | float) and not isinstance(temperature, bool):
        normalized["temperature"] = str(float(temperature))
    return normalized


def _multipart_extra_options(
    options: JsonObject,
    *,
    protected_fields: Collection[str] = (),
) -> dict[str, str]:
    """Render the ``extra_options`` escape hatch as multipart form fields.

    Multipart values must be strings: scalars are stringified (booleans as
    lowercase JSON literals), containers are JSON-encoded. Empty placeholders
    are dropped like everywhere else.
    """

    extra = options.get(EXTRA_OPTIONS_KEY)
    if not isinstance(extra, dict):
        return {}
    rendered: dict[str, str] = {}
    for key, value in extra.items():
        if not isinstance(key, str) or not key or is_omittable_option(value):
            continue
        if isinstance(value, str):
            rendered[key] = value
        elif isinstance(value, bool):
            rendered[key] = "true" if value else "false"
        elif isinstance(value, int | float):
            rendered[key] = str(value)
        else:
            rendered[key] = json.dumps(value)
    collisions = sorted(set(protected_fields) & rendered.keys())
    if collisions:
        raise ProviderError(
            "extra_options cannot override request fields: " + ", ".join(collisions),
            retryable=False,
        )
    return rendered


def _normalized_tts_options(options: JsonObject, *, provider_id: str) -> JsonObject:
    normalized: JsonObject = {}
    for key in ("voice", "response_format"):
        value = options.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    speed = options.get("speed")
    if isinstance(speed, int | float) and not isinstance(speed, bool):
        normalized["speed"] = float(speed)

    instructions = options.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        if provider_id == "openrouter":
            normalized["provider"] = {
                "options": {
                    "openai": {
                        "instructions": instructions.strip(),
                    }
                }
            }
        else:
            normalized["instructions"] = instructions.strip()
    return normalized


def _response_format(options: JsonObject) -> str:
    value = options.get("response_format")
    return value.strip().lower() if isinstance(value, str) and value.strip() else "mp3"


def _media_type_for_format(audio_format: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "opus": "audio/opus",
        "pcm": "audio/pcm",
    }.get(audio_format, "application/octet-stream")


def _transcription_result(payload: Any) -> SpeechTranscriptionResult:
    if not isinstance(payload, dict):
        raise ProviderError("Speech transcription response must be a JSON object", retryable=False)
    text = payload.get("text")
    if not isinstance(text, str):
        raise ProviderError("Speech transcription response is missing text", retryable=False)
    language = payload.get("language")
    raw_segments = payload.get("segments")
    segments = (
        tuple(segment for segment in raw_segments if isinstance(segment, dict))
        if isinstance(raw_segments, list)
        else ()
    )
    usage = payload.get("usage")
    return SpeechTranscriptionResult(
        text=text,
        language=language if isinstance(language, str) else None,
        segments=segments,
        usage=dict(usage) if isinstance(usage, dict) else None,
        raw=dict(payload),
    )
