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
    ImageConfigurationError,
    ImageExecutionError,
    ImageService,
    ImageUnsupportedTargetError,
    TaskModelError,
)
from core.model_tasks.image import split_image_call_options
from core.model_tasks.image_types import ImageGenerationResult
from core.providers.errors import ProviderError


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
            task_options={TASK_IMAGE_GENERATION: {"parameters": parameters}}
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

    async def generate(self, prompt: str, *, options: dict[str, Any]) -> ImageGenerationResult:
        self.prompt = prompt
        self.options = options
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
