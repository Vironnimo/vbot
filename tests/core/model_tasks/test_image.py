"""Tests for image."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from core.model_tasks import (
    TASK_IMAGE_GENERATION,
    ImageConfigurationError,
    ImageExecutionError,
    ImageInputError,
    ImageOutcomeUnknownError,
    ImageService,
    ImageUnsupportedTargetError,
)
from core.model_tasks.image import (
    split_image_call_options,
)
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


def test_generation_source_image_capability_follows_configured_model(tmp_path: Path) -> None:
    image_model = _image_model({})
    text_model = _image_model({})
    text_model.capabilities.input_modalities = ("text",)

    assert (
        ImageService(
            _RoutingModelTasks(image_model),
            cast(Any, object()),
        ).generation_supports_source_images()
        is True
    )
    assert (
        ImageService(
            _RoutingModelTasks(text_model),
            cast(Any, object()),
        ).generation_supports_source_images()
        is False
    )
    assert (
        ImageService(
            _MissingModelTasks(),
            cast(Any, object()),
        ).generation_supports_source_images()
        is False
    )
    assert (
        ImageService(
            _LocalModelTasks(),
            cast(Any, object()),
        ).generation_supports_source_images()
        is False
    )


@pytest.mark.asyncio
async def test_generate_with_local_target_is_unsupported(tmp_path: Path) -> None:
    """Local image targets are out of scope for this iteration."""

    service = ImageService(_LocalModelTasks(), cast(Any, object()))

    with pytest.raises(ImageUnsupportedTargetError):
        await service.generate("a cat")


@pytest.mark.parametrize(
    ("media_type", "extension"),
    [("image/png", ".png"), ("image/svg+xml", ".svg"), ("image/webp", ".webp")],
)
@pytest.mark.asyncio
async def test_generate_artifacts_stores_each_image_in_the_caller_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
    extension: str,
) -> None:
    service = ImageService(_MissingModelTasks(), cast(Any, object()))

    async def generate(_prompt: str, **_kwargs: Any) -> ImageGenerationResult:
        return ImageGenerationResult(
            images=(b"first image", b"second image"),
            media_type=media_type,
            model="provider/model",
        )

    monkeypatch.setattr(service, "generate", generate)

    output_dir = tmp_path / "workspace" / "image-gen"
    artifacts = await service.generate_artifacts("a cat", output_dir=output_dir)

    assert len(artifacts) == 2
    assert {artifact.file_path.parent for artifact in artifacts} == {output_dir}
    assert {(artifact.media_type, artifact.file_path.suffix) for artifact in artifacts} == {
        (media_type, extension)
    }
    assert artifacts[0].file_path != artifacts[1].file_path
    assert [artifact.file_path.read_bytes() for artifact in artifacts] == [
        b"first image",
        b"second image",
    ]
    assert list(output_dir.glob("*.json")) == []


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


# ---------------------------------------------------------------------------
# split_image_call_options — pure per-call routing
# ---------------------------------------------------------------------------
def _image_model(parameters: dict[str, Any]) -> Any:
    """A minimal model double exposing image-generation parameter specs."""

    return SimpleNamespace(
        capabilities=SimpleNamespace(
            task_options={TASK_IMAGE_GENERATION: {"parameters": parameters}},
            input_modalities=("image", "text"),
        )
    )


_NO_TASK_OPTIONS = SimpleNamespace(capabilities=SimpleNamespace(task_options={}))


@pytest.mark.parametrize(
    ("model", "call_options", "wire_options", "hints"),
    [
        pytest.param(
            _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1", "16:9")}}),
            {"aspect_ratio": "16:9"},
            {"aspect_ratio": "16:9"},
            [],
            id="advertised-enum-value",
        ),
        # Fallback specs build ``values`` as lists; loaded specs freeze them to tuples.
        pytest.param(
            _image_model({"resolution": {"type": "enum", "values": ["1K", "2K", "4K"]}}),
            {"resolution": "2K"},
            {"resolution": "2K"},
            [],
            id="advertised-enum-value-in-list-form",
        ),
        pytest.param(
            _image_model({"aspect_ratio": {"type": "string"}}),
            {"aspect_ratio": "16:9"},
            {"aspect_ratio": "16:9"},
            [],
            id="open-string-spec",
        ),
        pytest.param(
            _image_model({"resolution": {"type": "enum", "values": ("1K", "2K")}}),
            {"resolution": "4K"},
            {},
            ["4K resolution"],
            id="unsupported-enum-value",
        ),
        pytest.param(
            _image_model({"resolution": {"type": "enum", "values": ("1K", "2K")}}),
            {"aspect_ratio": "16:9"},
            {},
            ["aspect ratio 16:9"],
            id="unadvertised-parameter",
        ),
        pytest.param(
            None,
            {"aspect_ratio": "16:9", "resolution": "4K"},
            {},
            ["aspect ratio 16:9", "4K resolution"],
            id="no-model",
        ),
        pytest.param(
            _NO_TASK_OPTIONS, {"resolution": "2K"}, {}, ["2K resolution"], id="no-task-options"
        ),
        pytest.param(None, {"color_space": "srgb"}, {}, ["color space srgb"], id="unknown-knob"),
        pytest.param(
            _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1",)}}),
            {"aspect_ratio": "  "},
            {},
            [],
            id="blank-value",
        ),
        pytest.param(
            _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1",)}}),
            {},
            {},
            [],
            id="no-call-options",
        ),
    ],
)
def test_call_options_go_to_the_wire_only_when_the_model_supports_them(
    model: Any, call_options: dict[str, Any], wire_options: dict[str, Any], hints: list[str]
) -> None:
    assert split_image_call_options(model, call_options) == (wire_options, hints)


# ---------------------------------------------------------------------------
# ImageService.generate — per-call routing integration
# ---------------------------------------------------------------------------
class _RecordingImageClient:
    def __init__(self) -> None:
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
        return ImageGenerationResult(images=(b"x",), media_type="image/png", model="m")


class _RoutingModelTasks:
    def __init__(self, model: Any, binding_options: dict[str, Any] | None = None) -> None:
        self._model = model
        self._binding_options = binding_options or {}
        self.model_for_target_calls = 0

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
        self.model_for_target_calls += 1
        return self._model


@pytest.mark.parametrize(
    ("binding_options", "call_options", "wire_options", "prompt"),
    [
        pytest.param(
            {"aspect_ratio": "1:1"},
            {"aspect_ratio": "16:9"},
            {"aspect_ratio": "16:9"},
            "a cat",
            id="native-call-value-overrides-binding",
        ),
        # A non-native value becomes a prompt hint and keeps the binding default.
        pytest.param(
            {"aspect_ratio": "1:1"},
            {"aspect_ratio": "21:9"},
            {"aspect_ratio": "1:1"},
            "a cat (aspect ratio 21:9)",
            id="non-native-call-value-hints",
        ),
        pytest.param(
            {"size": "1024x1024"},
            None,
            {"size": "1024x1024"},
            "a cat",
            id="no-call-options-reproduce-binding",
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_routes_per_call_options(
    binding_options: dict[str, Any],
    call_options: dict[str, Any] | None,
    wire_options: dict[str, Any],
    prompt: str,
) -> None:
    model = _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1", "16:9")}})
    model_tasks = _RoutingModelTasks(model, binding_options=binding_options)
    service = ImageService(model_tasks, cast(Any, object()))
    client = _RecordingImageClient()

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        await service.generate("a cat", call_options=call_options)

    assert (client.options, client.prompt) == (wire_options, prompt)
    # Without call options the model is not even resolved.
    assert model_tasks.model_for_target_calls == (0 if call_options is None else 1)


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


class _FailingProviderImageClient:
    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    async def generate(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception
