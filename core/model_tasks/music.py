"""Provider-neutral Music generation execution service."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.model_tasks.artifacts import GeneratedMediaArtifact, write_generated_media_artifact
from core.model_tasks.constants import TASK_MUSIC_GENERATION
from core.model_tasks.image import load_image_inputs
from core.model_tasks.model_tasks import TaskModelTargetRef, model_supports_task
from core.model_tasks.music_providers import ProviderMusicClient
from core.model_tasks.music_types import MusicGenerationResult
from core.model_tasks.task_execution import TaskBindingResolver
from core.providers.errors import ProviderOutcomeUnknownError
from core.providers.task_client import TaskClientRuntime
from core.utils.errors import TaskError, VBotError


class MusicError(TaskError):
    """Base class for expected Music generation errors."""

    code = "music_error"
    retryable = False


class MusicConfigurationError(MusicError):
    """Raised when Music generation is not configured or usable."""


class MusicExecutionError(MusicError):
    """Raised when an OpenRouter Music request fails."""

    code = "provider_error"


class MusicOutcomeUnknownError(MusicExecutionError):
    """Raised when Music generation may have completed at the provider."""

    code = ProviderOutcomeUnknownError.code


class MusicService:
    """Execute ``music_generation`` through its configured Task Model."""

    def __init__(self, model_tasks: Any, runtime: TaskClientRuntime) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._resolver = TaskBindingResolver(
            model_tasks,
            configuration_error=MusicConfigurationError,
        )

    def generation_supports_source_images(self) -> bool:
        """Return whether the configured Music Model accepts image input."""

        model = self._configured_model()
        input_modalities = getattr(getattr(model, "capabilities", None), "input_modalities", ())
        return "image" in input_modalities

    async def generate(
        self,
        prompt: str,
        *,
        source_paths: Sequence[str | Path] = (),
    ) -> MusicGenerationResult:
        normalized_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not normalized_prompt:
            raise MusicConfigurationError("Prompt must not be empty")

        try:
            _binding, options, target_ref = self._resolver.resolve(TASK_MUSIC_GENERATION)
        except MusicConfigurationError as exc:
            raise MusicConfigurationError(
                "Music generation is not configured. Select a Music generation "
                "Task Model in Settings."
            ) from exc
        model = self._validated_model(target_ref)
        if source_paths and "image" not in model.capabilities.input_modalities:
            raise MusicConfigurationError(
                "The configured Music generation model does not accept reference images."
            )
        images = await asyncio.to_thread(load_image_inputs, source_paths)

        client = ProviderMusicClient.from_runtime(self._runtime, target_ref)
        try:
            return await client.generate(
                normalized_prompt,
                options=options,
                input_images=images,
            )
        except MusicError:
            raise
        except ProviderOutcomeUnknownError as exc:
            raise MusicOutcomeUnknownError(str(exc)) from exc
        except VBotError as exc:
            raise MusicExecutionError(str(exc)) from exc
        except Exception as exc:
            raise MusicExecutionError(str(exc)) from exc

    async def generate_artifact(
        self,
        prompt: str,
        *,
        output_dir: str | Path,
        source_paths: Sequence[str | Path] = (),
    ) -> GeneratedMediaArtifact:
        """Generate Music and persist it in the caller-owned directory."""

        result = await self.generate(prompt, source_paths=source_paths)
        return write_generated_media_artifact(
            result.data,
            output_dir=output_dir,
            extension=_music_extension(result.media_type),
            media_type=result.media_type,
            error=MusicExecutionError,
        )

    def _configured_model(self) -> Any | None:
        try:
            binding = self._resolver.binding_for(TASK_MUSIC_GENERATION)
            target_ref = self._resolver.parse_target(binding.target)
        except MusicConfigurationError:
            return None
        if target_ref.kind != "provider" or target_ref.provider_id != "openrouter":
            return None
        return self._model_tasks.model_for_target(target_ref)

    def _validated_model(self, target_ref: TaskModelTargetRef) -> Any:
        if target_ref.kind != "provider" or target_ref.provider_id != "openrouter":
            raise MusicConfigurationError(
                "The configured provider does not support Music generation."
            )
        model = self._model_tasks.model_for_target(target_ref)
        if model is None:
            raise MusicConfigurationError(
                "The configured Music generation model is no longer available. "
                "Select another Task Model in Settings."
            )
        if not model_supports_task(model, TASK_MUSIC_GENERATION):
            raise MusicConfigurationError(
                "The configured provider does not support Music generation."
            )
        return model


def _music_extension(media_type: str) -> str:
    normalized = media_type.split(";", 1)[0].strip().lower()
    return {
        "audio/wav": "wav",
        "audio/flac": "flac",
        "audio/ogg": "ogg",
    }.get(normalized, "mp3")
