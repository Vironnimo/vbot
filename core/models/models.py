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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar


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
    max_output_tokens: int
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


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_metadata_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value
