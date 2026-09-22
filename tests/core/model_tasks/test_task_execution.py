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

    def validate_execution_target(self, _binding: object) -> None:
        pass

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

    with pytest.raises(_StubConfigurationError):
        resolver.resolve("speech_to_text")


def test_parse_target_wraps_malformed_target_as_configuration_error() -> None:
    resolver = TaskBindingResolver(
        _StubModelTasks(None), configuration_error=_StubConfigurationError
    )

    with pytest.raises(_StubConfigurationError):
        resolver.parse_target("not a target")


@pytest.mark.parametrize("change", ["credentials", "model", "capability", "connection"])
def test_execution_rechecks_live_target_after_binding_was_saved(change: str) -> None:
    from core.model_tasks import TASK_SPEECH_TO_TEXT, TaskModelService
    from tests.core.model_tasks.model_tasks_test_support import (
        _Credentials,
        _model,
        _Models,
        _Providers,
        _Storage,
    )

    model = _model("transcribe", (TASK_SPEECH_TO_TEXT,))
    models = _Models([model])
    credentials = _Credentials()
    storage = _Storage()
    service = TaskModelService(_Providers(), models, credentials, storage)
    service.update({TASK_SPEECH_TO_TEXT: {"target": "openrouter/transcribe::api-key"}})
    resolver = TaskBindingResolver(service, configuration_error=_StubConfigurationError)
    assert resolver.resolve(TASK_SPEECH_TO_TEXT)[2].model_id == "transcribe"

    if change == "credentials":
        credentials._granted.clear()
    elif change == "model":
        models._models.clear()
    elif change == "capability":
        model.capabilities.task_types = ()
    else:
        model.allows_connection = lambda _connection: False

    assert service.binding_for(TASK_SPEECH_TO_TEXT).target == "openrouter/transcribe::api-key"
    assert not service.binding_is_usable(TASK_SPEECH_TO_TEXT)
    with pytest.raises(_StubConfigurationError):
        resolver.resolve(TASK_SPEECH_TO_TEXT)


def test_schema_resolution_failure_is_a_task_configuration_error() -> None:
    class ChangedLocalTarget(_StubModelTasks):
        def options_with_defaults(self, binding: TaskModelBinding) -> dict[str, Any]:
            raise TaskModelError("Target no longer registered")

    binding = TaskModelBinding(task_type="speech_to_text", target="local/engine", options={})
    resolver = TaskBindingResolver(
        ChangedLocalTarget(binding), configuration_error=_StubConfigurationError
    )
    with pytest.raises(_StubConfigurationError):
        resolver.resolve("speech_to_text")


def test_execution_rejects_disabled_connection_even_while_credential_remains() -> None:
    from pathlib import Path

    from core.model_tasks import TASK_SPEECH_TO_TEXT, TaskModelService
    from core.providers.credentials import ProviderCredentialResolver
    from core.providers.providers import ProviderRegistry
    from tests.core.model_tasks.model_tasks_test_support import _model, _Models, _Storage

    providers = ProviderRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    enabled = {"openrouter:api-key": True}
    credentials = ProviderCredentialResolver(
        providers,
        process_env={"OPENROUTER_API_KEY": "test-key"},
        enabled_overrides_loader=lambda: enabled,
    )
    service = TaskModelService(
        providers, _Models([_model("transcribe", (TASK_SPEECH_TO_TEXT,))]), credentials, _Storage()
    )
    service.update({TASK_SPEECH_TO_TEXT: {"target": "openrouter/transcribe::api-key"}})
    resolver = TaskBindingResolver(service, configuration_error=_StubConfigurationError)
    resolver.resolve(TASK_SPEECH_TO_TEXT)
    enabled["openrouter:api-key"] = False
    assert credentials.has_credentials("openrouter", "openrouter:api-key")
    with pytest.raises(_StubConfigurationError):
        resolver.resolve(TASK_SPEECH_TO_TEXT)
