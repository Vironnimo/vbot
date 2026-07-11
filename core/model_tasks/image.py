"""Provider-neutral image generation execution service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.attachments import sniff_media_type
from core.model_tasks.artifacts import StoredArtifact, TaskArtifactStore
from core.model_tasks.constants import TASK_IMAGE_GENERATION
from core.model_tasks.image_providers import ProviderImageClient
from core.model_tasks.image_types import (
    ImageArtifact,
    ImageGenerationResult,
    ImageInput,
    JsonObject,
)
from core.model_tasks.task_execution import TaskBindingResolver
from core.providers.task_client import TaskClientRuntime
from core.utils.errors import TaskError, VBotError
from core.utils.logging import get_logger

JsonObject = JsonObject
_LOGGER = get_logger("image")

# Per-call knob → prompt-hint phrasing, used when a knob cannot be routed as a
# native provider parameter. Unknown knobs fall back to a generic label.
_IMAGE_CALL_OPTION_HINTS: Mapping[str, str] = {
    "aspect_ratio": "aspect ratio {value}",
    "resolution": "{value} resolution",
}


class ImageError(TaskError):
    """Base class for expected image generation errors."""


class ImageConfigurationError(ImageError):
    """Raised when image generation is not configured."""


class ImageUnsupportedTargetError(ImageError):
    """Raised when a configured image target has no execution adapter."""


class ImageExecutionError(ImageError):
    """Raised when a provider image generation request fails."""


class ImageInputError(ImageError):
    """Raised when a local source image cannot be loaded for editing."""


class ImageService:
    """Execute image generation through configured task-model bindings."""

    def __init__(
        self,
        model_tasks: Any,
        runtime: TaskClientRuntime,
        data_dir: str | Path,
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._resolver = TaskBindingResolver(
            model_tasks, configuration_error=ImageConfigurationError
        )
        self._artifacts = TaskArtifactStore(
            Path(data_dir) / "images", kind="image", error=ImageConfigurationError
        )

    async def generate(
        self,
        prompt: str,
        *,
        call_options: Mapping[str, Any] | None = None,
        source_paths: Sequence[str | Path] | None = None,
    ) -> ImageGenerationResult:
        """Generate or edit images using the configured binding.

        ``call_options`` carries the agent's per-call intent knobs (aspect
        ratio, resolution). Each is routed against the resolved model's
        advertised image parameters: a natively supported value overwrites the
        binding default in the wire options, while an unsupported value is
        appended to the prompt as a best-effort hint instead. Empty or absent
        ``call_options`` reproduces the request the binding alone would make.

        ``source_paths`` may name any local image file reachable by the process.
        Each file is read and sent to the configured external provider. An empty
        sequence keeps the text-to-image path unchanged.
        """

        normalized_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not normalized_prompt:
            raise ImageConfigurationError("Prompt must not be empty")

        _binding, options, target_ref = self._resolver.resolve(TASK_IMAGE_GENERATION)

        if target_ref.kind == "local":
            raise ImageUnsupportedTargetError(
                f"Image generation does not support local targets: {target_ref.target}"
            )

        model = None
        if call_options or source_paths:
            model = self._model_tasks.model_for_target(target_ref)

        if source_paths and model is not None:
            input_modalities = getattr(getattr(model, "capabilities", None), "input_modalities", ())
            if "image" not in input_modalities:
                raise ImageUnsupportedTargetError(
                    f"Configured image model does not support source images: {target_ref.target}"
                )

        wire_options, prompt_hints = split_image_call_options(model, call_options or {})
        merged_options = {**options, **wire_options}
        request_prompt = _prompt_with_hints(normalized_prompt, prompt_hints)
        input_images = _load_image_inputs(source_paths or ())

        provider_client = ProviderImageClient.from_runtime(self._runtime, target_ref)
        try:
            if input_images:
                return await provider_client.generate(
                    request_prompt,
                    options=merged_options,
                    input_images=input_images,
                )
            return await provider_client.generate(request_prompt, options=merged_options)
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

    async def generate_artifacts(
        self,
        prompt: str,
        *,
        call_options: Mapping[str, Any] | None = None,
        source_paths: Sequence[str | Path] | None = None,
    ) -> tuple[ImageArtifact, ...]:
        """Generate images and persist them as runtime artifacts."""

        result = await self.generate(
            prompt,
            call_options=call_options,
            source_paths=source_paths,
        )
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


def split_image_call_options(
    model: Any | None,
    call_options: Mapping[str, Any],
) -> tuple[JsonObject, list[str]]:
    """Route each per-call image knob to a native wire option or a prompt hint.

    A knob goes native (into the returned wire options) only when the model
    advertises that image parameter *and* the requested value is acceptable for
    it — the value is in the parameter's ``values`` enum, or the spec is an
    open/free-form one (``string``/``boolean``) with no fixed value set.
    Everything else — an unadvertised parameter, a value the enum does not
    list, a model with no ``task_options``, or ``model is None`` — becomes a
    best-effort prompt hint. Because a value is only sent when the catalog
    confirms it, an unsupported knob can never trigger a provider error.

    Blank knob values are treated as omitted; empty ``call_options`` yields
    ``({}, [])``.
    """

    parameters = _image_generation_parameters(model)
    wire_options: JsonObject = {}
    prompt_hints: list[str] = []
    for name, raw_value in call_options.items():
        value = raw_value.strip() if isinstance(raw_value, str) else raw_value
        if value is None or value == "":
            continue
        if _value_routes_native(parameters.get(name), value):
            wire_options[name] = value
        else:
            prompt_hints.append(_image_call_option_hint(name, value))
    return wire_options, prompt_hints


def _image_generation_parameters(model: Any | None) -> Mapping[str, Any]:
    """Return the model's advertised image-generation parameter specs, if any."""

    if model is None:
        return {}
    task_options = getattr(getattr(model, "capabilities", None), "task_options", None)
    if not isinstance(task_options, Mapping):
        return {}
    image_options = task_options.get(TASK_IMAGE_GENERATION)
    if not isinstance(image_options, Mapping):
        return {}
    parameters = image_options.get("parameters")
    return parameters if isinstance(parameters, Mapping) else {}


