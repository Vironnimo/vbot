"""OpenRouter streaming wire client for the ``music_generation`` Task Model."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import uuid4

import httpx

from core.attachments import sniff_media_type
from core.model_tasks.image_types import ImageInput
from core.model_tasks.music_types import MusicGenerationResult
from core.providers._http_shared import (
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.errors import (
    NetworkError,
    ProviderContentRefusedError,
    ProviderError,
    ProviderOutcomeUnknownError,
    classify_in_band_provider_error,
)
from core.providers.task_client import (
    ProviderTaskClient,
    classify_task_response,
    content_refusal,
    is_omittable_option,
    merge_extra_options,
)
from core.utils.errors import VBotError

JsonObject = dict[str, Any]
MUSIC_ENDPOINT = "/chat/completions"
MUSIC_REQUEST_TIMEOUT_SECONDS = 10 * 60.0
SSE_DONE_MARKER = "[DONE]"
_MAX_REASON_CHARS = 500


class ProviderMusicClient(ProviderTaskClient):
    """Generate Music through OpenRouter Chat Completions audio streaming."""

    async def generate(
        self,
        prompt: str,
        *,
        options: JsonObject,
        input_images: Sequence[ImageInput] = (),
    ) -> MusicGenerationResult:
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

    @asynccontextmanager
    async def _stream_response(
        self, payload: JsonObject, *, usage: JsonObject
    ) -> AsyncIterator[httpx.Response]:
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
            try:
                response = await client.send(request, stream=True)
                if response.status_code >= 400:
                    await response.aread()
                    refusal = content_refusal(response)
                    if refusal is not None:
                        raise refusal
                    classify_task_response(response)
                yield response
            except httpx.TransportError as exc:
                status = "failed"
                raise wrap_network_error(exc) from exc
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except BaseException:
                status = "failed"
                raise
            finally:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    if observer is not None:
                        await observer.finish(call_id, usage=usage, status=status)


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
        if finish_reason == "content_filter":
            raise ProviderContentRefusedError(self.text()[:_MAX_REASON_CHARS] or None)
        if (finish_reason is not None and finish_reason != "stop") or choice.get(
            "native_finish_reason"
        ) in ("network_error", "server_error"):
            raise ProviderError(f"OpenRouter ended the music stream with {finish_reason}.")
