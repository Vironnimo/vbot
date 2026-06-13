"""Tests for the provider-neutral image generation service."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from core.model_tasks import (
    ImageConfigurationError,
    ImageExecutionError,
    ImageService,
    ImageUnsupportedTargetError,
    TaskModelError,
)
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
