"""HTTP speech clients for provider-backed task-model targets."""

from __future__ import annotations

import base64
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

import httpx

from core.model_tasks.speech_audio import decode_speech_audio, pcm_to_wav
from core.model_tasks.speech_input import SpeechInputError, TranscriptionInputPolicy
from core.model_tasks.speech_types import (
    SpeechAudioCallback,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.providers.errors import ProviderError
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
        if self._provider.id == "mistral":
            raise ProviderError("Mistral speech execution is not implemented yet", retryable=False)
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
        return await self._transcribe_openai_compatible(
            audio,
            filename=filename,
            media_type=media_type,
            options=options,
        )

    async def synthesize(
        self, text: str, *, options: JsonObject, on_audio: SpeechAudioCallback | None = None
    ) -> SpeechSynthesisResult:
        """Call an OpenAI-compatible text-to-speech endpoint."""

        if self._provider.id == "mistral":
            raise ProviderError("Mistral speech execution is not implemented yet", retryable=False)
        return await self._synthesize_openai_compatible(text, options=options, on_audio=on_audio)

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
