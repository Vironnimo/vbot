"""Tests for the provider-neutral image generation service."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from core.model_tasks import (
    TASK_IMAGE_GENERATION,
    TASK_IMAGE_UNDERSTANDING,
    ImageConfigurationError,
    ImageExecutionError,
    ImageInputError,
    ImageService,
    ImageUnsupportedTargetError,
    TaskModelError,
)
from core.model_tasks.image import (
    IMAGE_UNDERSTANDING_SYSTEM_PROMPT,
    split_image_call_options,
)
from core.model_tasks.image_types import ImageGenerationResult
from core.providers.errors import ProviderError
from core.storage.layout import DataDirectoryLayout


@pytest.mark.asyncio
async def test_generate_without_configured_binding_is_expected_error(tmp_path: Path) -> None:
    """A missing image-generation binding is an expected configuration error."""

    service = ImageService(_MissingModelTasks(), cast(Any, object()), tmp_path)

    with pytest.raises(ImageConfigurationError, match="configured"):
        await service.generate("a cat")


@pytest.mark.asyncio
async def test_generate_with_local_target_is_unsupported(tmp_path: Path) -> None:
    """Local image targets are out of scope for this iteration."""

    service = ImageService(_LocalModelTasks(), cast(Any, object()), tmp_path)

    with pytest.raises(ImageUnsupportedTargetError, match="local"):
        await service.generate("a cat")


@pytest.mark.asyncio
async def test_generate_artifacts_uses_canonical_image_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ImageService(_MissingModelTasks(), cast(Any, object()), tmp_path)

    async def generate(_prompt: str, **_kwargs: Any) -> ImageGenerationResult:
        return ImageGenerationResult(
            images=(b"image",),
            media_type="image/png",
            model="provider/model",
        )

    monkeypatch.setattr(service, "generate", generate)

    artifacts = await service.generate_artifacts("a cat")

    assert artifacts[0].file_path.parent == DataDirectoryLayout(tmp_path).images


@pytest.mark.asyncio
async def test_generate_logs_provider_error_at_warning_without_traceback(
    tmp_path: Path,
    caplog: Any,
) -> None:
    """A provider :class:`ProviderError` (a VBotError) logs at warning, no traceback."""

    service = ImageService(_ProviderModelTasks(), cast(Any, object()), tmp_path)
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


def test_split_routes_advertised_enum_value_to_wire() -> None:
    model = _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1", "16:9")}})

    wire_options, hints = split_image_call_options(model, {"aspect_ratio": "16:9"})

    assert wire_options == {"aspect_ratio": "16:9"}
    assert hints == []


def test_split_routes_unsupported_enum_value_to_hint() -> None:
    model = _image_model({"resolution": {"type": "enum", "values": ("1K", "2K")}})

    wire_options, hints = split_image_call_options(model, {"resolution": "4K"})

    assert wire_options == {}
    assert hints == ["4K resolution"]


def test_split_routes_unadvertised_parameter_to_hint() -> None:
    model = _image_model({"resolution": {"type": "enum", "values": ("1K", "2K")}})

    wire_options, hints = split_image_call_options(model, {"aspect_ratio": "16:9"})

    assert wire_options == {}
    assert hints == ["aspect ratio 16:9"]


def test_split_routes_open_string_spec_to_wire() -> None:
    model = _image_model({"aspect_ratio": {"type": "string"}})

    wire_options, hints = split_image_call_options(model, {"aspect_ratio": "16:9"})

    assert wire_options == {"aspect_ratio": "16:9"}
    assert hints == []


def test_split_reads_enum_values_as_list_form() -> None:
    """Fallback specs build ``values`` as lists; loaded specs freeze them to tuples."""

    model = _image_model({"resolution": {"type": "enum", "values": ["1K", "2K", "4K"]}})

    wire_options, hints = split_image_call_options(model, {"resolution": "2K"})

    assert wire_options == {"resolution": "2K"}
    assert hints == []


def test_split_without_model_makes_every_knob_a_hint() -> None:
    wire_options, hints = split_image_call_options(
        None, {"aspect_ratio": "16:9", "resolution": "4K"}
    )

    assert wire_options == {}
    assert hints == ["aspect ratio 16:9", "4K resolution"]


def test_split_without_task_options_makes_every_knob_a_hint() -> None:
    model = SimpleNamespace(capabilities=SimpleNamespace(task_options={}))

    wire_options, hints = split_image_call_options(model, {"resolution": "2K"})

    assert wire_options == {}
    assert hints == ["2K resolution"]


def test_split_ignores_blank_and_empty_call_options() -> None:
    model = _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1",)}})

    assert split_image_call_options(model, {}) == ({}, [])
    assert split_image_call_options(model, {"aspect_ratio": "  "}) == ({}, [])


def test_split_generic_hint_label_for_unknown_knob() -> None:
    wire_options, hints = split_image_call_options(None, {"color_space": "srgb"})

    assert wire_options == {}
    assert hints == ["color space srgb"]


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

    def options_with_defaults(self, _binding: object) -> dict[str, Any]:
        return dict(self._binding_options)

    def model_for_target(self, _target_ref: object) -> Any:
        self.model_for_target_calls += 1
        return self._model


@pytest.mark.asyncio
async def test_generate_native_call_value_overrides_binding_default(tmp_path: Path) -> None:
    model = _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1", "16:9")}})
    model_tasks = _RoutingModelTasks(model, binding_options={"aspect_ratio": "1:1"})
    service = ImageService(model_tasks, cast(Any, object()), tmp_path)
    client = _RecordingImageClient()

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        await service.generate("a cat", call_options={"aspect_ratio": "16:9"})

    assert client.options == {"aspect_ratio": "16:9"}
    assert client.prompt == "a cat"


@pytest.mark.asyncio
async def test_generate_non_native_call_value_hints_and_keeps_binding(tmp_path: Path) -> None:
    model = _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1", "16:9")}})
    model_tasks = _RoutingModelTasks(model, binding_options={"aspect_ratio": "1:1"})
    service = ImageService(model_tasks, cast(Any, object()), tmp_path)
    client = _RecordingImageClient()

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        await service.generate("a cat", call_options={"aspect_ratio": "21:9"})

    # A non-native value never touches the wire options — the binding default stays.
    assert client.options == {"aspect_ratio": "1:1"}
    assert client.prompt == "a cat (aspect ratio 21:9)"


@pytest.mark.asyncio
async def test_generate_without_call_options_reproduces_binding_request(tmp_path: Path) -> None:
    model = _image_model({"aspect_ratio": {"type": "enum", "values": ("1:1",)}})
    model_tasks = _RoutingModelTasks(model, binding_options={"size": "1024x1024"})
    service = ImageService(model_tasks, cast(Any, object()), tmp_path)
    client = _RecordingImageClient()

    with patch("core.model_tasks.image.ProviderImageClient.from_runtime", return_value=client):
        await service.generate("a cat")

    assert client.options == {"size": "1024x1024"}
    assert client.prompt == "a cat"
    # The no-options path must not even resolve the model.
    assert model_tasks.model_for_target_calls == 0


@pytest.mark.asyncio
async def test_generate_loads_any_reachable_local_source_image(tmp_path: Path) -> None:
    source_dir = tmp_path / "outside-workspace"
    source_dir.mkdir()
    source = source_dir / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsource-bytes")
    model_tasks = _RoutingModelTasks(_image_model({}))
    service = ImageService(model_tasks, cast(Any, object()), tmp_path / "data")
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
    service = ImageService(model_tasks, cast(Any, object()), tmp_path / "data")
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
    service = ImageService(_RoutingModelTasks(model), cast(Any, object()), tmp_path / "data")

    with pytest.raises(ImageUnsupportedTargetError, match="does not support source images"):
        await service.generate("make it rainy", source_paths=[source])


@pytest.mark.asyncio
async def test_generate_rejects_missing_or_non_image_source(tmp_path: Path) -> None:
    model_tasks = _RoutingModelTasks(_image_model({}))
    service = ImageService(model_tasks, cast(Any, object()), tmp_path / "data")

    with pytest.raises(ImageInputError, match="not found"):
        await service.generate("make it rainy", source_paths=[tmp_path / "missing.png"])

    text_file = tmp_path / "notes.txt"
    text_file.write_text("not an image", encoding="utf-8")
    with pytest.raises(ImageInputError, match="not a supported image"):
        await service.generate("make it rainy", source_paths=[text_file])


class _MissingModelTasks:
    def binding_for(self, _task_type: str) -> object:
        raise TaskModelError("No task model configured")


class _LocalModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(task_type=task_type, target="local/sd", options={})

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _ProviderModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type,
            target="openrouter/openai/gpt-image-1::api-key",
            options={},
        )

    def options_with_defaults(self, _binding: object) -> dict[str, object]:
        return {}


class _FailingProviderImageClient:
    def __init__(self, exception: Exception) -> None:
        self._exception = exception

    async def generate(self, *_args: object, **_kwargs: object) -> object:
        raise self._exception


# ---------------------------------------------------------------------------
# ImageService.analyze — isolated image-understanding execution
# ---------------------------------------------------------------------------


class _UnderstandingModelTasks:
    def __init__(
        self,
        *,
        target: str = "openrouter/vision-model::api-key",
        input_modalities: tuple[str, ...] = ("text", "image"),
        output_modalities: tuple[str, ...] = ("text",),
        task_types: tuple[str, ...] = (TASK_IMAGE_UNDERSTANDING,),
    ) -> None:
        self._target = target
        self._model = SimpleNamespace(
            capabilities=SimpleNamespace(
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                task_types=task_types,
            )
        )

    def binding_for(self, task_type: str) -> object:
        assert task_type == TASK_IMAGE_UNDERSTANDING
        return SimpleNamespace(task_type=task_type, target=self._target, options={})

    def model_for_target(self, _target_ref: object) -> Any:
        return self._model


class _UnderstandingAdapter:
    def __init__(
        self,
        response: object | None = None,
        *,
        wire_media_types: frozenset[str] = frozenset({"image/png"}),
    ) -> None:
        self.response = response or {
            "content": "Visible ingredients: flour and salt.",
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }
        self.wire_media_types = wire_media_types
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def wire_media_support(self, _model_id: str) -> frozenset[str]:
        return self.wire_media_types

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.requests.append({"messages": messages, "model_id": model_id, "kwargs": kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return cast(dict[str, Any], self.response)

    def normalize_response(
        self,
        response: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        del model_id
        return response

    async def aclose(self) -> None:
        self.closed = True


class _UnderstandingRuntime:
    def __init__(self, adapter: _UnderstandingAdapter) -> None:
        self.adapter = adapter
        self.calls: list[tuple[str, str]] = []

    def get_adapter(self, provider_id: str, connection_id: str) -> _UnderstandingAdapter:
        self.calls.append((provider_id, connection_id))
        return self.adapter


def _png(path: Path, suffix: bytes = b"pixels") -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + suffix)
    return path


@pytest.mark.asyncio
async def test_analyze_sends_fixed_isolated_prompt_and_ordered_images(
    tmp_path: Path,
) -> None:
    first = _png(tmp_path / "first.png", b"first")
    second = _png(tmp_path / "second.png", b"second")
    adapter = _UnderstandingAdapter()
    runtime = _UnderstandingRuntime(adapter)
    service = ImageService(
        _UnderstandingModelTasks(task_types=("chat", "text_output")),
        cast(Any, runtime),
        tmp_path,
    )

    result = await service.analyze(
        "List the recipe ingredients exactly.",
        image_paths=[first, second],
    )

    assert result.to_dict() == {
        "analysis": "Visible ingredients: flour and salt.",
        "model": "vision-model",
        "image_count": 2,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    assert runtime.calls == [("openrouter", "openrouter:api-key")]
    request = adapter.requests[0]
    assert request["model_id"] == "vision-model"
    assert request["kwargs"] == {"temperature": 0.0, "tools": []}
    assert request["messages"][0] == {
        "role": "system",
        "content": IMAGE_UNDERSTANDING_SYSTEM_PROMPT,
    }
    user_content = request["messages"][1]["content"]
    assert user_content[0] == {
        "type": "text",
        "text": "List the recipe ingredients exactly.",
    }
    assert [block["media_type"] for block in user_content[1:]] == [
        "image/png",
        "image/png",
    ]
    assert user_content[1]["base64"] != user_content[2]["base64"]
    assert adapter.closed is True


@pytest.mark.asyncio
async def test_analyze_rejects_non_understanding_model_and_unsupported_wire(
    tmp_path: Path,
) -> None:
    source = _png(tmp_path / "source.png")
    text_only = ImageService(
        _UnderstandingModelTasks(input_modalities=("text",)),
        cast(Any, _UnderstandingRuntime(_UnderstandingAdapter())),
        tmp_path / "text-only",
    )
    image_only = ImageService(
        _UnderstandingModelTasks(input_modalities=("image",)),
        cast(Any, _UnderstandingRuntime(_UnderstandingAdapter())),
        tmp_path / "image-only",
    )
    adapter = _UnderstandingAdapter(wire_media_types=frozenset())
    unsupported_wire = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(adapter)),
        tmp_path / "wire",
    )

    with pytest.raises(ImageUnsupportedTargetError, match="not an image-understanding"):
        await text_only.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageUnsupportedTargetError, match="not an image-understanding"):
        await image_only.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageUnsupportedTargetError, match="cannot carry"):
        await unsupported_wire.analyze("Describe it", image_paths=[source])

    assert adapter.closed is True


@pytest.mark.asyncio
async def test_analyze_rejects_missing_non_image_and_oversize_input(
    tmp_path: Path,
) -> None:
    runtime = _UnderstandingRuntime(_UnderstandingAdapter())
    service = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, runtime),
        tmp_path / "data",
        max_input_bytes=12,
    )
    text_file = tmp_path / "notes.txt"
    text_file.write_text("plain text", encoding="utf-8")
    oversize = _png(tmp_path / "large.png", b"too-many-pixels")

    with pytest.raises(ImageInputError, match="not found"):
        await service.analyze("Describe it", image_paths=[tmp_path / "missing.png"])
    with pytest.raises(ImageInputError, match="not a supported image"):
        await service.analyze("Describe it", image_paths=[text_file])
    with pytest.raises(ImageInputError, match="size limit"):
        await service.analyze("Describe it", image_paths=[oversize])

    assert runtime.calls == []


@pytest.mark.asyncio
async def test_analyze_maps_provider_failure_and_empty_output_and_closes_adapter(
    tmp_path: Path,
) -> None:
    source = _png(tmp_path / "source.png")
    failing_adapter = _UnderstandingAdapter(ProviderError("rate limited"))
    failing = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(failing_adapter)),
        tmp_path / "failing",
    )
    empty_adapter = _UnderstandingAdapter({"content": "   "})
    empty = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(empty_adapter)),
        tmp_path / "empty",
    )

    with pytest.raises(ImageExecutionError, match="rate limited"):
        await failing.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageExecutionError, match="no text analysis"):
        await empty.analyze("Describe it", image_paths=[source])

    assert failing_adapter.closed is True
    assert empty_adapter.closed is True


@pytest.mark.asyncio
async def test_analyze_requires_binding_prompt_and_images(tmp_path: Path) -> None:
    source = _png(tmp_path / "source.png")
    missing = ImageService(_MissingModelTasks(), cast(Any, object()), tmp_path / "missing")
    configured = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(_UnderstandingAdapter())),
        tmp_path / "configured",
    )

    with pytest.raises(ImageConfigurationError, match="configured"):
        await missing.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageConfigurationError, match="Prompt"):
        await configured.analyze("  ", image_paths=[source])
    with pytest.raises(ImageInputError, match="At least one"):
        await configured.analyze("Describe it", image_paths=[])
