"""OpenRouter streaming wire client for the ``music_generation`` Task Model."""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx

from core.model_tasks.image_types import ImageInput
from core.model_tasks.music_types import MusicGenerationResult
from core.providers._http_shared import (
    build_streaming_request,
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.errors import NetworkError, ProviderError
from core.providers.task_client import (
    ProviderTaskClient,
    classify_task_response,
    merge_extra_options,
)

JsonObject = dict[str, Any]
MUSIC_ENDPOINT = "/chat/completions"
MUSIC_REQUEST_TIMEOUT_SECONDS = 10 * 60.0
SSE_DONE_MARKER = "[DONE]"


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
        audio_parts: list[str] = []
        transcript_parts: list[str] = []
        seen_done = False
        async with self._stream_response(payload) as response:
            try:
                async for data in iter_sse_data(response):
                    if data.strip() == SSE_DONE_MARKER:
                        seen_done = True
                        break
                    chunk = parse_sse_json_data(data, context="OpenRouter Music generation")
                    _collect_music_delta(chunk, audio_parts, transcript_parts)
                if not seen_done:
                    raise NetworkError("Stream ended without [DONE] marker")
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

        if not audio_parts:
            raise ProviderError("OpenRouter did not return generated music audio.", retryable=False)
        try:
            audio = base64.b64decode("".join(audio_parts), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(
                "OpenRouter did not return generated music audio.",
                retryable=False,
            ) from exc
        if not audio:
            raise ProviderError("OpenRouter did not return generated music audio.", retryable=False)
        return MusicGenerationResult(
            data=audio,
            media_type="audio/mpeg",
            model=self._model_id,
            transcript="".join(transcript_parts).strip(),
        )

    @asynccontextmanager
    async def _stream_response(self, payload: JsonObject) -> AsyncIterator[httpx.Response]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=MUSIC_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            request = build_streaming_request(
                client,
                "POST",
                MUSIC_ENDPOINT,
                json=payload,
                headers=await self._headers(),
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            try:
                if response.status_code >= 400:
                    await response.aread()
                    classify_task_response(response)
                yield response
            finally:
                await response.aclose()


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
        if value is not None:
            payload[name] = value
    merge_extra_options(payload, options)
    payload["stream"] = True
    return payload


def _collect_music_delta(
    chunk: Any,
    audio_parts: list[str],
    transcript_parts: list[str],
) -> None:
    if not isinstance(chunk, Mapping):
        return
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue
        audio = delta.get("audio")
        if not isinstance(audio, Mapping):
            continue
        data = audio.get("data")
        if isinstance(data, str) and data:
            audio_parts.append(data)
        transcript = audio.get("transcript")
        if isinstance(transcript, str) and transcript:
            transcript_parts.append(transcript)
