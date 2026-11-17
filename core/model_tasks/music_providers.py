"""Wire client for the ``music_generation`` Task Model.

Two routes: OpenRouter's streamed Chat Completions audio and Google's native
Gemini ``generateContent`` (Lyria). Both keep one outcome contract: a refusal
is a known outcome, every other failure after the Provider accepted the
request may follow a billed track, and no request is replayed automatically.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal, override
from urllib.parse import quote
from uuid import uuid4

import httpx

from core.attachments import sniff_media_type
from core.model_tasks.image_types import ImageInput
from core.model_tasks.music_types import MusicGenerationResult
from core.providers._gemini_wire import _gemini_finish_reason, _normalize_gemini_usage
from core.providers.adapter import TERMINAL_OUTCOME_CONTENT_FILTERED, TERMINAL_OUTCOME_STOP
from core.providers.errors import (
    NetworkError,
    ProviderContentRefusedError,
    ProviderError,
    ProviderOutcomeUnknownError,
    classify_in_band_provider_error,
)
from core.providers.http import (
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.openai_compatible import (
    OPENAI_CONTENT_FILTER_FINISH_REASONS,
    OPENAI_NATIVE_TRANSPORT_FINISH_REASONS,
)
from core.providers.task_client import (
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    TaskRequestRetryPolicy,
    classify_task_response_for_retry_policy,
    classify_task_transport_error,
    is_omittable_option,
    merge_extra_options,
)
from core.utils.errors import VBotError

JsonObject = dict[str, Any]
MUSIC_ENDPOINT = "/chat/completions"
MUSIC_REQUEST_TIMEOUT_SECONDS = 10 * 60.0
SSE_DONE_MARKER = "[DONE]"
_MAX_REASON_CHARS = 500
# ``response_format`` option values and the Google audio MIME enum they select.
GOOGLE_MUSIC_AUDIO_FORMATS = {"mp3": "AUDIO_MP3", "wav": "AUDIO_WAV"}
# Google documents 503 UNAVAILABLE as an overloaded service to retry with
# backoff (aistudio.google.com/docs/api-errors, read 2026-11-17), so it proves
# the request was not processed; every other ambiguous failure may follow a
# billed track. The image route shares this reading.
_GOOGLE_MUSIC_RETRY_POLICY = TaskRequestRetryPolicy(
    replay_safe=False,
    verified_safe_retry_status_codes=frozenset({503}),
)


class ProviderMusicClient(ProviderTaskClient):
    """Generate Music through OpenRouter audio streaming or Google ``generateContent``."""

    async def generate(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: Sequence[ImageInput] = (),
    ) -> MusicGenerationResult:
        if self._provider.adapter == "google":
            return await self._generate_google(prompt, options=options, input_images=input_images)
        payload = _music_payload(
            self._model_id,
            prompt,
            options=options,
            input_images=input_images,
        )
        stream = _MusicStream()
        usage: JsonObject = {}
        async with self._stream_response(payload, usage=usage) as response:
            # The provider accepted the request: from here on a failure can follow
            # a finished, billed track, so it is never reported as a plain error.
            try:
                await stream.read(response, usage)
            except ProviderContentRefusedError:
                raise
            except VBotError as exc:
                raise ProviderOutcomeUnknownError(
                    f"the music stream broke off: {exc}", operation_key=uuid4().hex
                ) from exc
            audio = stream.audio()
        media_type = sniff_media_type(audio, "music")
        return MusicGenerationResult(
            data=audio,
            media_type=media_type if media_type.startswith("audio/") else "audio/mpeg",
            model=self._model_id,
            transcript="".join(stream.transcript_parts).strip(),
            text=stream.text(),
        )

    async def _generate_google(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: Sequence[ImageInput],
    ) -> MusicGenerationResult:
        """Generate one track with a single non-streamed ``generateContent`` call.

        Google documents Lyria only on ``generateContent``; the answer carries
        the whole track as an inline audio part beside lyrics or structure text.
        """

        payload = _google_music_payload(prompt, options=options, input_images=input_images)
        model_id = self._model_id
        return await self.post_and_parse(
            f"/models/{quote(model_id, safe='')}:generateContent",
            timeout=MUSIC_REQUEST_TIMEOUT_SECONDS,
            parse=lambda response: _google_music_result(response, model_id),
            json=payload,
            retry_policy=_GOOGLE_MUSIC_RETRY_POLICY,
        )

    @override
    async def _observe_response_usage(self, response: httpx.Response) -> Mapping[str, Any] | None:
        if self._provider.adapter != "google":
            return await super()._observe_response_usage(response)
        try:
            body = response.json()
        except ValueError, UnicodeError:
            return None
        if not isinstance(body, Mapping):
            return None
        return _normalize_gemini_usage(body.get("usageMetadata"))

    @asynccontextmanager
    async def _stream_response(
        self, payload: JsonObject, *, usage: JsonObject
    ) -> AsyncIterator[httpx.Response]:
        operation_key = uuid4().hex
        async with self.http_client(MUSIC_REQUEST_TIMEOUT_SECONDS) as client:
            # Music has no Chat streaming clocks; keep the task's read timeout.
            request = client.build_request(
                "POST",
                MUSIC_ENDPOINT,
                json=payload,
                headers=await self._headers(),
            )
            observer = self._usage_observer
            call_id = await observer.start() if observer is not None else ""
            response = None
            status: Literal["completed", "failed", "cancelled"] = "completed"
            failure: BaseException | None = None
            try:
                response = await client.send(request, stream=True)
                if response.status_code >= 400:
                    await response.aread()
                    classify_task_response_for_retry_policy(
                        response,
                        retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
                        operation_key=operation_key,
                        extra_retryable_status_codes=frozenset(),
                        classify_status=self._classify_status,
                    )
                yield response
            except httpx.TransportError as exc:
                status = "failed"
                failure = classify_task_transport_error(
                    exc,
                    retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
                    operation_key=operation_key,
                )
                raise failure from exc
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except BaseException as exc:
                status = "failed"
                failure = exc
                raise
            finally:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    if observer is not None:
                        await observer.finish(call_id, usage=usage, status=status, failure=failure)


def _music_payload(
    model_id: str,
    prompt: str,
    *,
    options: JsonObject,
    input_images: Sequence[ImageInput],
) -> JsonObject:
    content: str | list[JsonObject] = prompt
    if input_images:
        content = [
            {"type": "text", "text": prompt},
            *[
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{image.media_type};base64,"
                            f"{base64.b64encode(image.data).decode('ascii')}"
                        )
                    },
                }
                for image in input_images
            ],
        ]
    payload: JsonObject = {
        "model": model_id,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["text", "audio"],
        "stream": True,
    }
    for name in ("temperature", "top_p", "seed"):
        value = options.get(name)
        if not is_omittable_option(value):
            payload[name] = value
    merge_extra_options(payload, options)
    return payload


def _google_music_payload(
    prompt: str,
    *,
    options: JsonObject,
    input_images: Sequence[ImageInput],
) -> JsonObject:
    parts: list[JsonObject] = [{"text": prompt}]
    parts.extend(
        {
            "inlineData": {
                "mimeType": image.media_type,
                "data": base64.b64encode(image.data).decode("ascii"),
            }
        }
        for image in input_images
    )
    payload: JsonObject = {"contents": [{"role": "user", "parts": parts}]}
    audio_format = options.get("response_format")
    if not is_omittable_option(audio_format):
        mime_type = GOOGLE_MUSIC_AUDIO_FORMATS.get(str(audio_format))
        if mime_type is None:
            raise ProviderError(
                f"Unsupported Google music response_format: {audio_format}", retryable=False
            )
        payload["generationConfig"] = {
            "responseModalities": ["AUDIO", "TEXT"],
            "responseFormat": {"audio": {"mimeType": mime_type}},
        }
    merge_extra_options(payload, options)
    return payload


def _google_music_result(response: httpx.Response, model_id: str) -> MusicGenerationResult:
    """Read one ``generateContent`` answer: the audio part and the text beside it.

    A blocked prompt, a safety finish or a text-only answer is the Model
    declining. Any other unusable answer raises ``ProviderError``, which the
    non-replayable request policy reports as an unknown outcome.
    """

    body = response.json()
    if not isinstance(body, Mapping):
        raise ProviderError("Google answered the music request with a non-object body.")
    feedback = body.get("promptFeedback")
    if isinstance(feedback, Mapping) and feedback.get("blockReason"):
        raise ProviderContentRefusedError(f"Google blocked the prompt ({feedback['blockReason']})")
    candidates = body.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    if not isinstance(candidate, Mapping):
        raise ProviderError("Google answered the music request without a candidate.")
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, Mapping) else None
    text_parts: list[str] = []
    encoded_audio: list[str] = []
    declared_type = ""
    for part in parts if isinstance(parts, list) else ():
        if not isinstance(part, Mapping) or part.get("thought") is True:
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())
        inline = part.get("inlineData")
        if not isinstance(inline, Mapping):
            continue
        mime_type = inline.get("mimeType")
        if isinstance(mime_type, str) and mime_type.startswith("audio/"):
            data = inline.get("data")
            if not isinstance(data, str):
                raise ProviderError("Google sent a malformed music audio part.")
            encoded_audio.append(data)
            declared_type = declared_type or mime_type
    text = "\n".join(text_parts)
    finish_reason = candidate.get("finishReason")
    outcome = _gemini_finish_reason(finish_reason, has_tool_calls=False)
    if outcome == TERMINAL_OUTCOME_CONTENT_FILTERED:
        message = candidate.get("finishMessage")
        reason = text or (message.strip() if isinstance(message, str) else "")
        raise ProviderContentRefusedError(
            reason[:_MAX_REASON_CHARS] or f"Google stopped the track ({finish_reason})"
        )
    if outcome != TERMINAL_OUTCOME_STOP:
        raise ProviderError(f"Google music finish reason: {finish_reason or 'missing'}.")
    if not encoded_audio:
        if text:
            raise ProviderContentRefusedError(text[:_MAX_REASON_CHARS])
        raise ProviderError("Google answered without music audio or text.")
    try:
        # Each part is complete Base64; decoding them one by one keeps padding valid.
        audio = b"".join(base64.b64decode(data, validate=True) for data in encoded_audio)
    except (binascii.Error, ValueError) as exc:
        raise ProviderError("Google returned undecodable music audio.") from exc
    if not audio:
        raise ProviderError("Google returned empty music audio.")
    sniffed = sniff_media_type(audio, "music")
    return MusicGenerationResult(
        data=audio,
        media_type=sniffed if sniffed.startswith("audio/") else declared_type or "audio/mpeg",
        model=model_id,
        text=text,
    )


class _MusicStream:
    """Collect one streamed Music answer: audio, transcript, and reply text."""

    def __init__(self) -> None:
        self.audio_parts: list[str] = []
        self.transcript_parts: list[str] = []
        self.text_parts: list[str] = []

    async def read(self, response: httpx.Response, usage: JsonObject) -> None:
        try:
            async for data in iter_sse_data(response):
                if data.strip() == SSE_DONE_MARKER:
                    return
                chunk = parse_sse_json_data(data, context="OpenRouter Music generation")
                if isinstance(chunk, Mapping) and isinstance(chunk.get("usage"), Mapping):
                    usage.update(chunk["usage"])
                self._collect(chunk)
        except httpx.TransportError as exc:
            raise wrap_network_error(exc) from exc
        raise NetworkError("Stream ended without [DONE] marker")

    def text(self) -> str:
        return "".join(self.text_parts).strip()

    def audio(self) -> bytes:
        """Return the decoded track; a text-only answer is the Model declining."""

        if not self.audio_parts:
            text = self.text()
            if text:
                raise ProviderContentRefusedError(text[:_MAX_REASON_CHARS])
            raise ProviderOutcomeUnknownError(
                "OpenRouter answered without music audio or text", operation_key=uuid4().hex
            )
        try:
            audio = base64.b64decode("".join(self.audio_parts), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderOutcomeUnknownError(
                "OpenRouter returned undecodable music audio", operation_key=uuid4().hex
            ) from exc
        if not audio:
            raise ProviderOutcomeUnknownError(
                "OpenRouter returned empty music audio", operation_key=uuid4().hex
            )
        return audio

    def _collect(self, chunk: Any) -> None:
        if not isinstance(chunk, Mapping):
            return
        error = chunk.get("error")
        if error is not None:
            raise classify_in_band_provider_error(error)
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if isinstance(choice, Mapping):
                self._collect_choice(choice)

    def _collect_choice(self, choice: Mapping[str, Any]) -> None:
        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            content = delta.get("content")
            if isinstance(content, str) and content:
                self.text_parts.append(content)
            audio = delta.get("audio")
            if isinstance(audio, Mapping):
                data = audio.get("data")
                if data is not None and not isinstance(data, str):
                    raise ProviderError("OpenRouter sent a malformed audio fragment.")
                if isinstance(data, str) and data:
                    self.audio_parts.append(data)
                transcript = audio.get("transcript")
                if isinstance(transcript, str) and transcript:
                    self.transcript_parts.append(transcript)
        finish_reason = choice.get("finish_reason")
        native_reason = choice.get("native_finish_reason")
        # The finishes Chat Completions reads as a content filter or a failed
        # transport mean the same here, but a billed track is never retried.
        if isinstance(finish_reason, str) and finish_reason in OPENAI_CONTENT_FILTER_FINISH_REASONS:
            raise ProviderContentRefusedError(self.text()[:_MAX_REASON_CHARS] or None)
        if (finish_reason is not None and finish_reason != "stop") or (
            isinstance(native_reason, str)
            and native_reason in OPENAI_NATIVE_TRANSPORT_FINISH_REASONS
        ):
            raise ProviderError(f"OpenRouter ended the music stream with {finish_reason}.")
