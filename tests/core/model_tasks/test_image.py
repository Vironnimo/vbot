"""Tests for image."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from PIL import Image

from core.model_tasks import (
    TASK_IMAGE_GENERATION,
    ImageConfigurationError,
    ImageExecutionError,
    ImageInputError,
    ImageOptionError,
    ImageOutcomeUnknownError,
    ImageService,
    ImageTooLargeError,
    ImageUnsupportedTargetError,
)
from core.model_tasks.artifacts import OutputDirectoryError
from core.model_tasks.image_profile import ImageWire, build_image_profile
from core.model_tasks.image_types import ImageGenerationResult
from core.providers.errors import ProviderError, ProviderOutcomeUnknownError
from core.utils import ids
from tests.core.model_tasks.image_test_support import (
    _MissingModelTasks,
)


@pytest.mark.asyncio
async def test_generate_without_configured_binding_is_expected_error(tmp_path: Path) -> None:
    """A missing image-generation binding is an expected configuration error."""

    service = ImageService(_MissingModelTasks(), cast(Any, object()))

    with pytest.raises(ImageConfigurationError):
        await service.generate("a cat")


def test_generation_profile_follows_configured_model() -> None:
    text_model = _image_model({})
    text_model.capabilities.input_modalities = ("text",)

    def profile(model_tasks: Any) -> Any:
        return ImageService(model_tasks, cast(Any, object())).generation_profile()

    assert profile(_RoutingModelTasks(_image_model({}))).accepts_source_images is True
    assert profile(_RoutingModelTasks(text_model)).accepts_source_images is False
    # Without a usable binding nothing is offered.
    assert profile(_MissingModelTasks()).accepts_source_images is False
    assert profile(_MissingModelTasks()).call_choices == {}


@pytest.mark.asyncio
async def test_generate_with_local_target_is_unsupported(tmp_path: Path) -> None:
    """Local image targets are out of scope for this iteration."""

    service = ImageService(_LocalModelTasks(), cast(Any, object()))

    with pytest.raises(ImageUnsupportedTargetError):
        await service.generate("a cat")


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


_PNG_3X2 = _png(3, 2)


@pytest.mark.parametrize(
    ("media_type", "extension", "images", "dimensions"),
    [
        ("image/png", ".png", (_PNG_3X2, _PNG_3X2), (3, 2)),
        # Vector images have no pixel size.
        ("image/svg+xml", ".svg", (b"<svg/>", b"<svg></svg>"), (None, None)),
    ],
)
@pytest.mark.asyncio
async def test_generate_artifacts_stores_each_image_in_the_caller_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
    extension: str,
    images: tuple[bytes, bytes],
    dimensions: tuple[int | None, int | None],
) -> None:
    service = ImageService(_MissingModelTasks(), cast(Any, object()))

    async def generate(_prompt: str, **_kwargs: Any) -> ImageGenerationResult:
        return ImageGenerationResult(images=images, media_type=media_type, model="provider/model")

    monkeypatch.setattr(service, "generate", generate)

    output_dir = tmp_path / "workspace" / "image-gen"
    artifacts = await service.generate_artifacts("a cat", output_dir=output_dir)

    assert len(artifacts) == 2
    assert {artifact.file_path.parent for artifact in artifacts} == {output_dir}
    assert {(artifact.media_type, artifact.file_path.suffix) for artifact in artifacts} == {
        (media_type, extension)
    }
    assert artifacts[0].file_path != artifacts[1].file_path
    assert [artifact.file_path.read_bytes() for artifact in artifacts] == list(images)
    assert {(artifact.width, artifact.height) for artifact in artifacts} == {dimensions}
    assert list(output_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_unusable_output_folder_fails_before_generating(tmp_path: Path) -> None:
    service = ImageService(_MissingModelTasks(), cast(Any, object()))
    occupied = tmp_path / "notes.txt"
    occupied.write_text("a file, not a folder", encoding="utf-8")

    # The missing binding would fail generation; the folder check comes first.
    with pytest.raises(OutputDirectoryError) as caught:
        await service.generate_artifacts("a cat", output_dir=occupied)

    assert caught.value.reason == "a file with that name exists"


@pytest.mark.asyncio
async def test_generate_artifacts_never_overwrite_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ImageService(_MissingModelTasks(), cast(Any, object()))

    async def generate(_prompt: str, **_kwargs: Any) -> ImageGenerationResult:
        return ImageGenerationResult(images=(b"new",), media_type="image/png", model="m")

    monkeypatch.setattr(service, "generate", generate)
    existing = tmp_path / "img_000000000001.png"
    existing.write_bytes(b"keep")
    # The first generated id collides with the existing file.
    values = iter((1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))

    [artifact] = await service.generate_artifacts("a cat", output_dir=tmp_path)

    assert artifact.id == "img_000000000002"
    assert artifact.file_path.read_bytes() == b"new"
    assert existing.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_generate_logs_provider_error_at_warning_without_traceback(
    tmp_path: Path,
    caplog: Any,
) -> None:
    """A provider :class:`ProviderError` (a VBotError) logs at warning, no traceback."""

    service = ImageService(_ProviderModelTasks(), cast(Any, object()))
    failing_client = _FailingProviderImageClient(ProviderError("rate limited"))

    with (
        patch(
            "core.model_tasks.image.ProviderImageClient.from_runtime",
            return_value=failing_client,
        ),
        caplog.at_level(logging.WARNING, logger="vbot.image"),
        pytest.raises(ImageExecutionError, match="rate limited"),
    ):
        await service.generate("a cat")

    relevant = [r for r in caplog.records if "Image generation failed" in r.getMessage()]
    assert relevant, "expected a log record for the failed image generation"
    assert all(r.levelno == logging.WARNING for r in relevant)
    assert all(r.exc_info is None for r in relevant)


@pytest.mark.asyncio
async def test_generate_redacts_pinned_account_from_error_and_log(
    tmp_path: Path,
    caplog: Any,
) -> None:
    account_id = "private_account"
    target = f"openrouter/openai/gpt-image-1::api-key:{account_id}"
    service = ImageService(_ProviderModelTasks(target=target), cast(Any, object()))
    failing_client = _FailingProviderImageClient(
        ProviderError(f"request for {target} via {account_id} failed")
    )

    with (
        patch(
            "core.model_tasks.image.ProviderImageClient.from_runtime",
            return_value=failing_client,
        ),
        caplog.at_level(logging.WARNING, logger="vbot.image"),
        pytest.raises(ImageExecutionError) as error,
    ):
        await service.generate("a cat")

    assert "openrouter/openai/gpt-image-1" in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert account_id not in str(error.value)
    assert account_id not in caplog.text
    assert target not in caplog.text
    assert "target=openrouter/openai/gpt-image-1" in caplog.text


@pytest.mark.asyncio
async def test_generate_preserves_unknown_provider_outcome(
    tmp_path: Path,
    caplog: Any,
) -> None:
    service = ImageService(_ProviderModelTasks(), cast(Any, object()))
    failing_client = _FailingProviderImageClient(
        ProviderOutcomeUnknownError("request may have completed", operation_key="image-op")
    )

    with (
        patch(
            "core.model_tasks.image.ProviderImageClient.from_runtime",
            return_value=failing_client,
        ),
        caplog.at_level(logging.WARNING, logger="vbot.image"),
        pytest.raises(ImageOutcomeUnknownError) as exc_info,
    ):
        await service.generate("a cat")

    assert exc_info.value.code == "provider_outcome_unknown"
    assert exc_info.value.operation_key == "image-op"
    assert caplog.records


def _image_model(parameters: dict[str, Any]) -> Any:
    """A minimal model double exposing image-generation parameter specs."""

    return SimpleNamespace(
        capabilities=SimpleNamespace(
            task_options={TASK_IMAGE_GENERATION: {"parameters": parameters}},
            input_modalities=("image", "text"),
        )
    )


# ---------------------------------------------------------------------------
# ImageService.generate — per-call routing integration
# ---------------------------------------------------------------------------
class _RecordingImageClient:
    def __init__(self, revised_prompt: str | None = None) -> None:
        self.revised_prompt = revised_prompt
        self.prompt: str | None = None
        self.options: dict[str, Any] | None = None
        self.input_images: tuple[Any, ...] = ()

    async def generate(
        self,
        prompt: str,
        *,
        options: dict[str, Any],
        input_images: tuple[Any, ...] = (),
    ) -> ImageGenerationResult:
        self.prompt = prompt
        self.options = options
        self.input_images = input_images
        return ImageGenerationResult(
            images=(b"x",), media_type="image/png", model="m", revised_prompt=self.revised_prompt
        )


class _RoutingModelTasks:
    def __init__(
        self,
        model: Any,
        binding_options: dict[str, Any] | None = None,
        wire: ImageWire = "openrouter",
    ) -> None:
        self._model = model
        self._binding_options = binding_options or {}
        self._wire = wire

    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type,
            target="openrouter/foo/bar::api-key",
            options={},
        )

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, Any]:
        return dict(self._binding_options)

    def model_for_target(self, _target_ref: object) -> Any:
        return self._model

    def image_profile(self, _target_ref: object) -> Any:
        return build_image_profile(self._model, self._wire)


_GPT_IMAGE_PARAMETERS = {
    "size": {"type": "string"},
    "response_format": {"type": "enum", "values": ["url", "b64_json"]},
}


@pytest.mark.parametrize(
    ("wire", "parameters", "binding_options", "call_options", "wire_options"),
    [
        pytest.param(
            "openrouter",
            {"aspect_ratio": {"type": "enum", "values": ("1:1", "16:9")}},
            {"aspect_ratio": "1:1"},
            {"aspect_ratio": "16:9"},
            {"aspect_ratio": "16:9"},
            id="call-choice-overrides-settings",
        ),
        # The profile translates the choice, and vBot always asks for Base64 images.
        pytest.param(
            "openai_images",
            _GPT_IMAGE_PARAMETERS,
            {"size": "1024x1024", "response_format": "url"},
            {"aspect_ratio": "16:9"},
            {"size": "1280x720", "response_format": "b64_json"},
            id="translated-choice-and-fixed-option",
        ),
        pytest.param(
            "openrouter",
            {},
            {"size": "1024x1024"},
            None,
            {"size": "1024x1024"},
            id="no-call-options-reproduce-binding",
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_applies_call_choices_over_settings(
    wire: ImageWire,
    parameters: dict[str, Any],
    binding_options: dict[str, Any],
    call_options: dict[str, Any] | None,
    wire_options: dict[str, Any],
) -> None:
    model_tasks = _RoutingModelTasks(_image_model(parameters), binding_options, wire)
    service = ImageService(model_tasks, cast(Any, object()))
    client = _RecordingImageClient()

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        await service.generate("a cat", call_options=call_options)

    assert (client.options, client.prompt) == (wire_options, "a cat")


@pytest.mark.asyncio
async def test_generate_refuses_a_choice_the_model_lacks_before_any_request() -> None:
    model = _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1", "16:9")}})
    service = ImageService(_RoutingModelTasks(model), cast(Any, object()))

    with (
        patch("core.model_tasks.image.ProviderImageClient.from_runtime") as client,
        pytest.raises(ImageOptionError, match="choose one of: 1:1, 16:9"),
    ):
        await service.generate("a cat", call_options={"aspect_ratio": "21:9"})

    client.assert_not_called()


@pytest.mark.parametrize(
    ("revised_prompt", "reported"),
    [
        pytest.param("A cat  ", None, id="same-as-prompt"),
        pytest.param("an original tabby cat", "an original tabby cat", id="rewritten"),
    ],
)
@pytest.mark.asyncio
async def test_generate_reports_only_a_revision_that_changes_the_prompt(
    revised_prompt: str, reported: str | None
) -> None:
    service = ImageService(_RoutingModelTasks(_image_model({})), cast(Any, object()))
    client = _RecordingImageClient(revised_prompt)

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        result = await service.generate("a cat")

    assert result.revised_prompt == reported


@pytest.mark.asyncio
async def test_generate_loads_any_reachable_local_source_image(tmp_path: Path) -> None:
    source_dir = tmp_path / "outside-workspace"
    source_dir.mkdir()
    source = source_dir / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsource-bytes")
    model_tasks = _RoutingModelTasks(_image_model({}))
    service = ImageService(model_tasks, cast(Any, object()))
    client = _RecordingImageClient()

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        await service.generate("make it rainy", source_paths=[source])

    assert len(client.input_images) == 1
    image = client.input_images[0]
    assert image.filename == "photo.png"
    assert image.media_type == "image/png"
    assert image.data == b"\x89PNG\r\n\x1a\nsource-bytes"


@pytest.mark.asyncio
async def test_generate_gives_extensionless_source_a_provider_filename(tmp_path: Path) -> None:
    source = tmp_path / "attachment-blob"
    source.write_bytes(b"\xff\xd8\xffsource-bytes")
    model_tasks = _RoutingModelTasks(_image_model({}))
    service = ImageService(model_tasks, cast(Any, object()))
    client = _RecordingImageClient()

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        await service.generate("make it rainy", source_paths=[source])

    assert client.input_images[0].filename == "attachment-blob.jpg"
    assert client.input_images[0].media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_generate_rejects_source_image_for_text_only_model(tmp_path: Path) -> None:
    source = tmp_path / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
    model = _image_model({})
    model.capabilities.input_modalities = ("text",)
    service = ImageService(_RoutingModelTasks(model), cast(Any, object()))

    with pytest.raises(ImageUnsupportedTargetError):
        await service.generate("make it rainy", source_paths=[source])


@pytest.mark.asyncio
async def test_generate_rejects_more_source_images_than_the_model_takes(tmp_path: Path) -> None:
    source = tmp_path / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
    model = _image_model({"input_references": {"type": "range", "max": 2}})
    service = ImageService(_RoutingModelTasks(model), cast(Any, object()))

    with pytest.raises(ImageTooLargeError, match="at most 2 source images, but received 3"):
        await service.generate("make it rainy", source_paths=[source] * 3)


@pytest.mark.asyncio
async def test_generate_rejects_missing_or_non_image_source(tmp_path: Path) -> None:
    model_tasks = _RoutingModelTasks(_image_model({}))
    service = ImageService(model_tasks, cast(Any, object()))

    with pytest.raises(ImageInputError):
        await service.generate("make it rainy", source_paths=[tmp_path / "missing.png"])

    text_file = tmp_path / "notes.txt"
    text_file.write_text("not an image", encoding="utf-8")
    with pytest.raises(ImageInputError):
        await service.generate("make it rainy", source_paths=[text_file])


class _LocalModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(task_type=task_type, target="local/sd", options={})

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _ProviderModelTasks:
    def __init__(
        self,
        *,
        target: str = "openrouter/openai/gpt-image-1::api-key",
    ) -> None:
        self._target = target

    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type,
            target=self._target,
            options={},
        )

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}

    def image_profile(self, _target_ref: object) -> Any:
        return build_image_profile(None, "openrouter")


class _FailingProviderImageClient:
    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    async def generate(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception
