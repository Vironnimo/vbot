"""Provider-neutral image generation execution service."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.image.providers import ProviderImageClient
from core.image.types import ImageArtifact, ImageGenerationResult, JsonObject
from core.model_tasks import TASK_IMAGE_GENERATION, TaskModelError
from core.utils.errors import VBotError
from core.utils.logging import get_logger

JsonObject = JsonObject
_LOGGER = get_logger("image")
_ARTIFACT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class ImageError(VBotError):
    """Base class for expected image generation errors."""


class ImageConfigurationError(ImageError):
    """Raised when image generation is not configured."""


class ImageUnsupportedTargetError(ImageError):
    """Raised when a configured image target has no execution adapter."""


class ImageExecutionError(ImageError):
    """Raised when a provider image generation request fails."""


@dataclass(frozen=True)
class _InternalImageArtifact:
    """Internal artifact data before path resolution."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    index: int


class ImageService:
    """Execute image generation through configured task-model bindings."""

    def __init__(
        self,
        model_tasks: Any,
        runtime: Any,
        data_dir: str | Path,
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._artifact_dir = Path(data_dir) / "images"

    async def generate(self, prompt: str) -> ImageGenerationResult:
        """Generate images from a text prompt using the configured binding."""

        normalized_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not normalized_prompt:
            raise ImageConfigurationError("Prompt must not be empty")

        binding = self._binding_for(TASK_IMAGE_GENERATION)
        options = self._model_tasks.options_with_defaults(binding)
        target_ref = self._parse_target(binding.target)

        if target_ref.kind == "local":
            raise ImageUnsupportedTargetError(
                f"Image generation does not support local targets: {target_ref.target}"
            )

        provider_client = ProviderImageClient.from_runtime(self._runtime, target_ref)
        try:
            return await provider_client.generate(normalized_prompt, options=options)
        except ImageError:
            raise
        except Exception as exc:
            _LOGGER.error("Image generation failed", exc_info=True)
            raise ImageExecutionError(str(exc)) from exc

    async def generate_artifacts(self, prompt: str) -> tuple[ImageArtifact, ...]:
        """Generate images and persist them as runtime artifacts."""

        result = await self.generate(prompt)
        artifacts: list[ImageArtifact] = []

        self._artifact_dir.mkdir(parents=True, exist_ok=True)

        for idx, image_bytes in enumerate(result.images):
            artifact_id = uuid4().hex
            extension = _extension_for_media_type(result.media_type)
            filename = f"{artifact_id}.{extension}"
            file_path = self._artifact_dir / filename
            metadata_path = self._artifact_dir / f"{artifact_id}.json"

            file_path.write_bytes(image_bytes)
            metadata = {
                "id": artifact_id,
                "filename": filename,
                "media_type": result.media_type,
                "size_bytes": len(image_bytes),
                "index": idx,
            }
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")

            artifacts.append(
                ImageArtifact(
                    id=artifact_id,
                    filename=filename,
                    media_type=result.media_type,
                    size_bytes=len(image_bytes),
                    file_path=file_path,
                    index=idx,
                )
            )

        return tuple(artifacts)

    def get_artifact(self, artifact_id: str) -> ImageArtifact:
        """Return a persisted image artifact by id."""

        if not isinstance(artifact_id, str) or _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
            raise ImageConfigurationError("Invalid image artifact id")
        metadata_path = self._artifact_dir / f"{artifact_id}.json"
        if not metadata_path.is_file():
            raise ImageConfigurationError("Image artifact not found")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ImageConfigurationError("Image artifact metadata is unreadable") from exc

        filename = metadata.get("filename")
        media_type = metadata.get("media_type")
        size_bytes = metadata.get("size_bytes")
        index = metadata.get("index", 0)
        if not isinstance(filename, str) or not isinstance(media_type, str):
            raise ImageConfigurationError("Image artifact metadata is invalid")
        file_path = self._artifact_dir / filename
        if not file_path.is_file():
            raise ImageConfigurationError("Image artifact file not found")
        return ImageArtifact(
            id=artifact_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes if isinstance(size_bytes, int) else file_path.stat().st_size,
            file_path=file_path,
            index=index if isinstance(index, int) else 0,
        )

    def _binding_for(self, task_type: str) -> Any:
        try:
            return self._model_tasks.binding_for(task_type)
        except TaskModelError as exc:
            raise ImageConfigurationError(str(exc)) from exc

    def _parse_target(self, target: str) -> Any:
        try:
            from core.model_tasks import parse_task_model_target_id

            return parse_task_model_target_id(target)
        except TaskModelError as exc:
            raise ImageConfigurationError(str(exc)) from exc


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
