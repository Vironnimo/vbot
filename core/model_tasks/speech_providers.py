"""HTTP speech clients for provider-backed task-model targets."""

from __future__ import annotations

import base64
import io
import json
import math
import re
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Mapping
from pathlib import Path
from typing import Any, override
from urllib.parse import quote

import httpx

from core.model_tasks.speech_audio import MAX_SPEECH_BYTES, decode_speech_audio, pcm_to_wav
from core.model_tasks.speech_input import SpeechInputError, TranscriptionInputPolicy
from core.model_tasks.speech_types import (
    SpeechAudioCallback,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.providers._gemini_wire import _gemini_finish_reason, _normalize_gemini_usage
from core.providers.adapter import TERMINAL_OUTCOME_CONTENT_FILTERED
from core.providers.errors import ProviderContentRefusedError, ProviderError
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
# Gemini answers TTS and transcription only once the whole result exists; a
# transcript of up to an hour of audio or minutes of speech take longer.
GOOGLE_SPEECH_TIMEOUT = 600.0
# Gemini Transcribe input media types (aistudio.google.com/docs/transcribe,
# reviewed 2026-11-17), keyed by vBot's prepared audio formats.
_GOOGLE_TRANSCRIPTION_MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mp3",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "webm": "audio/webm",
    "aac": "audio/aac",
    "m4a": "audio/m4a",
}


def rejects_transcription_format(error: ProviderError, sent_format: str) -> bool:
    """Only a definitive input-container rejection can repair a billable request."""
    if type(error) is not ProviderError or error.status_code not in {400, 415, 422}:
        return False
    message = str(error)
    # Prefer the Provider's diagnostic over fields which may echo user input.
    start = message.find("{")
    if start >= 0:
        try:
            payload = json.loads(message[start:])
        except ValueError, RecursionError:
            return False
        detail = payload.get("error", payload.get("detail")) if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            if detail.get("param") not in (
                None,
                "file",
                "audio",
                "input_audio",
                "input_audio.format",
                "mime_type",
                "format",
            ):
                return False
            detail = detail.get("message")
        if not isinstance(detail, str):
            return False
        message = detail
    message = message.lower().strip()
    if re.search(
        r"\b(?:(?:response|output)[ _-]format|time(?:d? out|out)|forbidden|unauthorized"
        r"|access (?:is )?denied|permission denied)\b",
        message,
    ):
        return False
    message = re.sub(
        r"^(?:provider error:\s*)?(?:(?:http\s+)?(?:400|415|422)\b\s*(?:bad request|"
        r"unsupported media type|unprocessable entity)?\s*[:\-]?\s*)?",
        "",
        message,
    )
    # A list of accepted values is advice, never evidence that its entries were rejected.
    diagnostic = (
        re.split(
            r"[.,;]\s*(?:(?:supported|accepted|allowed)\s+(?:audio\s+)?(?:file\s+)?"
            r"(?:formats?|types?)\b|please (?:use|upload)\b)",
            message,
            maxsplit=1,
        )[0]
        .strip()
        .rstrip(".")
    )
    subject = (
        r"(?:(?:input )?audio (?:file )?(?:format|type|codec|encoding)"
        r"|(?:input |file )format|(?:input )?mime[ _-]?type)"
    )
    sent = sent_format.lower()
    if not sent:
        return False
    values = {sent, f"audio/{sent}", f"video/{sent}"}
    values.update(
        {
            "wav": ("audio/x-wav",),
            "mp3": ("audio/mpeg",),
            "m4a": ("audio/mp4",),
            "flac": ("audio/x-flac",),
        }.get(sent, ())
    )
    fmt = r"""[`'"]?(?:""" + "|".join(map(re.escape, sorted(values))) + r""")[`'"]?"""
    value_after = rf"(?:\s*[:=]\s*|\s+){fmt}"
    input_format = rf"(?:{subject}(?:{value_after})?|{fmt}(?: (?:files?|format))?)"
    rejected = r"(?:unsupported|invalid|unrecognized|not (?:supported|accepted)|does not support)"
    return (
        re.fullmatch(
            rf"(?:(?:the|this|provided) )?(?:(?:model )?{rejected}\s+"
            rf"(?:(?:input|provided|requested)\s+)?"
            rf"{input_format}|{input_format}\s+(?:(?:is|are) )?{rejected}(?:{value_after})?)"
            rf"(?: (?:by|for) (?:this|the) (?:model|endpoint))?",
            diagnostic,
        )
        is not None
    )


