"""HTTP speech clients for provider-backed task-model targets."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from core.model_tasks.speech_types import SpeechSynthesisResult, SpeechTranscriptionResult
from core.providers.errors import ProviderError
from core.providers.task_client import ProviderTaskClient

JsonObject = dict[str, Any]
OPENROUTER_TRANSCRIPTIONS_ENDPOINT = "/audio/transcriptions"
OPENAI_TRANSCRIPTIONS_ENDPOINT = "/audio/transcriptions"
SPEECH_ENDPOINT = "/audio/speech"
DEFAULT_SPEECH_TIMEOUT = 120.0


class ProviderSpeechClient(ProviderTaskClient):
    """Small OpenAI-compatible speech HTTP client bound to one target."""

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: JsonObject,
    ) -> SpeechTranscriptionResult:
        """Call the selected provider's speech-to-text endpoint."""

        if self._provider.id == "mistral":
            raise ProviderError("Mistral speech execution is not implemented yet", retryable=False)
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

    async def synthesize(self, text: str, *, options: JsonObject) -> SpeechSynthesisResult:
        """Call an OpenAI-compatible text-to-speech endpoint."""

        if self._provider.id == "mistral":
            raise ProviderError("Mistral speech execution is not implemented yet", retryable=False)
        return await self._synthesize_openai_compatible(text, options=options)

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
                "data": base64.b64encode(audio).decode("ascii"),
                "format": audio_format,
            },
        }
        payload.update(_normalized_stt_options(options, provider_id=self._provider.id))

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
    ) -> SpeechSynthesisResult:
        response_format = _response_format(options)
        payload: JsonObject = {
            "model": self._model_id,
            "input": text,
        }
        payload.update(_normalized_tts_options(options, provider_id=self._provider.id))

        def _parse(response: httpx.Response) -> SpeechSynthesisResult:
            media_type = response.headers.get("content-type", "")
            if not media_type:
                media_type = _media_type_for_format(response_format)
            return SpeechSynthesisResult(
                audio=response.content,
                media_type=media_type.split(";", 1)[0],
                format=response_format,
                generation_id=response.headers.get("x-generation-id"),
            )

        return await self.post_and_parse(
            SPEECH_ENDPOINT,
            timeout=DEFAULT_SPEECH_TIMEOUT,
            parse=_parse,
            json=payload,
        )


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
