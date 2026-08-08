"""Provider-neutral Video generation execution service."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.model_tasks.artifacts import GeneratedMediaArtifact, write_generated_media_artifact
from core.model_tasks.constants import TASK_VIDEO_GENERATION
from core.model_tasks.image import load_image_inputs
from core.model_tasks.model_tasks import TaskModelTargetRef, model_supports_task
from core.model_tasks.task_execution import TaskBindingResolver
from core.model_tasks.video_providers import ProviderVideoClient
from core.model_tasks.video_types import VideoGenerationResult
from core.providers.errors import ProviderOutcomeUnknownError
from core.providers.task_client import TaskClientRuntime
from core.utils.errors import TaskError, VBotError

JsonObject = dict[str, Any]


class VideoError(TaskError):
    """Base class for expected Video generation errors."""

    code = "video_error"
    retryable = False


class VideoConfigurationError(VideoError):
    """Raised when Video generation is not configured or usable."""


class VideoExecutionError(VideoError):
    """Raised when an OpenRouter Video request fails."""

    code = "provider_error"


class VideoOutcomeUnknownError(VideoExecutionError):
    """Raised when the initial Video request may have been accepted."""

    code = ProviderOutcomeUnknownError.code

    def __init__(self, message: str, *, operation_key: str) -> None:
        self.operation_key = operation_key
        super().__init__(message)


class VideoService:
    """Execute ``video_generation`` through its configured Task Model."""

    def __init__(self, model_tasks: Any, runtime: TaskClientRuntime) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._resolver = TaskBindingResolver(
            model_tasks,
            configuration_error=VideoConfigurationError,
        )

    def generation_capabilities(self) -> frozenset[str]:
        """Return stable per-Model option and frame-image capabilities."""

        model = self._configured_model()
        if model is None:
            return frozenset()
        task_options = _video_task_options(model)
        parameters = task_options.get("parameters")
        names = set(parameters) if isinstance(parameters, Mapping) else set()
        frame_images = task_options.get("frame_images")
        if isinstance(frame_images, list | tuple):
            names.update(value for value in frame_images if isinstance(value, str))
        if {"resolution", "aspect_ratio"}.intersection(names):
            names.discard("size")
        return frozenset(names)

    async def generate(
        self,
        prompt: str,
        *,
        call_options: Mapping[str, Any] | None = None,
        frame_paths: Mapping[str, str | Path] | None = None,
    ) -> VideoGenerationResult:
        normalized_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not normalized_prompt:
            raise VideoConfigurationError("Prompt must not be empty")

        try:
            _binding, options, target_ref = self._resolver.resolve(TASK_VIDEO_GENERATION)
        except VideoConfigurationError as exc:
            raise VideoConfigurationError(
                "Video generation is not configured. Select a Video generation "
                "Task Model in Settings."
            ) from exc
        model = self._validated_model(target_ref)
        merged_options = {**options, **_validated_video_call_options(model, call_options or {})}
        frames = await self._load_frames(model, frame_paths or {})

        client = ProviderVideoClient.from_runtime(self._runtime, target_ref)
        try:
            return await client.generate(
                normalized_prompt,
                options=merged_options,
                frame_images=frames,
            )
        except VideoError:
            raise
        except ProviderOutcomeUnknownError as exc:
            raise VideoOutcomeUnknownError(str(exc), operation_key=exc.operation_key) from exc
        except VBotError as exc:
            raise VideoExecutionError(str(exc)) from exc
        except Exception as exc:
            raise VideoExecutionError(str(exc)) from exc

    async def generate_artifact(
        self,
        prompt: str,
        *,
        output_dir: str | Path,
        call_options: Mapping[str, Any] | None = None,
        frame_paths: Mapping[str, str | Path] | None = None,
    ) -> GeneratedMediaArtifact:
        """Generate one Video and persist it in the caller-owned directory."""

        result = await self.generate(
            prompt,
            call_options=call_options,
            frame_paths=frame_paths,
        )
        return write_generated_media_artifact(
            result.data,
            output_dir=output_dir,
            extension=_video_extension(result.media_type),
            media_type=result.media_type,
            error=VideoExecutionError,
        )

    def _configured_model(self) -> Any | None:
        try:
            binding = self._resolver.binding_for(TASK_VIDEO_GENERATION)
            target_ref = self._resolver.parse_target(binding.target)
        except VideoConfigurationError:
            return None
        if target_ref.kind != "provider" or target_ref.provider_id != "openrouter":
            return None
        return self._model_tasks.model_for_target(target_ref)

    def _validated_model(self, target_ref: TaskModelTargetRef) -> Any:
        if target_ref.kind != "provider" or target_ref.provider_id != "openrouter":
            raise VideoConfigurationError(
                "The configured provider does not support Video generation."
            )
        model = self._model_tasks.model_for_target(target_ref)
        if model is None:
            raise VideoConfigurationError(
                "The configured Video generation model is no longer available. "
                "Select another Task Model in Settings."
            )
        if not model_supports_task(model, TASK_VIDEO_GENERATION):
            raise VideoConfigurationError(
                "The configured provider does not support Video generation."
            )
        return model

    async def _load_frames(
        self,
        model: Any,
        frame_paths: Mapping[str, str | Path],
    ) -> tuple[tuple[str, Any], ...]:
        if not frame_paths:
            return ()
        frame_support = _video_task_options(model).get("frame_images")
        supported = set(frame_support) if isinstance(frame_support, list | tuple) else set()
        for frame_type in frame_paths:
            if frame_type not in supported:
                raise VideoConfigurationError(
                    f"The configured Video generation model does not support {frame_type}."
                )
        ordered = [
            (frame_type, frame_paths[frame_type])
            for frame_type in ("first_frame", "last_frame")
            if frame_type in frame_paths
        ]
        images = await asyncio.to_thread(load_image_inputs, [path for _, path in ordered])
        return tuple(
            (frame_type, image) for (frame_type, _), image in zip(ordered, images, strict=True)
        )


def _video_task_options(model: Any) -> Mapping[str, Any]:
    task_options = getattr(getattr(model, "capabilities", None), "task_options", {})
    options = task_options.get(TASK_VIDEO_GENERATION) if isinstance(task_options, Mapping) else None
    return options if isinstance(options, Mapping) else {}


def _validated_video_call_options(
    model: Any,
    call_options: Mapping[str, Any],
) -> JsonObject:
    parameters = _video_task_options(model).get("parameters")
    supported = parameters if isinstance(parameters, Mapping) else {}
    validated: JsonObject = {}
    for field, value in call_options.items():
        spec = supported.get(field)
        if not isinstance(spec, Mapping):
            raise VideoConfigurationError(
                f"The configured Video generation model does not support {field}."
            )
        values = spec.get("values")
        comparable = str(value) if field == "duration" else value
        if isinstance(values, list | tuple) and comparable not in values:
            allowed = ", ".join(str(candidate) for candidate in values)
            raise VideoConfigurationError(
                f"{field} must be one of the values supported by the configured model: {allowed}."
            )
        validated[field] = value
    return validated


def _video_extension(media_type: str) -> str:
    return "webm" if media_type.split(";", 1)[0].strip().lower() == "video/webm" else "mp4"
