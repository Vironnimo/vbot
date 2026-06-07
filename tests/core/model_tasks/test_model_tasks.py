"""Tests for task-model binding and target discovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from core.model_tasks import (
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
    LocalTaskTargetDescriptor,
    LocalTaskTargetRegistry,
    TaskModelBinding,
    TaskModelService,
    TaskModelValidationError,
    parse_task_model_target_id,
)
from core.models import Model, ModelQuery


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


def test_list_targets_for_tts_returns_only_tts_models() -> None:
    providers = _Providers()
    models = _Models(
        [
            _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
            _model("openai/gpt-4o-mini-tts", (TASK_TEXT_TO_SPEECH,)),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_TEXT_TO_SPEECH)

    assert [target.id for target in targets] == ["openrouter/openai/gpt-4o-mini-tts::api-key"]
    assert targets[0].label == "OpenRouter / OpenAI GPT-4o Mini TTS"


def test_list_targets_for_image_generation() -> None:
    providers = _Providers()
    models = _Models(
        [
            _model("dall-e-3", (TASK_IMAGE_GENERATION,), name="DALL-E 3"),
            _model("gpt-image-1", (TASK_IMAGE_GENERATION,), name="GPT Image 1"),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_IMAGE_GENERATION)

    assert [target.id for target in targets] == [
        "openrouter/dall-e-3::api-key",
        "openrouter/gpt-image-1::api-key",
    ]


def test_list_targets_expands_multiple_usable_connections() -> None:
    providers = _Providers(
        providers=[
            _provider(
                "openrouter",
                "OpenRouter",
                [("api-key", "API Key"), ("oauth", "OAuth")],
            )
        ]
    )
    models = _Models([_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))])
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openrouter:api-key", "openrouter:oauth"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # Multi-connection expansion: one target per usable connection, sorted
    # by (kind, label.lower(), id) — alphabetical on label puts "API Key" before "OAuth".
    assert [target.id for target in targets] == [
        "openrouter/openai/gpt-4o-transcribe::api-key",
        "openrouter/openai/gpt-4o-transcribe::oauth",
    ]
    assert [target.label for target in targets] == [
        "OpenRouter / OpenAI GPT-4o Transcribe (API Key)",
        "OpenRouter / OpenAI GPT-4o Transcribe (OAuth)",
    ]


def test_list_targets_single_usable_connection_omits_label_suffix() -> None:
    """With one usable connection, the label is the bare model label — unchanged from before."""

    providers = _Providers(
        providers=[
            _provider(
                "openrouter",
                "OpenRouter",
                [("api-key", "API Key"), ("oauth", "OAuth")],
            )
        ]
    )
    models = _Models([_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))])
    service = TaskModelService(
        providers,
        models,
        # Only one connection has credentials — only it is expanded.
        _Credentials(granted={"openrouter:api-key"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert [target.id for target in targets] == [
        "openrouter/openai/gpt-4o-transcribe::api-key",
    ]
    assert targets[0].label == "OpenRouter / OpenAI GPT-4o Transcribe"


def test_list_targets_skips_provider_without_credentials() -> None:
    providers = _Providers(
        providers=[
            _provider("openrouter", "OpenRouter", [("api-key", "API Key")]),
            _provider("unauth", "Unauth Provider", [("api-key", "API Key")]),
        ]
    )
    models = _Models(
        [
            _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
            _model(
                "openai/gpt-4o-transcribe",
                (TASK_SPEECH_TO_TEXT,),
                name="Unauth Transcribe",
                provider_id="unauth",
            ),
        ]
    )
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openrouter:api-key"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # Credential gating removes "unauth" entirely — both from provider
    # iteration and from query results.
    assert [target.id for target in targets] == ["openrouter/openai/gpt-4o-transcribe::api-key"]


def test_list_targets_merges_local_targets_with_provider_targets() -> None:
    providers = _Providers()
    models = _Models([_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))])
    local_registry = LocalTaskTargetRegistry(
        [
            LocalTaskTargetDescriptor(
                id="whisper-local",
                label="Local Whisper",
                task_types=(TASK_SPEECH_TO_TEXT,),
            )
        ]
    )
    service = TaskModelService(
        providers,
        models,
        _Credentials(),
        _Storage(),
        local_targets=local_registry,
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # Sorted by (kind, label.lower(), id): "local" < "provider".
    assert [(target.kind, target.id) for target in targets] == [
        ("local", "local/whisper-local"),
        ("provider", "openrouter/openai/gpt-4o-transcribe::api-key"),
    ]


def test_list_targets_query_delegation_does_not_reach_provider_without_match() -> None:
    """When the core query excludes all models for a provider, no targets are produced."""

    providers = _Providers()
    models = _Models(
        [
            # Only TTS-capable; STT query should exclude this.
            _model("openai/gpt-4o-mini-tts", (TASK_TEXT_TO_SPEECH,)),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert targets == []


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


def _model(
    model_id: str,
    task_types: tuple[str, ...],
    *,
    name: str | None = None,
    provider_id: str = "openrouter",
) -> SimpleNamespace:
    """Build a model stub that satisfies ``ModelQuery.matches``.

    The capability fields beyond ``task_types`` are populated with neutral
    defaults so the core query can run end-to-end without raising on
    missing attributes. Callers that care about a specific name must pass
    it explicitly.
    """

    if name is None:
        name = (
            "OpenAI GPT-4o Transcribe"
            if TASK_SPEECH_TO_TEXT in task_types
            else "OpenAI GPT-4o Mini TTS"
        )
    return SimpleNamespace(
        provider_id=provider_id,
        model_id=model_id,
        name=name,
        context_window=128000,
        capabilities=SimpleNamespace(
            task_types=task_types,
            input_modalities=("text",),
            output_modalities=("text",),
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=SimpleNamespace(supported=False),
        ),
    )


def _provider(provider_id: str, name: str, connections: list[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        id=provider_id,
        name=name,
        connections=[SimpleNamespace(id=cid, label=clabel) for cid, clabel in connections],
    )


class _Providers:
    def __init__(self, providers: list[SimpleNamespace] | None = None) -> None:
        self._providers = providers or [
            _provider("openrouter", "OpenRouter", [("api-key", "API Key")])
        ]

    def list_ids(self) -> list[str]:
        return [provider.id for provider in self._providers]

    def get(self, provider_id: str) -> SimpleNamespace:
        for provider in self._providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(provider_id)


class _Models:
    def __init__(self, models: list[SimpleNamespace]) -> None:
        self._models = models

    def query(self, model_query: ModelQuery) -> list[tuple[str, SimpleNamespace]]:
        provider_filter = model_query.provider_id
        matches: list[tuple[str, SimpleNamespace]] = []
        for model in self._models:
            if provider_filter and model.provider_id != provider_filter:
                continue
            if not model_query.matches(cast("Model", model)):
                continue
            matches.append((model.provider_id, model))
        return sorted(matches, key=lambda item: (item[0], item[1].model_id))


class _Credentials:
    def __init__(self, granted: set[str] | None = None) -> None:
        self._granted = granted if granted is not None else {"openrouter:api-key"}

    def has_credentials(self, _provider_id: str, connection_id: str) -> bool:
        return connection_id in self._granted


class _Storage:
    def load_model_task_settings(self) -> dict[str, object]:
        return {}

    def update_model_task_settings(self, model_tasks: object) -> object:
        return model_tasks
