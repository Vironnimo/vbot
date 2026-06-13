"""Provider-neutral image generation execution service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.model_tasks.artifacts import StoredArtifact, TaskArtifactStore
from core.model_tasks.constants import TASK_IMAGE_GENERATION
from core.model_tasks.image_providers import ProviderImageClient
from core.model_tasks.image_types import ImageArtifact, ImageGenerationResult, JsonObject
from core.model_tasks.task_execution import TaskBindingResolver
from core.providers.task_client import TaskClientRuntime
from core.utils.errors import TaskError, VBotError
from core.utils.logging import get_logger

JsonObject = JsonObject
_LOGGER = get_logger("image")


class ImageError(TaskError):
    """Base class for expected image generation errors."""


class ImageConfigurationError(ImageError):
    """Raised when image generation is not configured."""


class ImageUnsupportedTargetError(ImageError):
    """Raised when a configured image target has no execution adapter."""


class ImageExecutionError(ImageError):
    """Raised when a provider image generation request fails."""


class ImageService:
    """Execute image generation through configured task-model bindings."""

    def __init__(
        self,
        model_tasks: Any,
        runtime: TaskClientRuntime,
        data_dir: str | Path,
    ) -> None:
        self._runtime = runtime
        self._resolver = TaskBindingResolver(
            model_tasks, configuration_error=ImageConfigurationError
        )
        self._artifacts = TaskArtifactStore(
            Path(data_dir) / "images", kind="image", error=ImageConfigurationError
        )

    async def generate(self, prompt: str) -> ImageGenerationResult:
        """Generate images from a text prompt using the configured binding."""

        normalized_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not normalized_prompt:
            raise ImageConfigurationError("Prompt must not be empty")

        _binding, options, target_ref = self._resolver.resolve(TASK_IMAGE_GENERATION)

        if target_ref.kind == "local":
            raise ImageUnsupportedTargetError(
                f"Image generation does not support local targets: {target_ref.target}"
            )

        provider_client = ProviderImageClient.from_runtime(self._runtime, target_ref)
        try:
            return await provider_client.generate(normalized_prompt, options=options)
        except ImageError:
            raise
        except VBotError as exc:
            # ProviderError / NetworkError / ProviderAuthError / … are
            # expected provider failures, not crashes.
            _LOGGER.warning(
                "Image generation failed for target=%s: %s",
                target_ref.target,
                exc,
            )
            raise ImageExecutionError(str(exc)) from exc
        except Exception as exc:
            _LOGGER.error("Image generation failed", exc_info=True)
            raise ImageExecutionError(str(exc)) from exc

    async def generate_artifacts(self, prompt: str) -> tuple[ImageArtifact, ...]:
        """Generate images and persist them as runtime artifacts."""

        result = await self.generate(prompt)
        extension = _extension_for_media_type(result.media_type)
        return tuple(
            _image_artifact(
                self._artifacts.write(
                    image_bytes,
                    extension=extension,
                    media_type=result.media_type,
                    extra_metadata={"index": idx},
                )
            )
            for idx, image_bytes in enumerate(result.images)
        )

    def get_artifact(self, artifact_id: str) -> ImageArtifact:
        """Return a persisted image artifact by id."""

        return _image_artifact(self._artifacts.read(artifact_id))


def _image_artifact(stored: StoredArtifact) -> ImageArtifact:
    index = stored.metadata.get("index", 0)
    return ImageArtifact(
        id=stored.id,
        filename=stored.filename,
        media_type=stored.media_type,
        size_bytes=stored.size_bytes,
        file_path=stored.file_path,
        index=index if isinstance(index, int) else 0,
    )


def _extension_for_media_type(media_type: str) -> str:
    """Infer a file extension from a MIME media type."""

    media_type_lower = media_type.split(";", 1)[0].lower().strip()
    if media_type_lower == "image/png":
        return "png"
    if media_type_lower in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if media_type_lower == "image/webp":
        return "webp"
    if media_type_lower == "image/gif":
        return "gif"
    if media_type_lower == "image/bmp":
        return "bmp"
    if media_type_lower == "image/svg+xml":
        return "svg"
    return "png"
