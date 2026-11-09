"""Tests for the shared task binding resolver."""

from __future__ import annotations

import threading
from typing import Any, override

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


@pytest.mark.asyncio
async def test_resolve_returns_binding_options_and_parsed_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = TaskModelBinding(
        task_type="speech_to_text",
        target="openai/whisper-1::api-key",
        options={"language": "de"},
    )
    service = _StubModelTasks(binding)
    loop_thread = threading.get_ident()

    def validate_execution_target(_binding: object) -> None:
        assert threading.get_ident() != loop_thread

    monkeypatch.setattr(service, "validate_execution_target", validate_execution_target)
    resolver = TaskBindingResolver(service, configuration_error=_StubConfigurationError)

    resolved_binding, options, target_ref = await resolver.resolve_async("speech_to_text")

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
        @override
        def options_with_defaults(self, binding: TaskModelBinding) -> dict[str, Any]:
            raise TaskModelError("Target no longer registered")

    binding = TaskModelBinding(task_type="speech_to_text", target="local/engine", options={})
    resolver = TaskBindingResolver(
        ChangedLocalTarget(binding), configuration_error=_StubConfigurationError
    )
    with pytest.raises(_StubConfigurationError):
        resolver.resolve("speech_to_text")


def test_missing_local_target_is_a_task_configuration_error() -> None:
    from core.model_tasks import TASK_SPEECH_TO_TEXT, TaskModelService
    from tests.core.model_tasks.model_tasks_test_support import (
        _Credentials,
        _Models,
        _Providers,
        _Storage,
    )

    service = TaskModelService(
        _Providers(),
        _Models([]),
        _Credentials(),
        _Storage({TASK_SPEECH_TO_TEXT: {"target": "local/missing", "options": {}}}),
    )
    resolver = TaskBindingResolver(service, configuration_error=_StubConfigurationError)

    assert not service.binding_is_usable(TASK_SPEECH_TO_TEXT)
    with pytest.raises(_StubConfigurationError):
        resolver.resolve(TASK_SPEECH_TO_TEXT)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rate_limit", "network", "unavailable"])
async def test_fallbacks_use_their_options_and_remember_failed_routes(monkeypatch, failure):
    from core.model_tasks import task_execution
    from core.providers.errors import NetworkError, ProviderRateLimitError

    now = [0.0]
    monkeypatch.setattr(task_execution, "_clock", lambda: now[0])
    backup = TaskModelBinding("text_to_speech", "local/backup", {"voice": "backup"})
    primary = TaskModelBinding("text_to_speech", "local/primary", {"voice": "primary"}, (backup,))
    service = _StubModelTasks(primary)
    resolver = TaskBindingResolver(service, configuration_error=_StubConfigurationError)
    calls = []
    recovered = False

    async def execute(attempt):
        calls.append((attempt.target.local_id, attempt.options["voice"]))
        if attempt.target.local_id == "primary" and not recovered:
            if failure == "rate_limit":
                raise ProviderRateLimitError("busy")
            if failure == "unavailable":
                raise TaskModelError("removed")
            raise NetworkError("unreachable")
        return attempt.options["voice"]

    assert await resolver.execute("text_to_speech", execute) == "backup"
    assert calls == [("primary", "primary")] * (2 if failure == "network" else 1) + [
        ("backup", "backup")
    ]
    calls.clear()
    assert await resolver.execute("text_to_speech", execute) == "backup"
    assert calls == [("backup", "backup")]
    assert service._binding is primary
    now[0] = 901
    recovered = True
    assert await resolver.execute("text_to_speech", execute) == "primary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["ambiguous", "refused", "input", "cancelled", "sealed", "unexpected"]
)
async def test_fallback_never_replays_an_unsafe_or_unrelated_failure(failure):
    import asyncio

    from core.providers.errors import (
        NetworkError,
        ProviderContentRefusedError,
        ProviderOutcomeUnknownError,
    )

    primary = TaskModelBinding(
        "text_to_speech", "local/primary", {}, (TaskModelBinding("text_to_speech", "local/backup"),)
    )
    resolver = TaskBindingResolver(
        _StubModelTasks(primary), configuration_error=_StubConfigurationError
    )
    calls = []

    async def execute(attempt):
        calls.append(attempt.target.local_id)
        if failure == "sealed":
            attempt.seal()
            raise NetworkError("stream lost")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        if failure == "input":
            raise ValueError("bad input")
        if failure == "unexpected":
            raise RuntimeError("bug")
        cause = (
            ProviderOutcomeUnknownError("accepted", operation_key="test")
            if failure == "ambiguous"
            else ProviderContentRefusedError("refused")
        )
        try:
            raise cause
        except Exception as exc:
            raise NetworkError("outer wrapper") from exc

    expected = {
        "cancelled": asyncio.CancelledError,
        "input": ValueError,
        "unexpected": RuntimeError,
    }.get(failure, NetworkError)
    with pytest.raises(expected):
        await resolver.execute("text_to_speech", execute)
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_unusable_and_incompatible_candidates_do_not_block_later_candidates(monkeypatch):
    from core.model_tasks.task_execution import TaskCandidateMismatchError

    bindings = tuple(
        TaskModelBinding("image_generation", f"local/{name}")
        for name in ("missing", "incompatible", "ready")
    )
    primary = TaskModelBinding("image_generation", bindings[0].target, {}, bindings[1:])
    service = _StubModelTasks(primary)

    def validate(binding):
        if binding.target == "local/missing":
            raise TaskModelError("removed")

    monkeypatch.setattr(service, "validate_execution_target", validate)
    resolver = TaskBindingResolver(service, configuration_error=_StubConfigurationError)
    calls = []

    async def execute(attempt):
        calls.append(attempt.target.local_id)
        if attempt.target.local_id == "incompatible":
            raise TaskCandidateMismatchError(ValueError("unsupported size"))
        return "image"

    assert await resolver.execute("image_generation", execute) == "image"
    assert calls == ["incompatible", "ready"]
    assert resolver.resolve("image_generation")[2].local_id == "incompatible"