class ProviderSpeechClient(ProviderTaskClient):
    """Speech HTTP client bound to one target: OpenAI-compatible, Mistral or Google."""

    def transcription_input_policy(self) -> TranscriptionInputPolicy:
        """Return preparation formats and conservative vBot request ceilings.

        OpenAI documents a 25 MB transcription upload limit. OpenRouter's
        larger JSON uploads depend on its selected upstream, which vBot does
        not pin; unknown compatible endpoints have no verified higher limit.
        Both therefore keep the same 25 MB file ceiling. OpenRouter's 36 MB
        JSON ceiling is vBot's bound, including base64 and request options,
        not a claim about every upstream's maximum request size.
        """
        if self._provider.adapter == "google":
            # Google accepts 100 MB of inline data per request; the 25 MB
            # ceiling keeps the Base64 body well below it.
            return TranscriptionInputPolicy(
                accepted_formats=frozenset(_GOOGLE_TRANSCRIPTION_MEDIA_TYPES),
                route=self.request_route,
            )
        if self._provider.id == "openai":
            # OpenAI Audio Transcriptions API reference, reviewed 2026-10-09.
            return TranscriptionInputPolicy(
                route=self.request_route,
                accepted_formats=frozenset(
                    {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"}
                ),
            )
        if self._provider.id == "openrouter":
            # OpenRouter's STT guide lists these input formats; individual
            # upstreams may still reject a format they do not support.
            return TranscriptionInputPolicy(
                route=self.request_route,
                accepted_formats=frozenset({"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}),
                max_request_bytes=36_000_000,
            )
        # Keep the existing WAV/FLAC conversion path for unverified compatible
        # Providers rather than inferring OpenAI's full media contract.
        return TranscriptionInputPolicy(
            accepted_formats=frozenset({"wav", "flac"}), route=self.request_route
        )

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
        if self._provider.adapter == "google":
            return await self._transcribe_google(
                audio, filename=filename, media_type=media_type, options=options
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

        if self._provider.adapter == "google":
            return await self._synthesize_google(text, options=options, on_audio=on_audio)
        if self._provider.id == "mistral":
            return await self._synthesize_mistral(text, options=options, on_audio=on_audio)
        return await self._synthesize_openai_compatible(text, options=options, on_audio=on_audio)

    @override
    async def _observe_response_usage(self, response: httpx.Response) -> Mapping[str, Any] | None:
        if self._provider.adapter != "google":
            return await super()._observe_response_usage(response)
        try:
            payload = response.json()
        except ValueError, UnicodeError:
            return None
        return _normalize_gemini_usage(
            payload.get("usageMetadata") if isinstance(payload, Mapping) else None
        )

    def _google_endpoint(self, method: str) -> str:
        return f"/models/{quote(self._model_id, safe='')}:{method}"

    async def _transcribe_google(
        self, audio: bytes, *, filename: str, media_type: str, options: JsonObject
    ) -> SpeechTranscriptionResult:
        """Transcribe inline audio through ``generateContent``; no prompt is needed."""

        audio_format = audio_format_from(filename=filename, media_type=media_type)
        inline: JsonObject = {
            "mimeType": _GOOGLE_TRANSCRIPTION_MEDIA_TYPES.get(audio_format, "audio/wav"),
            "data": "",
        }
        payload: JsonObject = {"contents": [{"role": "user", "parts": [{"inlineData": inline}]}]}
        if config := _google_transcription_config(options):
            payload["generationConfig"] = {"audioTranscriptionConfig": config}
        merge_extra_options(payload, options)
        inline["data"] = base64.b64encode(audio).decode("ascii")
        return await self.post_and_parse(
            self._google_endpoint("generateContent"),
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            timeout=GOOGLE_SPEECH_TIMEOUT,
            parse=lambda response: _google_transcription_result(response.json()),
            json=payload,
        )

    async def _synthesize_google(
        self, text: str, *, options: JsonObject, on_audio: SpeechAudioCallback | None
    ) -> SpeechSynthesisResult:
        """Synthesize through ``generateContent``, or its SSE stream with *on_audio*.

        The artifact is always WAV: unary 3.8 Models answer WAV, older Models
        and every stream answer headerless little-endian ``audio/l16`` PCM.
        """

        part: JsonObject = {"text": text}
        style = options.get("style")
        if isinstance(style, str) and style.strip():
            part["speech_metadata"] = {"style": style.strip()}
        voice = options.get("voice")
        voice_name = voice.strip() if isinstance(voice, str) and voice.strip() else "kore"
        payload: JsonObject = {
            "contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}},
            },
        }
        merge_extra_options(payload, options)

        def parse(response: httpx.Response) -> SpeechSynthesisResult:
            candidate = _google_candidate(response.json(), final=True)
            pcm = bytearray()
            audio_format: tuple[int, int] | None = None
            for chunk, rate, channels in _google_audio(candidate):
                if audio_format not in (None, (rate, channels)):
                    raise ValueError("Google speech response mixes audio formats")
                audio_format = (rate, channels)
                pcm.extend(chunk)
                if len(pcm) > MAX_SPEECH_BYTES:
                    raise ValueError("Speech response exceeds the audio size limit")
            if audio_format is None or not pcm:
                raise ValueError("Google speech response contains no audio")
            return SpeechSynthesisResult(pcm_to_wav(bytes(pcm), *audio_format), "audio/wav", "wav")

        async def consume(response: httpx.Response) -> SpeechSynthesisResult:
            assert on_audio is not None
            finished = False

            async def chunks() -> AsyncGenerator[tuple[bytes, int, int]]:
                nonlocal finished
                async for raw in iter_sse_data(response):
                    event = parse_sse_json_data(raw, context="Google speech")
                    if not isinstance(event, dict) or "error" in event:
                        raise ValueError("Google speech stream reported an error")
                    # Every event carries the cumulative usage so far.
                    usage = _normalize_gemini_usage(event.get("usageMetadata"))
                    if usage is not None and self._usage_observer is not None:
                        await self._usage_observer.update_latest(usage)
                    candidate = _google_candidate(event, final=False)
                    finished = finished or (
                        candidate is not None and candidate.get("finishReason") == "STOP"
                    )
                    for chunk in _google_audio(candidate):
                        yield chunk

            stream = chunks()
            try:
                first = await anext(stream, None)
                if first is None:
                    raise ValueError("Google speech stream contains no audio")
                _, rate, channels = first

                async def pcm() -> AsyncIterator[bytes]:
                    yield first[0]
                    async for chunk, chunk_rate, chunk_channels in stream:
                        if (chunk_rate, chunk_channels) != (rate, channels):
                            raise ValueError("Google speech stream mixes audio formats")
                        yield chunk

                audio = await decode_speech_audio(
                    pcm(), on_audio, audio_format="pcm", pcm_sample_rate=rate, pcm_channels=channels
                )
            finally:
                await stream.aclose()
            if not finished:
                raise ValueError("Google speech stream ended before completion")
            return SpeechSynthesisResult(pcm_to_wav(audio, rate, channels), "audio/wav", "wav")

        return await self.post_and_parse(
            self._google_endpoint(
                "streamGenerateContent?alt=sse" if on_audio is not None else "generateContent"
            ),
            timeout=GOOGLE_SPEECH_TIMEOUT,
            parse=parse,
            consume=consume if on_audio is not None else None,
            json=payload,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

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
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
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
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
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
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
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


def _google_transcription_config(options: JsonObject) -> JsonObject:
    """Map speech options onto Gemini's ``audioTranscriptionConfig``.

    Google rejects custom vocabulary or smart mode together with word
    timestamps or speaker labels, so those two win and the others are dropped.
    """

    config: JsonObject = {}
    language = options.get("language")
    if isinstance(language, str) and language.strip() and language.strip().lower() != "auto":
        config["languageCodes"] = [language.strip()]
    if options.get("word_timestamps") is True:
        config["wordTimestamp"] = True
    if options.get("diarize") is True:
        config["diarization"] = True
    if config.keys() & {"wordTimestamp", "diarization"}:
        return config
    mode = options.get("mode")
    if isinstance(mode, str) and mode.strip().lower() == "smart":
        config["mode"] = "SMART"
    context = options.get("context_bias")
    terms = context.splitlines() if isinstance(context, str) else []
    if terms := [term.strip() for term in terms if term.strip()]:
        config["customVocabulary"] = terms
    return config


def _google_candidate(payload: Any, *, final: bool) -> Mapping[str, Any] | None:
    """Return the first candidate of a Gemini answer or stream event.

    A blocked prompt or a safety finish is a refusal; any other finish than
    ``STOP``, or none on a *final* answer, leaves the result unconfirmed.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("Google response must be a JSON object")
    candidates = payload.get("candidates")
    candidate = (
        candidates[0]
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping)
        else None
    )
    finish = candidate.get("finishReason") if candidate is not None else None
    feedback = payload.get("promptFeedback")
    outcome = _gemini_finish_reason(finish, has_tool_calls=False, prompt_feedback=feedback)
    if outcome == TERMINAL_OUTCOME_CONTENT_FILTERED:
        reason = finish if isinstance(finish, str) else None
        if reason is None and isinstance(feedback, Mapping):
            reason = feedback.get("blockReason")
        raise ProviderContentRefusedError(f"Google declined the request ({reason})")
    if (final or finish is not None) and finish != "STOP":
        raise ValueError(f"Google response finished with {finish or 'no finish reason'}")
    return candidate


def _google_parts(candidate: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    content = candidate.get("content") if candidate is not None else None
    parts = content.get("parts") if isinstance(content, Mapping) else None
    return [part for part in parts if isinstance(part, Mapping)] if isinstance(parts, list) else []


def _google_audio(candidate: Mapping[str, Any] | None) -> list[tuple[bytes, int, int]]:
    """Decode a candidate's inline audio into ``(pcm16le, rate, channels)`` parts."""

    decoded = []
    for part in _google_parts(candidate):
        inline = part.get("inlineData")
        if not isinstance(inline, Mapping):
            continue
        encoded, mime_type = inline.get("data"), inline.get("mimeType")
        if not isinstance(encoded, str) or not isinstance(mime_type, str):
            raise ValueError("Google speech response has malformed inline audio")
        if len(encoded) > 4 * ((MAX_SPEECH_BYTES + 2) // 3):
            raise ValueError("Speech response exceeds the audio size limit")
        audio = base64.b64decode(encoded, validate=True)
        media_type = mime_type.split(";", 1)[0].strip().lower()
        if media_type in {"audio/wav", "audio/x-wav", "audio/wave"}:
            try:
                with wave.open(io.BytesIO(audio)) as reader:
                    if reader.getsampwidth() != 2:
                        raise ValueError("Google speech WAV is not 16-bit PCM")
                    frames = reader.readframes(reader.getnframes())
                    decoded.append((frames, reader.getframerate(), reader.getnchannels()))
            except (wave.Error, EOFError) as error:
                raise ValueError("Google speech response has an unreadable WAV") from error
        elif media_type in {"audio/l16", "audio/pcm"}:
            # Gemini's L16 is little-endian, unlike RFC 2586 (checked live 2026-11-17).
            decoded.append((audio, *_pcm_format(mime_type, "google")))
        else:
            raise ValueError(f"Google speech response has unsupported audio {media_type}")
    return decoded


def _google_offset(value: Any) -> float | None:
    """Parse a protobuf Duration such as ``"1.200s"``."""

    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        seconds = float(value[:-1])
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _google_transcription_result(payload: Any) -> SpeechTranscriptionResult:
    """Read Gemini Transcribe's ``audioTranscription`` parts.

    Each part is one utterance; with word timestamps or speaker labels it
    becomes a segment with its speaker, word times and outer bounds.
    """

    try:
        candidate = _google_candidate(payload, final=True)
    except ValueError as error:
        raise ProviderError(f"Speech transcription failed: {error}", retryable=False) from error
    texts: list[str] = []
    segments: list[JsonObject] = []
    for part in _google_parts(candidate):
        transcription = part.get("audioTranscription")
        details = transcription if isinstance(transcription, Mapping) else {}
        text = details.get("text", part.get("text"))
        if not isinstance(text, str) or not text.strip():
            continue
        texts.append(text.strip())
        words: list[JsonObject] = []
        raw_words = details.get("words")
        for word in raw_words if isinstance(raw_words, list) else ():
            if not isinstance(word, Mapping):
                continue
            label = word.get("word")
            start = _google_offset(word.get("startOffset"))
            end = _google_offset(word.get("endOffset"))
            if isinstance(label, str) and start is not None and end is not None and start <= end:
                words.append({"word": label, "start": start, "end": end})
        speaker = details.get("speakerLabel")
        if not words and not isinstance(speaker, str):
            continue
        segment: JsonObject = {"text": text.strip()}
        if words:
            segment.update(start=words[0]["start"], end=words[-1]["end"], words=words)
        if isinstance(speaker, str):
            segment["speaker"] = speaker
        segments.append(segment)
    return SpeechTranscriptionResult(
        text="\n".join(texts),
        segments=tuple(segments),
        usage=_normalize_gemini_usage(payload.get("usageMetadata")),
        raw=dict(payload),
    )