def _value_routes_native(spec: Any, value: Any) -> bool:
    """Whether *value* may be sent as a native parameter given its typed *spec*."""

    if not isinstance(spec, Mapping):
        return False
    values = spec.get("values")
    if isinstance(values, list | tuple):
        # Enum: only a value the catalog lists is safe to send natively.
        return any(str(value) == str(candidate) for candidate in values)
    # An open spec (string/boolean = "supported, value free-form") accepts any
    # value; a range or unknown spec can't confirm a free-form knob value, so
    # it falls to a safe prompt hint.
    return spec.get("type") in {"string", "boolean"}


def _image_call_option_hint(name: str, value: Any) -> str:
    template = _IMAGE_CALL_OPTION_HINTS.get(name)
    if template is not None:
        return template.format(value=value)
    return f"{name.replace('_', ' ')} {value}"


def _prompt_with_hints(prompt: str, hints: list[str]) -> str:
    """Append best-effort option hints to the prompt as a trailing parenthetical."""

    if not hints:
        return prompt
    return f"{prompt} ({', '.join(hints)})"


def _load_image_inputs(source_paths: Sequence[str | Path]) -> tuple[ImageInput, ...]:
    """Read and validate local source images without imposing a path allowlist."""

    inputs: list[ImageInput] = []
    for source_path in source_paths:
        path = Path(source_path).expanduser().resolve()
        if not path.exists():
            raise ImageInputError(f"Source image not found: {path}")
        if not path.is_file():
            raise ImageInputError(f"Source image path is not a file: {path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ImageInputError(f"Cannot read source image {path}: {exc}") from exc

        media_type = sniff_media_type(data, path.name)
        if not media_type.startswith("image/"):
            raise ImageInputError(f"Source file is not a supported image: {path}")
        inputs.append(
            ImageInput(
                filename=_input_filename(path, media_type),
                media_type=media_type,
                data=data,
            )
        )
    return tuple(inputs)


def _input_filename(path: Path, media_type: str) -> str:
    """Give extensionless attachment blobs a provider-readable filename."""

    if path.suffix:
        return path.name
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(media_type, "")
    return f"{path.name}{extension}"


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
