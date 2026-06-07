"""Model data classes and model registry.

A Model represents a specific AI model at a specific provider.  Models are
always provider-specific — the same underlying model appears as different
entries in different provider files, with different IDs, capabilities, and
context windows.

The ModelRegistry loads model data from JSON files under a ``models/``
subdirectory and indexes entries by ``(provider_id, model_id)`` for fast
lookup.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from core.models.query import ModelQuery

MODEL_TASK_ORDER = (
    "chat",
    "text_output",
    "image_input",
    "image_understanding",
    "file_input",
    "file_understanding",
    "audio_input",
    "speech_to_text",
    "video_input",
    "video_understanding",
    "image_generation",
    "audio_generation",
    "text_to_speech",
    "video_generation",
)


@dataclass(frozen=True)
class ReasoningCapabilities:
    """Whether a model supports reasoning through a specific provider."""

    supported: bool


@dataclass(frozen=True)
class Capabilities:
    """Provider-specific capability flags for a model."""

    vision: bool
    tools: bool
    json_mode: bool
    reasoning: ReasoningCapabilities
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    supported_voices: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        input_modalities = _normalize_string_tuple(self.input_modalities)
        if not input_modalities:
            input_modalities = ("text", "image") if self.vision else ("text",)

        output_modalities = _normalize_string_tuple(self.output_modalities)
        if not output_modalities:
            output_modalities = ("text",)

        supported_parameters = _normalize_string_tuple(self.supported_parameters, sort=True)
        supported_voices = _normalize_string_tuple(self.supported_voices, sort=True)
        task_types = _normalize_string_tuple(self.task_types)
        if not task_types:
            task_types = derive_model_task_types(input_modalities, output_modalities)

        object.__setattr__(self, "input_modalities", input_modalities)
        object.__setattr__(self, "output_modalities", output_modalities)
        object.__setattr__(self, "supported_parameters", supported_parameters)
        object.__setattr__(self, "supported_voices", supported_voices)
        object.__setattr__(self, "task_types", task_types)


def derive_model_task_types(
    input_modalities: Iterable[str],
    output_modalities: Iterable[str],
) -> tuple[str, ...]:
    """Derive coarse task filters from provider-reported model modalities.

    Modality conventions (provider-specific, normalized on ingestion):

    * ``"transcription"`` in output — dedicated speech-to-text models (e.g.
      ``openai/whisper-1`` via OpenRouter ``?output_modalities=transcription``).
    * ``"speech"`` in output — dedicated text-to-speech models (e.g.
      ``openai/gpt-4o-mini-tts`` via OpenRouter ``?output_modalities=speech``).
    * ``"audio"`` in output — generic audio generation (music, sound effects,
      or conversational audio).  Models with only ``"audio"`` are NOT tagged
      ``text_to_speech`` unless they also have ``"speech"`` in their output
      modalities.
    """

    inputs = set(_normalize_string_tuple(tuple(input_modalities)))
    outputs = set(_normalize_string_tuple(tuple(output_modalities)))

    # "transcription" output means the model produces text from audio → STT
    has_transcription = "transcription" in outputs
    # "speech" output means the model produces speech audio → TTS
    has_speech = "speech" in outputs
    # "audio" output is generic audio generation (music, effects, conv audio)
    has_audio = "audio" in outputs

    has_text_output = "text" in outputs or has_transcription

    tasks: set[str] = set()

    if has_text_output:
        tasks.add("text_output")
    if "text" in inputs and has_text_output:
        tasks.add("chat")
    if "image" in inputs:
        tasks.add("image_input")
        if has_text_output:
            tasks.add("image_understanding")
    if "file" in inputs:
        tasks.add("file_input")
        if has_text_output:
            tasks.add("file_understanding")
    if "audio" in inputs:
        tasks.add("audio_input")
        if has_text_output:
            tasks.add("speech_to_text")
    if has_transcription:
        # Dedicated STT models that output transcription text
        tasks.add("speech_to_text")
    if "video" in inputs:
        tasks.add("video_input")
        if has_text_output:
            tasks.add("video_understanding")
    if "image" in outputs:
        tasks.add("image_generation")
    if has_audio:
        tasks.add("audio_generation")
    if has_speech:
        # Dedicated TTS models that output speech audio
        tasks.add("audio_generation")
        tasks.add("text_to_speech")
    if "video" in outputs:
        tasks.add("video_generation")

    return tuple(task for task in MODEL_TASK_ORDER if task in tasks)


@dataclass(frozen=True)
class Model:
    """A specific AI model at a specific provider.

    The ``model_id`` is the exact string sent in API requests — no remapping.
    For example, ``"anthropic/claude-sonnet-4"`` at OpenRouter is sent as-is.
    """

    model_id: str
    name: str
    capabilities: Capabilities
    context_window: int
    max_output_tokens: int | None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_metadata_value(self.metadata))


class ModelRegistry:
    """Registry of model data, indexed by (provider_id, model_id).

    Loads model data from JSON files in a ``models/`` subdirectory.  Caches
    after first load — subsequent calls with the same path return the cached
    instance.
    """

    _cache: ClassVar[dict[Path, ModelRegistry]] = {}

    def __init__(self, models: dict[tuple[str, str], Model]) -> None:
        self._models = models

    @classmethod
    def load(cls, resources_dir: Path) -> ModelRegistry:
        """Load model data from all ``<resources_dir>/models/*.json`` files.

        Args:
            resources_dir: Path to the resources directory containing a
                ``models/`` subdirectory with JSON model data files.

        Returns:
            A populated ModelRegistry instance.
        """
        resolved = resources_dir.resolve()
        if resolved in cls._cache:
            return cls._cache[resolved]

        models_dir = resolved / "models"
        models: dict[tuple[str, str], Model] = {}

        for json_file in sorted(models_dir.glob("*.json")):
            if json_file.name.endswith(".overrides.json") or json_file.name.endswith(".raw.json"):
                continue

            data = json.loads(json_file.read_text(encoding="utf-8"))
            provider_id = data["provider_id"]
            for model_id, model_data in data["models"].items():
                caps = model_data["capabilities"]
                reasoning = ReasoningCapabilities(
                    supported=caps["reasoning"]["supported"],
                )
                capabilities = Capabilities(
                    vision=caps["vision"],
                    tools=caps["tools"],
                    json_mode=caps["json_mode"],
                    reasoning=reasoning,
                    input_modalities=tuple(caps.get("input_modalities", ())),
                    output_modalities=tuple(caps.get("output_modalities", ())),
                    supported_parameters=tuple(caps.get("supported_parameters", ())),
                    supported_voices=tuple(caps.get("supported_voices", ())),
                    task_types=tuple(caps.get("task_types", ())),
                )
                model = Model(
                    model_id=model_id,
                    name=model_data["name"],
                    capabilities=capabilities,
                    context_window=model_data["context_window"],
                    max_output_tokens=model_data["max_output_tokens"],
                    metadata=model_data.get("metadata", {}),
                )
                models[(provider_id, model_id)] = model

        registry = cls(models)
        cls._cache[resolved] = registry
        return registry

    @classmethod
    def invalidate(cls, resources_dir: Path) -> None:
        """Remove the cached registry for ``resources_dir`` if present."""

        cls._cache.pop(resources_dir.resolve(), None)

    def get(self, provider_id: str, model_id: str) -> Model:
        """Look up a model by provider ID and model ID.

        Args:
            provider_id: The provider identifier (e.g. ``"openai"``).
            model_id: The exact model ID sent in API requests.

        Returns:
            The matching Model entry.

        Raises:
            KeyError: If no model matches the given provider and model ID.
        """
        key = (provider_id, model_id)
        if key not in self._models:
            raise KeyError(f"Model not found: {provider_id}/{model_id}")
        return self._models[key]

    def list_for_provider(self, provider_id: str) -> list[Model]:
        """Return all models for a given provider, sorted by model_id.

        Args:
            provider_id: The provider identifier (e.g. ``"openai"``).

        Returns:
            A sorted list of Model entries for the provider.  Returns an
            empty list if no models are found for the provider.
        """
        return sorted(
            [model for (pid, _), model in self._models.items() if pid == provider_id],
            key=lambda model: model.model_id,
        )

    def query(self, model_query: ModelQuery) -> list[tuple[str, Model]]:
        """Return ``(provider_id, model)`` tuples matching ``model_query``.

        Results are sorted by ``(provider_id, model_id)`` and contain no
        credential awareness — the caller is responsible for any
        per-connection gating. An empty list is returned for an unknown
        ``provider_id`` and for any query that matches no models.
        """

        provider_filter = model_query.provider_id
        matches: list[tuple[str, Model]] = []
        for (provider_id, _), model in self._models.items():
            if provider_filter and provider_id != provider_filter:
                continue
            if model_query.matches(model):
                matches.append((provider_id, model))
        return sorted(matches, key=lambda item: (item[0], item[1].model_id))


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_metadata_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value


def _normalize_string_tuple(values: Iterable[str], *, sort: bool = False) -> tuple[str, ...]:
    normalized_items: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized_value = value.strip().lower()
        if not normalized_value or normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized_items.append(normalized_value)

    if sort:
        normalized_items.sort()
    return tuple(normalized_items)
