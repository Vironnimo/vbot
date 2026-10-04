"""OpenRouter wire client for the ``video_generation`` Task Model."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from core.attachments import sniff_media_type
from core.model_tasks.image_types import ImageInput
from core.model_tasks.video_types import VideoGenerationResult
from core.providers._http_shared import decode_response_json
from core.providers.errors import ProviderError, ProviderOutcomeUnknownError
from core.providers.task_client import (
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    content_refusal_in,
    is_omittable_option,
    merge_extra_options,
)
from core.utils.errors import VBotError

JsonObject = dict[str, Any]
VIDEO_CREATE_ENDPOINT = "/videos"
VIDEO_REQUEST_TIMEOUT_SECONDS = 60.0
VIDEO_POLL_TIMEOUT_SECONDS = 20 * 60.0
VIDEO_POLL_INTERVAL_SECONDS = 10.0
_TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled", "expired"})


class VideoJobUnfinishedError(ProviderOutcomeUnknownError):
    """A submitted Video job whose result vBot could not collect.

    The job keeps running at the provider and is billed when it finishes, so
    submitting the same request again would pay twice.
    """

    def __init__(self, job_id: str, reason: str) -> None:
        self.job_id = job_id
        self.reason = reason
        super().__init__(f"video job {job_id}: {reason}", operation_key=job_id)


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
        job_id, created = await self.post_and_parse(
            VIDEO_CREATE_ENDPOINT,
            timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
            json=payload,
            parse=_parse_created_video_response,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )
        status_payload = await self._await_completion(
            job_id, created, poll_timeout=poll_timeout, poll_interval=poll_interval
        )
        safe_job_id = quote(job_id, safe="")
        try:
            content, media_type = await self.get_and_parse(
                f"/videos/{safe_job_id}/content?index=0",
                timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
                parse=_parse_video_content,
            )
        except VBotError as exc:
            raise VideoJobUnfinishedError(
                job_id, f"the finished video could not be downloaded: {exc}"
            ) from exc
        usage = status_payload.get("usage")
        return VideoGenerationResult(
            data=content,
            media_type=media_type,
            model=self._model_id,
            job_id=job_id,
            usage=dict(usage) if isinstance(usage, Mapping) else None,
            raw=dict(status_payload),
        )

    async def _await_completion(
        self,
        job_id: str,
        status_payload: JsonObject,
        *,
        poll_timeout: float,
        poll_interval: float,
    ) -> JsonObject:
        """Poll the job until it completes; a failed job raises its reason.

        Transient poll failures keep polling until the deadline; any other
        failure after submission leaves the job running at the provider.
        """

        safe_job_id = quote(job_id, safe="")
        last_problem = ""
        try:
            async with asyncio.timeout(poll_timeout):
                while status_payload.get("status") != "completed":
                    if status_payload.get("status") in _TERMINAL_FAILURE_STATUSES:
                        raise _job_failure(status_payload)
                    await asyncio.sleep(poll_interval)
                    try:
                        status_payload = await self.get_and_parse(
                            f"/videos/{safe_job_id}",
                            timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
                            parse=_parse_video_response,
                        )
                        last_problem = ""
                    except VBotError as exc:
                        if not getattr(exc, "retryable", False):
                            raise VideoJobUnfinishedError(
                                job_id, f"checking its status failed: {exc}"
                            ) from exc
                        last_problem = f" (the last status check failed: {exc})"
        except TimeoutError as exc:
            minutes = max(1, round(poll_timeout / 60))
            raise VideoJobUnfinishedError(
                job_id, f"it had not finished after {minutes} minutes{last_problem}"
            ) from exc
        return status_payload


def _job_failure(payload: Mapping[str, Any]) -> ProviderError:
    """Return the error a failed job reports: a refusal, or the provider's reason."""

    refusal = content_refusal_in(payload.get("error"))
    if refusal is not None:
        return refusal
    return ProviderError(
        f"OpenRouter video generation failed: {_video_error_message(payload)}",
        retryable=False,
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
    if isinstance(duration, str) and duration.isdecimal():
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


def _parse_created_video_response(response: httpx.Response) -> tuple[str, JsonObject]:
    payload = _parse_video_response(response)
    job_id = payload.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ProviderError("OpenRouter did not return a video job id.", retryable=False)
    return job_id, payload


def _parse_video_response(response: httpx.Response) -> JsonObject:
    payload = decode_response_json(response, "OpenRouter video generation")
    if not isinstance(payload, Mapping):
        raise ProviderError("OpenRouter did not return a video job id.", retryable=False)
    status = payload.get("status")
    if status is not None and not isinstance(status, str):
        raise ProviderError(
            "OpenRouter video generation failed: invalid job status.",
            retryable=False,
        )
    return dict(payload)


def _parse_video_content(response: httpx.Response) -> tuple[bytes, str]:
    content = response.content
    if not content:
        raise ProviderError("OpenRouter did not return generated video content.", retryable=False)
    media_type = sniff_media_type(content, "video")
    if not media_type.startswith("video/"):
        declared = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        media_type = declared if declared.startswith("video/") else "video/mp4"
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
