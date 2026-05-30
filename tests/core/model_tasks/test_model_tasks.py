"""Tests for task-model binding and target discovery."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.model_tasks import (
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
    TaskModelBinding,
    TaskModelService,
    TaskModelValidationError,
    parse_task_model_target_id,
)


def test_parse_openrouter_target_with_nested_model_id() -> None:
    ref = parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::api-key")

    assert ref.provider_id == "openrouter"
    assert ref.model_id == "openai/gpt-4o-transcribe"
    assert ref.connection_id == "openrouter:api-key"
    assert ref.local_connection_id == "api-key"


def test_parse_provider_target_requires_connection_suffix() -> None:
    with pytest.raises(TaskModelValidationError, match="connection suffix"):
        parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe")


def test_list_targets_filters_by_task_type_and_credentials() -> None:
    providers = _Providers()
    models = _Models(
        [
            _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
            _model("openai/gpt-4o-mini-tts", (TASK_TEXT_TO_SPEECH,)),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert [target.id for target in targets] == ["openrouter/openai/gpt-4o-transcribe::api-key"]
    assert targets[0].connection_id == "openrouter:api-key"
    assert targets[0].label == "OpenRouter / OpenAI GPT-4o Transcribe"


def test_options_with_defaults_merges_binding_values() -> None:
    service = TaskModelService(_Providers(), _Models([]), _Credentials(), _Storage())
    binding = TaskModelBinding(
        task_type=TASK_TEXT_TO_SPEECH,
        target="openrouter/openai/gpt-4o-mini-tts::api-key",
        options={"voice": "nova"},
    )

    options = service.options_with_defaults(binding)

    assert options["voice"] == "nova"
    assert options["response_format"] == "mp3"
    assert options["speed"] == 1.0


def _model(model_id: str, task_types: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        model_id=model_id,
        name="OpenAI GPT-4o Transcribe"
        if TASK_SPEECH_TO_TEXT in task_types
        else "OpenAI GPT-4o Mini TTS",
        capabilities=SimpleNamespace(task_types=task_types),
    )


class _Providers:
    def list_ids(self) -> list[str]:
        return ["openrouter"]

    def get(self, _provider_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id="openrouter",
            name="OpenRouter",
            connections=[SimpleNamespace(id="api-key", label="API Key")],
        )


class _Models:
    def __init__(self, models: list[SimpleNamespace]) -> None:
        self._models = models

    def list_for_provider(self, _provider_id: str) -> list[SimpleNamespace]:
        return self._models


class _Credentials:
    def has_credentials(self, _provider_id: str, connection_id: str) -> bool:
        return connection_id == "openrouter:api-key"


class _Storage:
    def load_model_task_settings(self) -> dict[str, object]:
        return {}

    def update_model_task_settings(self, model_tasks: object) -> object:
        return model_tasks
