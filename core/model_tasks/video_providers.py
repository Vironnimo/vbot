"""OpenRouter wire client for the ``video_generation`` Task Model."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from core.model_tasks.image_types import ImageInput
from core.model_tasks.video_types import VideoGenerationResult
from core.providers._http_shared import decode_response_json
from core.providers.errors import ProviderError
from core.providers.task_client import (
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    is_omittable_option,
    merge_extra_options,
)

JsonObject = dict[str, Any]
VIDEO_CREATE_ENDPOINT = "/videos"
VIDEO_REQUEST_TIMEOUT_SECONDS = 60.0
VIDEO_POLL_TIMEOUT_SECONDS = 20 * 60.0
VIDEO_POLL_INTERVAL_SECONDS = 5.0
_TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled", "expired"})


class ProviderVideoClient(ProviderTaskClient):
    """Execute OpenRouter's submit, poll, and download Video workflow."""

    async def generate(
        self,
        prompt: str,
        *,
        options: JsonObject,
        frame_images: Sequence[tuple[str, ImageInput]] = (),
        poll_timeout: float = VIDEO_POLL_TIMEOUT_SECONDS,
        poll_interval: float = VIDEO_POLL_INTERVAL_SECONDS,
    ) -> VideoGenerationResult:
        payload = _video_payload(
            self._model_id,
            prompt,
            options=options,
            frame_images=frame_images,
        )
        created = await self.post_and_parse(
            VIDEO_CREATE_ENDPOINT,
            timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
            json=payload,
            parse=_parse_video_response,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )
        job_id = created.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise ProviderError("OpenRouter did not return a video job id.", retryable=False)

        deadline = time.monotonic() + poll_timeout
        status_payload = created
        while status_payload.get("status") != "completed":
            status = status_payload.get("status")
            if status in _TERMINAL_FAILURE_STATUSES:
                raise ProviderError(
                    f"OpenRouter video generation failed: {_video_error_message(status_payload)}",
                    retryable=False,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    f"OpenRouter video generation timed out after {int(poll_timeout)} seconds.",
                    retryable=False,
                )
            await asyncio.sleep(min(poll_interval, remaining))
            safe_job_id = quote(job_id, safe="")
            status_payload = await self.get_and_parse(
                f"/videos/{safe_job_id}",
                timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
                parse=_parse_video_response,
            )

        safe_job_id = quote(job_id, safe="")
        content, media_type = await self.get_and_parse(
            f"/videos/{safe_job_id}/content?index=0",
            timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
            parse=_parse_video_content,
        )
        usage = status_payload.get("usage")
        return VideoGenerationResult(
            data=content,
            media_type=media_type,
            model=self._model_id,
            job_id=job_id,
            usage=dict(usage) if isinstance(usage, Mapping) else None,
            raw=dict(status_payload),
        )


def _video_payload(
    model_id: str,
    prompt: str,
    *,
    options: JsonObject,
    frame_images: Sequence[tuple[str, ImageInput]],
) -> JsonObject:
    payload: JsonObject = {"model": model_id, "prompt": prompt}
    size = options.get("size")
    if not is_omittable_option(size):
        payload["size"] = size
    for name in ("resolution", "aspect_ratio", "generate_audio", "seed"):
        if "size" in payload and name in {"resolution", "aspect_ratio"}:
            continue
        value = options.get(name)
        if not is_omittable_option(value):
            payload[name] = value
    duration = options.get("duration")
    if isinstance(duration, str) and duration.isdigit():
        payload["duration"] = int(duration)
    elif isinstance(duration, int) and not isinstance(duration, bool):
        payload["duration"] = duration
    provider_options = options.get("provider_options")
    if isinstance(provider_options, dict) and provider_options:
        payload["provider"] = {"options": provider_options}
    if frame_images:
        payload["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{image.media_type};base64,"
                        f"{base64.b64encode(image.data).decode('ascii')}"
                    )
                },
                "frame_type": frame_type,
            }
            for frame_type, image in frame_images
        ]
    merge_extra_options(payload, options)
    return payload


def _parse_video_response(response: httpx.Response) -> JsonObject:
    payload = decode_response_json(response, "OpenRouter video generation")
    if not isinstance(payload, Mapping):
        raise ProviderError("OpenRouter did not return a video job id.", retryable=False)
    return dict(payload)


def _parse_video_content(response: httpx.Response) -> tuple[bytes, str]:
    content = response.content
    if not content:
        raise ProviderError("OpenRouter did not return generated video content.", retryable=False)
    media_type = response.headers.get("content-type", "video/mp4").split(";", 1)[0].strip()
    if not media_type.startswith("video/"):
        media_type = "video/mp4"
    return content, media_type


def _video_error_message(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, str) and error:
        return error
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    status = payload.get("status")
    return str(status) if status else "unknown failure"
