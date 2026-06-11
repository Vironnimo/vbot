"""Tests for the shared task binding resolver."""

from __future__ import annotations

from typing import Any

import pytest

from core.model_tasks import TaskModelBinding, TaskModelError
from core.model_tasks.task_execution import TaskBindingResolver
from core.utils.errors import TaskError


class _StubConfigurationError(TaskError):
    pass


class _StubModelTasks:
    def __init__(self, binding: TaskModelBinding | None) -> None:
        self._binding = binding

    def binding_for(self, task_type: str) -> TaskModelBinding:
        if self._binding is None:
            raise TaskModelError(f"No task model configured for {task_type}")
        return self._binding

    def options_with_defaults(self, binding: TaskModelBinding) -> dict[str, Any]:
        return {"merged": True, **dict(binding.options)}


def test_resolve_returns_binding_options_and_parsed_target() -> None:
    binding = TaskModelBinding(
        task_type="speech_to_text",
        target="openai/whisper-1::api-key",
        options={"language": "de"},
    )
    resolver = TaskBindingResolver(
        _StubModelTasks(binding), configuration_error=_StubConfigurationError
    )

    resolved_binding, options, target_ref = resolver.resolve("speech_to_text")

    assert resolved_binding is binding
    assert options == {"merged": True, "language": "de"}
    assert target_ref.kind == "provider"
    assert target_ref.provider_id == "openai"
    assert target_ref.model_id == "whisper-1"


def test_resolve_wraps_missing_binding_as_configuration_error() -> None:
    resolver = TaskBindingResolver(
        _StubModelTasks(None), configuration_error=_StubConfigurationError
    )

    with pytest.raises(_StubConfigurationError, match="No task model configured"):
        resolver.resolve("speech_to_text")


def test_parse_target_wraps_malformed_target_as_configuration_error() -> None:
    resolver = TaskBindingResolver(
        _StubModelTasks(None), configuration_error=_StubConfigurationError
    )

    with pytest.raises(_StubConfigurationError):
        resolver.parse_target("not a target")
