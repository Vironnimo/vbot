"""Provider-neutral Video generation execution service."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.model_tasks.artifacts import (
    GeneratedMediaArtifact,
    ensure_output_dir,
    write_generated_media_artifact,
)
from core.model_tasks.constants import TASK_VIDEO_GENERATION
from core.model_tasks.image import load_image_inputs
from core.model_tasks.image_profile import (
    match_choice,
    missing_choice_message,
    unoffered_choice_message,
)
from core.model_tasks.model_tasks import TaskModelTargetRef, model_supports_task
from core.model_tasks.task_execution import (
    TaskBindingResolver,
    TaskUsage,
    TaskUsageContext,
    task_debug_context,
)
from core.model_tasks.video_providers import ProviderVideoClient, VideoJobUnfinishedError
from core.model_tasks.video_types import VideoGenerationResult
from core.providers.errors import ProviderContentRefusedError, ProviderOutcomeUnknownError
from core.providers.task_client import TaskClientRuntime
from core.usage import UsageRecorder
from core.utils.errors import TaskError, VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
_LOGGER = get_logger(__name__)

#: Per-call choices a video Model can offer, in Tool order.
VIDEO_CALL_OPTIONS = ("duration", "aspect_ratio", "resolution")
FRAME_IMAGE_TYPES = ("first_frame", "last_frame")


class VideoError(TaskError):
    """Base class for expected Video generation errors."""

    code = "video_error"
    retryable = False


class VideoConfigurationError(VideoError):
    """Raised when Video generation is not configured or usable."""


class VideoOptionError(VideoError):
    """Raised when a per-call option or frame image is not one the configured Model offers."""

    code = "invalid_arguments"


class VideoExecutionError(VideoError):
    """Raised when an OpenRouter Video request fails."""

    code = "provider_error"


class VideoRefusedError(VideoExecutionError):
    """Raised when the provider declined to create the requested video."""

    code = ProviderContentRefusedError.code

    def __init__(self, reason: str | None) -> None:
        self.reason = reason
        super().__init__(
            f"Video generation was refused: {reason}"
            if reason
            else "Video generation was refused without a reason"
        )


class VideoOutcomeUnknownError(VideoExecutionError):
    """Raised when a Video request or job may have completed and been billed.

    ``job_id`` names the submitted provider job when the request was accepted
    and only its result could not be collected.
    """

    code = ProviderOutcomeUnknownError.code

    def __init__(self, message: str, *, operation_key: str, job_id: str | None = None) -> None:
        self.operation_key = operation_key
        self.job_id = job_id
        super().__init__(message)


@dataclass(frozen=True)
class VideoProfile:
    """What the configured video Model offers per call.

    ``call_choices`` maps each per-call option to its allowed values (an
    option with fewer than two values is absent); ``duration`` values are
    whole seconds as text. ``generate_audio`` says whether the soundtrack can
    be switched per call; ``frame_images`` lists the accepted frame positions.
    """

    call_choices: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    generate_audio: bool = False
    frame_images: tuple[str, ...] = ()

    def validated_options(self, call_options: Mapping[str, Any]) -> JsonObject:
        """Return the wire form of per-call options, or raise :class:`VideoOptionError`."""

        validated: JsonObject = {}
        for name, value in call_options.items():
            if value is None or value == "":
                continue
            if name == "generate_audio":
                if not self.generate_audio:
                    raise VideoOptionError(missing_choice_message("video", name))
                validated[name] = bool(value)
                continue
            choices = self.call_choices.get(name)
            if choices is None:
                raise VideoOptionError(missing_choice_message("video", name))
            matched = match_choice(str(value).removesuffix("s").strip(), choices)
            if matched is None:
                raise VideoOptionError(unoffered_choice_message("video", name, value, choices))
            validated[name] = int(matched) if name == "duration" else matched
        return validated


def build_video_profile(model: Any | None) -> VideoProfile:
    """Read one Model's published video facts into its per-call profile."""

    facts = _video_task_options(model)
    parameters = facts.get("parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    call_choices: dict[str, tuple[str, ...]] = {}
    for name in VIDEO_CALL_OPTIONS:
        spec = parameters.get(name)
        values = spec.get("values") if isinstance(spec, Mapping) else None
        if not isinstance(values, list | tuple):
            continue
        choices = tuple(str(value) for value in values if str(value) != "auto")
        if name == "duration":
            choices = tuple(sorted((value for value in choices if value.isdecimal()), key=int))
        if len(choices) > 1:
            call_choices[name] = choices
    frame_support = facts.get("frame_images")
    frames = (
        tuple(frame for frame in FRAME_IMAGE_TYPES if frame in frame_support)
        if isinstance(frame_support, list | tuple)
        else ()
    )
    return VideoProfile(
        call_choices=call_choices,
        generate_audio="generate_audio" in parameters,
        frame_images=frames,
    )


class VideoService:
    """Execute ``video_generation`` through its configured Task Model."""

    def __init__(
        self,
        model_tasks: Any,
        runtime: TaskClientRuntime,
        *,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._usage_recorder = usage_recorder
        self._resolver = TaskBindingResolver(
            model_tasks,
            configuration_error=VideoConfigurationError,
        )
        self._cancelled_jobs: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        """Stop following cancelled jobs; each one still running logs its job id."""

        jobs = list(self._cancelled_jobs)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    def generation_profile(self) -> VideoProfile:
        """Return what the configured video Model offers, without a request."""

        try:
            binding = self._resolver.binding_for(TASK_VIDEO_GENERATION)
            target_ref = self._resolver.parse_target(binding.target)
        except VideoConfigurationError:
            return VideoProfile()
        if target_ref.kind != "provider" or target_ref.provider_id != "openrouter":
            return VideoProfile()
        return build_video_profile(self._model_tasks.model_for_target(target_ref))

    async def generate(
        self,
        prompt: str,
        *,
        call_options: Mapping[str, Any] | None = None,
        frame_paths: Mapping[str, str | Path] | None = None,
        usage_context: TaskUsageContext | None = None,
    ) -> VideoGenerationResult:
        """Generate one video; per-call options override the binding's Settings.

        An option, value, or frame position the configured Model does not
        offer raises :class:`VideoOptionError` before any request.
        """

        normalized_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not normalized_prompt:
            raise VideoOptionError("Prompt must not be empty")

        try:
            _binding, options, target_ref = self._resolver.resolve(TASK_VIDEO_GENERATION)
        except VideoConfigurationError as exc:
            raise VideoConfigurationError("no Video generation model is chosen") from exc
        model = self._validated_model(target_ref)
        profile = build_video_profile(model)
        requested = profile.validated_options(call_options or {})
        merged_options = {**_shape_options(options, requested, profile), **requested}
        frames = await self._load_frames(profile, frame_paths or {})

        client = ProviderVideoClient.from_runtime(
            self._runtime,
            target_ref,
            usage_observer=TaskUsage(
                self._usage_recorder, TASK_VIDEO_GENERATION, target_ref, context=usage_context
            ),
            debug_context=task_debug_context(usage_context, target_ref),
        )
        model_ref = f"{target_ref.provider_id}/{target_ref.model_id}"

        def follow_cancelled_job(job_id: str, polling: asyncio.Task[JsonObject]) -> None:
            job = asyncio.create_task(
                _settle_cancelled_job(job_id, polling, model_ref), name="video-job-cost"
            )
            self._cancelled_jobs.add(job)
            job.add_done_callback(self._cancelled_jobs.discard)

        try:
            return await client.generate(
                normalized_prompt,
                options=merged_options,
                frame_images=frames,
                on_cancelled_job=follow_cancelled_job,
            )
        except ProviderContentRefusedError as exc:
            raise VideoRefusedError(exc.reason) from exc
        except VideoJobUnfinishedError as exc:
            raise VideoOutcomeUnknownError(
                exc.reason, operation_key=exc.operation_key, job_id=exc.job_id
            ) from exc
        except ProviderOutcomeUnknownError as exc:
            raise VideoOutcomeUnknownError(str(exc), operation_key=exc.operation_key) from exc
        except VBotError as exc:
            raise VideoExecutionError(str(exc)) from exc

    async def generate_artifact(
        self,
        prompt: str,
        *,
        output_dir: str | Path,
        call_options: Mapping[str, Any] | None = None,
        frame_paths: Mapping[str, str | Path] | None = None,
        usage_context: TaskUsageContext | None = None,
    ) -> GeneratedMediaArtifact:
        """Generate one Video and persist it in the caller-owned directory.

        The directory is created before the request, so an unusable folder
        raises :class:`OutputDirectoryError` without paying for a video.
        """

        directory = ensure_output_dir(output_dir)
        result = await self.generate(
            prompt,
            call_options=call_options,
            frame_paths=frame_paths,
            usage_context=usage_context,
        )
        return await asyncio.to_thread(
            write_generated_media_artifact,
            result.data,
            output_dir=directory,
            extension=_video_extension(result.media_type),
            media_type=result.media_type,
        )

    def _validated_model(self, target_ref: TaskModelTargetRef) -> Any:
        if target_ref.kind != "provider" or target_ref.provider_id != "openrouter":
            raise VideoConfigurationError("the chosen provider does not offer Video generation")
        model = self._model_tasks.model_for_target(target_ref)
        if model is None:
            raise VideoConfigurationError("the chosen Video generation model is no longer offered")
        if not model_supports_task(model, TASK_VIDEO_GENERATION):
            raise VideoConfigurationError("the chosen model does not generate videos")
        return model

    async def _load_frames(
        self,
        profile: VideoProfile,
        frame_paths: Mapping[str, str | Path],
    ) -> tuple[tuple[str, Any], ...]:
        if not frame_paths:
            return ()
        for frame_type in frame_paths:
            if frame_type not in profile.frame_images:
                raise VideoOptionError(
                    f"Nothing was generated. The configured video model does not accept "
                    f"{frame_type}. Repeat the call without {frame_type}."
                )
        ordered = [
            (frame_type, frame_paths[frame_type])
            for frame_type in FRAME_IMAGE_TYPES
            if frame_type in frame_paths
        ]
        images = await asyncio.to_thread(load_image_inputs, [path for _, path in ordered])
        return tuple(
            (frame_type, image) for (frame_type, _), image in zip(ordered, images, strict=True)
        )


async def _settle_cancelled_job(
    job_id: str, polling: asyncio.Task[JsonObject], model_ref: str
) -> None:
    """Follow a cancelled generation's job until the provider reports its cost.

    The job cannot be cancelled at the provider and is billed when it finishes;
    its final status carries the cost, which the poll records on the create
    call's Usage. When that cannot happen, the job id is logged so the charge
    can still be traced.
    """

    try:
        await polling
    except asyncio.CancelledError:
        _LOGGER.warning(
            "Stopped following a cancelled video job; its cost is not recorded (job=%s model=%s)",
            job_id,
            model_ref,
        )
        raise
    except ProviderContentRefusedError:
        _LOGGER.debug("Cancelled video job was refused (model=%s)", model_ref)
    except VideoJobUnfinishedError as exc:
        _LOGGER.warning(
            "Cancelled video job did not report its cost (job=%s model=%s reason=%s)",
            job_id,
            model_ref,
            exc.reason,
        )
    except VBotError as exc:
        _LOGGER.debug("Cancelled video job failed (model=%s error=%s)", model_ref, exc)
    else:
        _LOGGER.debug("Cancelled video job finished; its cost is recorded (model=%s)", model_ref)


def _shape_options(
    options: Mapping[str, Any], requested: Mapping[str, Any], profile: VideoProfile
) -> dict[str, Any]:
    """Return the Settings options without ``size`` when a call asks for a shape.

    A configured size would override the requested aspect ratio or resolution
    on the wire, so it is dropped; the half of the shape the call left out is
    taken from that size, so an omitted choice keeps its configured value.
    """

    if "size" not in options or not {"aspect_ratio", "resolution"} & requested.keys():
        return dict(options)
    shaped = {name: value for name, value in options.items() if name != "size"}
    width, _, height = str(options["size"]).partition("x")
    if not (width.isdecimal() and height.isdecimal()) or not int(width) or not int(height):
        return shaped
    width_px, height_px = int(width), int(height)
    divisor = math.gcd(width_px, height_px)
    implied = {
        "aspect_ratio": f"{width_px // divisor}:{height_px // divisor}",
        "resolution": "4K" if min(width_px, height_px) >= 2160 else f"{min(width_px, height_px)}p",
    }
    for name, value in implied.items():
        if name in requested or name in shaped:
            continue
        matched = match_choice(value, profile.call_choices.get(name, ()))
        if matched is not None:
            shaped[name] = matched
    return shaped


def _video_task_options(model: Any) -> Mapping[str, Any]:
    task_options = getattr(getattr(model, "capabilities", None), "task_options", {})
    options = task_options.get(TASK_VIDEO_GENERATION) if isinstance(task_options, Mapping) else None
    return options if isinstance(options, Mapping) else {}


def _video_extension(media_type: str) -> str:
    return "webm" if media_type.split(";", 1)[0].strip().lower() == "video/webm" else "mp4"
