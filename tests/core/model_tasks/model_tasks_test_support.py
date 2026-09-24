"""Shared fixtures and fakes for model tasks behavior tests."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast

from core.model_tasks import (
    TASK_SPEECH_TO_TEXT,
)
from core.models import Capabilities, Model, ModelQuery, ModelRegistry, ReasoningCapabilities


def _model(
    model_id: str,
    task_types: tuple[str, ...],
    *,
    name: str | None = None,
    provider_id: str = "openrouter",
    supported_voices: tuple[str, ...] = (),
    supported_parameters: tuple[str, ...] = (),
    connections: tuple[str, ...] = (),
    task_options: dict | None = None,
    input_modalities: tuple[str, ...] = ("text",),
    output_modalities: tuple[str, ...] = ("text",),
) -> SimpleNamespace:
    """Build a model stub that satisfies ``ModelQuery.matches``.

    The capability fields beyond ``task_types`` are populated with neutral
    defaults so the core query can run end-to-end without raising on
    missing attributes. Callers that care about a specific name must pass
    it explicitly.

    ``supported_voices`` and ``supported_parameters`` flow through to the
    model-aware schema builder so the Phase 3 TTS voice / image seed /
    STT response_format behavior is exercised end-to-end.

    ``connections`` flows through to target expansion: an empty tuple
    means the model is valid for every connection, a non-empty tuple
    gates the model to those local connection ids only.
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
        connections=connections,
        # Mirror Model.allows_connection so target expansion sees the real rule.
        allows_connection=lambda connection_id: not connections or connection_id in connections,
        capabilities=SimpleNamespace(
            task_types=task_types,
            input_modalities=input_modalities,
            output_modalities=output_modalities,
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=SimpleNamespace(supported=False),
            supported_voices=supported_voices,
            supported_parameters=supported_parameters,
            task_options=task_options or {},
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
    def __init__(self, models: list[SimpleNamespace] | None = None) -> None:
        self._models: list[SimpleNamespace] = list(models or [])

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

    def get(self, provider_id: str, model_id: str) -> SimpleNamespace:
        """Look up a model by ``(provider_id, model_id)`` — mirrors
        :meth:`core.models.ModelRegistry.get` and raises ``KeyError`` when
        no model matches. The model-aware ``options()`` path catches the
        ``KeyError`` to fall back to provider-level conservative defaults.
        """

        for model in self._models:
            if model.provider_id == provider_id and model.model_id == model_id:
                return model
        raise KeyError(f"Model not found: {provider_id}/{model_id}")


class _Credentials:
    def __init__(self, granted: set[str] | None = None) -> None:
        self._granted = granted if granted is not None else {"openrouter:api-key"}

    def has_credentials(self, _provider_id: str, connection_id: str) -> bool:
        return connection_id in self._granted

    def is_usable(self, provider_id: str, connection_id: str) -> bool:
        return self.has_credentials(provider_id, connection_id)


class _Storage:
    def __init__(self, settings: Mapping[str, object] | None = None) -> None:
        self._settings = dict(settings or {})

    def load_model_task_settings(self) -> dict[str, object]:
        return dict(self._settings)

    def update_model_task_settings(self, model_tasks: object) -> object:
        assert isinstance(model_tasks, Mapping)
        for task_type, binding in model_tasks.items():
            if isinstance(binding, Mapping) and binding.get("target"):
                self._settings[task_type] = dict(binding)
            else:
                self._settings.pop(task_type, None)
        return dict(self._settings)


LIVE_VOICE_TASK_OPTIONS = {
    "live_voice": {
        "parameters": {
            "voice": {"type": "enum", "values": ["cove", "juniper", "maple"], "default": "juniper"},
            "backend_model": {"type": "model", "default": "terra"},
        }
    }
}


def _registry_model(
    model_id: str,
    name: str,
    *,
    task_types: tuple[str, ...],
    tools: bool = False,
    connections: tuple[str, ...] = (),
    task_options: Mapping[str, object] | None = None,
    reasoning_levels: tuple[str, ...] = (),
) -> Model:
    reasoning = (
        ReasoningCapabilities(supported=True, control="levels", levels=reasoning_levels)
        if reasoning_levels
        else ReasoningCapabilities(supported=False)
    )
    return Model(
        model_id=model_id,
        name=name,
        capabilities=Capabilities(
            vision=False,
            tools=tools,
            json_mode=False,
            reasoning=reasoning,
            task_types=task_types,
            task_options=task_options or {},
        ),
        context_window=128000,
        max_output_tokens=None,
        connections=connections,
    )


def _live_voice_registry() -> ModelRegistry:
    """Two live voice Models plus backend candidates spread over two Connections.

    ``live-sub`` only runs on ``subscription``; ``live-key`` only on
    ``api-key``. ``terra`` runs on both, ``astra`` only on ``subscription``
    and ``platform`` only on ``api-key``. ``quiet`` is chat-only without Tools,
    ``painter`` is tool-capable but not a chat Model, and ``foreign`` belongs to
    another Provider. ``terra`` and ``platform`` publish reasoning ladders;
    ``astra`` publishes none.
    """

    chat = ("chat", "text_output")
    entries = {
        ("openai", "live-sub"): _registry_model(
            "live-sub",
            "Live Sub",
            task_types=("live_voice",),
            connections=("subscription",),
            task_options=LIVE_VOICE_TASK_OPTIONS,
        ),
        ("openai", "live-key"): _registry_model(
            "live-key",
            "Live Key",
            task_types=("live_voice",),
            connections=("api-key",),
            task_options=LIVE_VOICE_TASK_OPTIONS,
        ),
        ("openai", "terra"): _registry_model(
            "terra",
            "Terra",
            task_types=chat,
            tools=True,
            connections=("api-key", "subscription"),
            reasoning_levels=("high", "low", "medium"),
        ),
        ("openai", "astra"): _registry_model(
            "astra", "Astra", task_types=chat, tools=True, connections=("subscription",)
        ),
        ("openai", "platform"): _registry_model(
            "platform",
            "Platform",
            task_types=chat,
            tools=True,
            connections=("api-key",),
            reasoning_levels=("low", "max"),
        ),
        ("openai", "quiet"): _registry_model("quiet", "Quiet", task_types=chat),
        ("openai", "painter"): _registry_model(
            "painter", "Painter", task_types=("image_generation",), tools=True
        ),
        ("openrouter", "foreign"): _registry_model(
            "foreign", "Foreign", task_types=chat, tools=True
        ),
    }
    return ModelRegistry(entries)
