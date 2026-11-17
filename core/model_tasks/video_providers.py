"""Wire client for the ``video_generation`` Task Model.

Two routes share one job contract (submit once, poll until the job ends,
collect its video, never resubmit): OpenRouter's ``POST /videos`` jobs with a
separate content download, and Google's Interactions API, where Gemini Omni
runs as a background interaction whose finished state carries the video inline.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, override
from urllib.parse import quote

import httpx

from core.attachments import sniff_media_type
from core.model_tasks.image_types import ImageInput
from core.model_tasks.video_types import VideoGenerationResult
from core.providers.errors import (
    ProviderContentRefusedError,
    ProviderError,
    ProviderOutcomeUnknownError,
    ProviderUsageLimitError,
)
from core.providers.http import decode_response_json
from core.providers.task_client import (
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    TaskRequestRetryPolicy,
    content_refusal_in,
    is_omittable_option,
    merge_extra_options,
)
from core.utils.errors import VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
VIDEO_CREATE_ENDPOINT = "/videos"
GOOGLE_INTERACTIONS_ENDPOINT = "/interactions"
VIDEO_REQUEST_TIMEOUT_SECONDS = 60.0
VIDEO_POLL_TIMEOUT_SECONDS = 20 * 60.0
VIDEO_POLL_INTERVAL_SECONDS = 10.0
_TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled", "expired"})
# Interaction states (Interactions API reference, read 2026-11-17). An
# ``incomplete`` interaction ended early but may still hold a video; the
# ``requires_*`` states wait for client input a video request never gives.
_GOOGLE_DONE_STATUSES = frozenset({"completed", "incomplete"})
_GOOGLE_FAILURE_STATUSES = frozenset(
    {"failed", "cancelled", "requires_action", "requires_continuation"}
)
# Google documents 503 UNAVAILABLE as an overloaded service to retry with
# backoff, so it proves no interaction was created; every other ambiguous
# failure may follow a billed video.
_GOOGLE_VIDEO_RETRY_POLICY = TaskRequestRetryPolicy(
    replay_safe=False,
    verified_safe_retry_status_codes=frozenset({503}),
)
# Interpolating between a first and a last frame is limited to 10-second clips.
_GOOGLE_FRAME_PAIR_MAX_SECONDS = 10
_MAX_REASON_CHARS = 500
_LOGGER = get_logger("video.providers")

#: Takes over the polling of a job whose caller was cancelled; see ``generate``.
type CancelledJobHandler = Callable[[str, asyncio.Task[JsonObject]], None]


class VideoJobUnfinishedError(ProviderOutcomeUnknownError):
    """A submitted Video job whose result vBot could not collect.

    The job keeps running at the provider and is billed when it finishes, so
    submitting the same request again would pay twice.
    """

    def __init__(self, job_id: str, reason: str) -> None:
        self.job_id = job_id
        self.reason = reason
        super().__init__(f"video job {job_id}: {reason}", operation_key=job_id)


@dataclass(frozen=True, slots=True)
class _JobWire:
    """How one route reads a submitted job: its status path, parser and end states."""

    status_path: str
    parse: Callable[[httpx.Response], JsonObject]
    done: frozenset[str]
    failure: Callable[[Mapping[str, Any]], ProviderError | None]


class ProviderVideoClient(ProviderTaskClient):
    """Execute a Video job: OpenRouter ``/videos`` or a Google background interaction."""

    async def generate(
        self,
        prompt: str,
        *,
        options: JsonObject,
        frame_images: Sequence[tuple[str, ImageInput]] = (),
        poll_timeout: float = VIDEO_POLL_TIMEOUT_SECONDS,
        poll_interval: float | None = None,
        on_cancelled_job: CancelledJobHandler | None = None,
    ) -> VideoGenerationResult:
        """Submit one job, wait for it, and collect its video.

        OpenRouter offers no way to cancel a submitted job: it keeps running and
        is billed when it finishes. A cancellation while the job is polled
        therefore hands the still-running poll to ``on_cancelled_job``, whose
        owner can await it so the poll's reported cost reaches the Usage
        observer; without a handler the poll stops with the caller. A Google
        interaction is first asked to cancel at Google, and the handed-over
        task then follows it to its end the same way.
        """

        google = self._provider.adapter == "google"
        if google:
            job = _GOOGLE_JOB
            job_id, created = await self.post_and_parse(
                GOOGLE_INTERACTIONS_ENDPOINT,
                timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
                json=_google_video_payload(
                    self._model_id, prompt, options=options, frame_images=frame_images
                ),
                parse=_parse_created_interaction,
                retry_policy=_GOOGLE_VIDEO_RETRY_POLICY,
            )
        else:
            job = _OPENROUTER_JOB
            job_id, created = await self.post_and_parse(
                VIDEO_CREATE_ENDPOINT,
                timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
                json=_video_payload(
                    self._model_id, prompt, options=options, frame_images=frame_images
                ),
                parse=_parse_created_video_response,
                retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
            )
        polling = asyncio.create_task(
            self._await_completion(
                job_id,
                created,
                job=job,
                poll_timeout=poll_timeout,
                poll_interval=VIDEO_POLL_INTERVAL_SECONDS
                if poll_interval is None
                else poll_interval,
            ),
            name="video-job-poll",
        )
        try:
            status_payload = await asyncio.shield(polling)
        except asyncio.CancelledError:
            if polling.done():
                if not polling.cancelled():
                    polling.exception()  # the caller's cancellation wins
            elif on_cancelled_job is not None:
                on_cancelled_job(
                    job_id,
                    asyncio.create_task(
                        self._cancel_and_follow(job_id, polling), name="video-job-cancel"
                    )
                    if google
                    else polling,
                )
            else:
                polling.cancel()
            raise
        if google:
            content, media_type = _google_video_content(job_id, status_payload)
            return VideoGenerationResult(
                data=content,
                media_type=media_type,
                model=self._model_id,
                job_id=job_id,
                usage=_interactions_usage(status_payload.get("usage")),
                raw=_without_video_data(status_payload),
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

    @override
    async def _observe_response_usage(self, response: httpx.Response) -> Mapping[str, Any] | None:
        if self._provider.adapter != "google":
            return await super()._observe_response_usage(response)
        try:
            body = response.json()
        except ValueError, UnicodeError:
            return None
        return _interactions_usage(body.get("usage") if isinstance(body, Mapping) else None)

    async def _await_completion(
        self,
        job_id: str,
        status_payload: JsonObject,
        *,
        job: _JobWire,
        poll_timeout: float,
        poll_interval: float,
    ) -> JsonObject:
        """Poll the job until it is done; a failed job raises its reason.

        Transient poll failures keep polling until the deadline; any other
        failure after submission leaves the job running at the provider.
        """

        status_path = job.status_path.format(job_id=quote(job_id, safe=""))
        last_problem = ""
        try:
            async with asyncio.timeout(poll_timeout):
                while status_payload.get("status") not in job.done:
                    failure = job.failure(status_payload)
                    if failure is not None:
                        raise failure
                    await asyncio.sleep(poll_interval)
                    try:
                        status_payload = await self.get_and_parse(
                            status_path,
                            timeout=VIDEO_REQUEST_TIMEOUT_SECONDS,
                            parse=job.parse,
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

    async def _cancel_and_follow(
        self, job_id: str, polling: asyncio.Task[JsonObject]
    ) -> JsonObject:
        """Cancel a Google interaction whose caller left, then follow it to its end."""

        try:
            await self._request_cancel(job_id)
        except asyncio.CancelledError:
            polling.cancel()
            raise
        return await polling

    async def _request_cancel(self, job_id: str) -> None:
        """Ask Google to stop the interaction once; a failure leaves it running."""

        endpoint = f"{GOOGLE_INTERACTIONS_ENDPOINT}/{quote(job_id, safe='')}/cancel"
        try:
            async with self.http_client(VIDEO_REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(endpoint, headers=await self._headers())
        except (httpx.HTTPError, VBotError) as exc:
            _LOGGER.debug(
                "Cancelling a video job failed (model=%s error=%s)",
                self._model_id,
                type(exc).__name__,
            )
            return
        if response.status_code >= 400:
            _LOGGER.debug(
                "Cancelling a video job failed (model=%s status=%s)",
                self._model_id,
                response.status_code,
            )


def _openrouter_job_failure(payload: Mapping[str, Any]) -> ProviderError | None:
    if payload.get("status") not in _TERMINAL_FAILURE_STATUSES:
        return None
    return _job_failure(payload)


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
    duration = _duration_seconds(options.get("duration"))
    if duration is not None:
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


def _duration_seconds(value: Any) -> int | None:
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


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


def _google_video_payload(
    model_id: str,
    prompt: str,
    *,
    options: JsonObject,
    frame_images: Sequence[tuple[str, ImageInput]],
) -> JsonObject:
    """Shape one background interaction for Gemini Omni.

    Frame images precede the prompt in first, last order under the
    ``image_to_video`` task, where the first image is the starting frame and
    an optional second one the ending frame; a last frame alone has no slot.
    """

    frames = dict(frame_images)
    if "last_frame" in frames and "first_frame" not in frames:
        raise ProviderError(
            "Gemini Omni takes a last_frame only together with a first_frame.",
            retryable=False,
        )
    duration = _duration_seconds(options.get("duration"))
    if len(frames) == 2 and duration is not None and duration > _GOOGLE_FRAME_PAIR_MAX_SECONDS:
        raise ProviderError(
            "Gemini Omni interpolates between a first_frame and a last_frame for at most "
            f"{_GOOGLE_FRAME_PAIR_MAX_SECONDS} seconds.",
            retryable=False,
        )
    response_format: JsonObject = {"type": "video"}
    for name in ("aspect_ratio", "resolution"):
        value = options.get(name)
        if not is_omittable_option(value):
            response_format[name] = value
    if duration is not None:
        response_format["duration"] = f"{duration}s"
    ordered = sorted(frame_images, key=lambda frame: frame[0] != "first_frame")
    payload: JsonObject = {
        "model": model_id,
        "input": [
            *(
                {
                    "type": "image",
                    "data": base64.b64encode(image.data).decode("ascii"),
                    "mime_type": image.media_type,
                }
                for _, image in ordered
            ),
            {"type": "text", "text": prompt},
        ]
        if ordered
        else prompt,
        "response_format": response_format,
        # Background execution answers with the interaction id at once; it
        # needs a stored interaction, which the status polls read.
        "background": True,
        "store": True,
    }
    if ordered:
        payload["generation_config"] = {"video_config": {"task": "image_to_video"}}
    merge_extra_options(payload, options)
    return payload


def _parse_created_interaction(response: httpx.Response) -> tuple[str, JsonObject]:
    payload = _parse_interaction(response)
    job_id = payload.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ProviderError("Google did not return a video interaction id.", retryable=False)
    return job_id, payload


def _parse_interaction(response: httpx.Response) -> JsonObject:
    payload = decode_response_json(response, "Google video generation")
    if not isinstance(payload, Mapping):
        raise ProviderError(
            "Google answered the video request with a non-object body.", retryable=False
        )
    status = payload.get("status")
    if status is not None and not isinstance(status, str):
        raise ProviderError(
            "Google video generation failed: invalid interaction status.", retryable=False
        )
    return dict(payload)


def _google_job_failure(payload: Mapping[str, Any]) -> ProviderError | None:
    """Return the error an ended interaction reports, or ``None`` while it runs.

    Its first ``errors`` entry carries a status code, ``RESOURCE_EXHAUSTED``
    when the account has no quota for the Model (live, 2026-11-17), or one of
    Google's blocked-generation codes, which are refusals.
    """

    status = payload.get("status")
    if status not in _GOOGLE_FAILURE_STATUSES:
        return None
    code, message = "", ""
    errors = payload.get("errors")
    first = errors[0] if isinstance(errors, list) and errors else None
    if isinstance(first, Mapping):
        raw_code, raw_message = first.get("code"), first.get("message")
        code = raw_code.rsplit("/", 1)[-1].strip() if isinstance(raw_code, str) else ""
        message = " ".join(raw_message.split()) if isinstance(raw_message, str) else ""
    refusal = content_refusal_in({"code": code.lower(), "message": message})
    if refusal is not None:
        return refusal
    reason = f"{code}: {message}" if code and message else code or message or "no reason given"
    if code.upper() == "RESOURCE_EXHAUSTED":
        return ProviderUsageLimitError(f"Google video generation ran out of quota ({reason})")
    return ProviderError(f"Google video generation {status} ({reason})", retryable=False)


def _google_video_content(job_id: str, payload: Mapping[str, Any]) -> tuple[bytes, str]:
    """Return the first video of a finished interaction's model output.

    A finished interaction with text but no video is the Model declining;
    with neither it is an ordinary failure. A video vBot cannot decode, or one
    reported without inline data, still exists at Google under the job id.
    """

    videos: list[Mapping[str, Any]] = []
    texts: list[str] = []
    steps = payload.get("steps")
    for step in steps if isinstance(steps, list) else ():
        if not isinstance(step, Mapping) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        for item in content if isinstance(content, list) else ():
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "video":
                videos.append(item)
            elif (
                item.get("type") == "text"
                and isinstance(item.get("text"), str)
                and (text := item["text"].strip())
            ):
                texts.append(text)
    if not videos:
        if texts:
            raise ProviderContentRefusedError("\n".join(texts)[:_MAX_REASON_CHARS])
        raise ProviderError(
            f"Google ended the video job ({payload.get('status')}) without a video.",
            retryable=False,
        )
    video = videos[0]
    data = video.get("data")
    if not isinstance(data, str) or not data:
        raise VideoJobUnfinishedError(job_id, "Google returned the video without inline data")
    try:
        content_bytes = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VideoJobUnfinishedError(job_id, "Google returned undecodable video data") from exc
    if not content_bytes:
        raise VideoJobUnfinishedError(job_id, "Google returned empty video data")
    media_type = sniff_media_type(content_bytes, "video")
    if not media_type.startswith("video/"):
        declared = video.get("mime_type")
        media_type = (
            declared if isinstance(declared, str) and declared.startswith("video/") else "video/mp4"
        )
    return content_bytes, media_type


def _without_video_data(payload: Mapping[str, Any]) -> JsonObject:
    """Return the interaction without inline media, which the result carries as bytes."""

    raw = dict(payload)
    steps = raw.get("steps")
    if isinstance(steps, list):
        raw["steps"] = [
            {
                **step,
                "content": [
                    {key: value for key, value in item.items() if key != "data"}
                    if isinstance(item, Mapping)
                    else item
                    for item in step["content"]
                ],
            }
            if isinstance(step, Mapping) and isinstance(step.get("content"), list)
            else step
            for step in steps
        ]
    return raw


def _interactions_usage(raw: Any) -> JsonObject | None:
    """Project Interactions API Usage onto the canonical counters.

    ``total_output_tokens`` excludes ``total_thought_tokens`` (the reference's
    example totals add both), so together they make the output, as Gemini's
    candidates and thoughts do on ``generateContent``.
    """

    if not isinstance(raw, Mapping):
        return None

    def count(name: str) -> int | None:
        value = raw.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    input_tokens = count("total_input_tokens")
    output_tokens = count("total_output_tokens")
    thought_tokens = count("total_thought_tokens")
    usage: JsonObject = {}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
        usage["cache_read_tokens"] = count("total_cached_tokens") or 0
    if output_tokens is not None or thought_tokens is not None:
        usage["output_tokens"] = (output_tokens or 0) + (thought_tokens or 0)
    if thought_tokens is not None:
        usage["reasoning_tokens"] = thought_tokens
    return usage or None


_OPENROUTER_JOB = _JobWire(
    status_path=VIDEO_CREATE_ENDPOINT + "/{job_id}",
    parse=_parse_video_response,
    done=frozenset({"completed"}),
    failure=_openrouter_job_failure,
)
_GOOGLE_JOB = _JobWire(
    status_path=GOOGLE_INTERACTIONS_ENDPOINT + "/{job_id}",
    parse=_parse_interaction,
    done=_GOOGLE_DONE_STATUSES,
    failure=_google_job_failure,
)
