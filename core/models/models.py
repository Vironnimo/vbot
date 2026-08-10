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
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from core.models.assembly import (
    CANONICAL_FILE_NAME,
    CANONICAL_OVERRIDES_FILE_NAME,
    assemble_provider_model,
    load_canonical_layer,
)
from core.models.database import (
    MODEL_DATABASE_MANIFEST_FILE_NAME,
    select_model_database_dir,
)

if TYPE_CHECKING:
    from core.models.query import ModelQuery

_LOGGER = logging.getLogger("vbot.models")
_MODEL_DATA_ERRORS = (AttributeError, KeyError, OSError, TypeError, UnicodeError, ValueError)

# Provider-layer files under ``models/`` are ``<provider>.json``; these siblings
# are never provider files and are excluded from the provider-file glob loop.
# ``*.raw.json`` is an inspection dump; ``*.overrides.json`` is a hand layer
# applied during assembly (not its own provider file); the canonical files are
# loaded by the dedicated canonical loader. The suffixes/classifier are public
# so the offline validator shares one definition of "what is a provider file".
RAW_FILE_SUFFIX = ".raw.json"
OVERRIDES_FILE_SUFFIX = ".overrides.json"
_NON_PROVIDER_FILE_NAMES = frozenset(
    {
        CANONICAL_FILE_NAME,
        CANONICAL_OVERRIDES_FILE_NAME,
        MODEL_DATABASE_MANIFEST_FILE_NAME,
    }
)


def is_provider_file(file_name: str) -> bool:
    """Return whether ``file_name`` is a provider-layer ``<provider>.json``.

    Excludes the inspection ``*.raw.json`` dump, the ``*.overrides.json`` hand
    layer (applied during assembly, not its own provider file), the database
    ``manifest.json``, and the canonical ``models.json`` /
    ``models.overrides.json`` (loaded separately). Shared by the registry loader
    and the offline validator so the classification can't drift.
    """

    if file_name.endswith(RAW_FILE_SUFFIX) or file_name.endswith(OVERRIDES_FILE_SUFFIX):
        return False
    return file_name not in _NON_PROVIDER_FILE_NAMES


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
    "music_generation",
    "text_to_speech",
    "text_embedding",
    "video_generation",
)

# How the provider exposes the reasoning control on the wire. ``levels`` is an
# effort ladder (e.g. low/medium/high), ``on_off`` a binary thinking toggle,
# ``budget`` a token budget. Derived from models.dev ``reasoning_options`` at
# refresh; see ``stuff/HANDOFF-model-db.md`` → "Reasoning — Steuerung".
REASONING_CONTROL_LEVELS = "levels"
REASONING_CONTROL_ON_OFF = "on_off"
REASONING_CONTROL_BUDGET = "budget"
REASONING_CONTROLS = (
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    REASONING_CONTROL_BUDGET,
)


@dataclass(frozen=True)
class ReasoningCapabilities:
    """How a model exposes reasoning through a specific provider.

    ``supported`` is the only required field and stays the load-bearing flag
    that runtime/snapping read (``model_reasoning_supported``). The typed
    control fields describe *how* the provider steers reasoning and are all
    optional:

    * ``control`` — the wire control kind (one of ``REASONING_CONTROLS``), or
      ``None`` when not yet known. It is absent when ``supported`` is ``False``
      and may also be absent when ``supported`` is ``True`` but no ladder data
      has been projected yet (effort ladders arrive from models.dev
      ``reasoning_options`` in a later refresh phase).
    * ``levels`` — the effort ladder for ``control == "levels"`` (a subset of
      ``THINKING_EFFORT_ORDER``), empty otherwise.
    * ``budget_max`` — the maximum thinking-token budget for
      ``control == "budget"``, ``None`` otherwise.

    The fields are ordered so existing ``ReasoningCapabilities(supported=...)``
    construction sites keep working unchanged.
    """

    supported: bool
    control: str | None = None
    levels: tuple[str, ...] = ()
    budget_max: int | None = None


@dataclass(frozen=True)
class Capabilities:
    """Provider-specific capability flags for a model.

    ``task_options`` holds typed option schemas for specialized task execution,
    keyed by task type (e.g. ``image_generation``). Each entry carries the
    provider-published facts the Settings option builder renders generically:

    * ``parameters`` — mapping of wire parameter name to a typed spec:
      ``{"type": "enum", "values": [...]}``, ``{"type": "range", "min": n,
      "max": n}``, or ``{"type": "boolean"}`` (the parameter is supported,
      value free-form).
    * ``passthrough`` — mapping of upstream provider slug to the list of
      provider-specific option keys accepted via passthrough (OpenRouter
      ``provider.options``).

    Projected at refresh from provider task-capability feeds (e.g. the
    OpenRouter image API) or hand-authored in ``<provider>.overrides.json``
    for providers whose APIs publish nothing (OpenAI native). Like
    ``metadata``, it is frozen on construction and merged wholesale as one
    ``capabilities`` sub-field at load.
    """

    vision: bool
    tools: bool
    json_mode: bool
    reasoning: ReasoningCapabilities
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    supported_voices: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    task_options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

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
        object.__setattr__(self, "task_options", _freeze_metadata_value(self.task_options))


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
    * ``"embeddings"`` in output — dedicated text-embedding models (e.g.
      ``openai/text-embedding-3-small`` via OpenRouter
      ``?output_modalities=embeddings``). The model produces vectors, not
      text, so it is NOT tagged ``chat`` or ``text_output``. Mirrors the
      ``speech`` → ``text_to_speech`` alias.
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
    if "embeddings" in outputs:
        # Dedicated embedding models: output is a vector, not text/chat.
        # Mirror of the "speech" → text_to_speech alias.
        tasks.add("text_embedding")
    if "video" in outputs:
        tasks.add("video_generation")

    return tuple(task for task in MODEL_TASK_ORDER if task in tasks)


@dataclass(frozen=True)
class Model:
    """A specific AI model at a specific provider.

    The ``model_id`` is the exact string sent in API requests — no remapping.
    For example, ``"anthropic/claude-sonnet-4"`` at OpenRouter is sent as-is.

    ``family`` is the model lineage as the provider/feed reports it (e.g.
    ``"gpt-5.2"``, ``"claude-sonnet-4"``). It is a first-class fact on the model
    — the handoff places it here ("eigenes Feld am Modell"), replacing per-adapter
    family-from-name guessing. Optional; defaults to ``""`` when unknown.

    ``metadata`` is the sanctioned home for provider-scoped per-model wire facts.
    Conventions (keep them tight — this is not a dumping ground):

    * **Provider-scoped:** keys are provider ids (e.g.
      ``metadata.github_copilot.supported_endpoints``,
      ``metadata.opencode_go.protocol``), so one provider's wire quirk never
      pollutes the schema for every model.
    * **Small and immutable after load:** nested mappings/lists are frozen on
      construction (see ``__post_init__``); loaded ``Model`` instances never
      mutate.
    * **Wire facts only — never raw payloads, provider policy text, credentials,
      or secrets.** Mirrors today's ``metadata.github_copilot`` usage.
    """

    model_id: str
    name: str
    capabilities: Capabilities
    context_window: int | None
    max_output_tokens: int | None
    family: str = ""
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    connections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_metadata_value(self.metadata))

    def allows_connection(self, connection_id: str) -> bool:
        """Whether this model may run on ``connection_id`` of its provider.

        An empty ``connections`` allowlist permits every connection; a non-empty
        one restricts the model to the listed connection ids. This is the single
        source of the per-model connection rule — target expansion and the
        save-time guards read it so the catalog cannot offer a model on a
        connection it forbids.
        """
        return not self.connections or connection_id in self.connections


class ModelRegistry:
    """Registry of model data, indexed by (provider_id, model_id).

    The single public read surface for model data. ``load()`` assembles each
    effective model at load time from the canonical, provider, and override
    layers (see :mod:`core.models.assembly`); ``get()`` / ``list_for_provider()``
    / ``query()`` read the assembled result. Caches after first load —
    subsequent calls with the same system/runtime root pair return the cached
    instance until ``invalidate()`` clears it (e.g. after a refresh publishes a
    new complete database).
    """

    _cache: ClassVar[dict[tuple[Path, Path | None], ModelRegistry]] = {}

    def __init__(self, models: dict[tuple[str, str], Model]) -> None:
        self._models = models

    @classmethod
    def load(
        cls,
        resources_dir: Path,
        *,
        runtime_models_dir: Path | None = None,
        custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ModelRegistry:
        """Assemble the registry from the canonical, provider, and override layers.

        Each effective model is built at load time from up to three layers — the
        provider-agnostic canonical base (joined deterministically), the
        ``<provider>.json`` provider layer, and the ``<provider>.overrides.json``
        hand layer — by :mod:`core.models.assembly`. The canonical files may be
        absent (Phase 3 generates them); assembly then runs on provider + override
        data alone. No network and no key are involved.

        Args:
            resources_dir: Path to the system resources directory containing
                the bundled complete ``models/`` database.
            runtime_models_dir: Optional complete data-dir Model DB. When it
                has a newer compatible refresh manifest than the system DB,
                this whole root is loaded instead; files are never mixed.

        Returns:
            A populated ModelRegistry instance.
        """
        resolved = resources_dir.resolve()
        resolved_runtime = runtime_models_dir.resolve() if runtime_models_dir is not None else None
        if custom_providers is not None:
            models_dir = select_model_database_dir(resolved, resolved_runtime)
            registry = cls(cls._assemble_models(models_dir, custom_providers))
            registry._active_models_dir = models_dir
            return registry

        cache_key = (resolved, resolved_runtime)
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        models_dir = select_model_database_dir(resolved, resolved_runtime)
        registry = cls(cls._assemble_models(models_dir, {}))
        registry._active_models_dir = models_dir
        cls._cache[cache_key] = registry
        return registry

    def reload(
        self,
        resources_dir: Path,
        *,
        runtime_models_dir: Path | None = None,
        custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Re-assemble the registry in place from disk, keeping object identity.

        ``load`` rebinds nothing here: refresh writes new layer files, then
        reload swaps *this* registry's contents so every holder that captured the
        instance at construction — task-model targets (speech/image/embeddings),
        the ``/status`` display, and the recall backend — sees the new catalog
        without re-wiring. The class cache entry is repointed at this same
        (now-updated) instance, so a later ``load`` returns it too.
        """

        resolved = resources_dir.resolve()
        resolved_runtime = runtime_models_dir.resolve() if runtime_models_dir is not None else None
        models_dir = select_model_database_dir(resolved, resolved_runtime)
        self._models = self._assemble_models(models_dir, custom_providers or {})
        self._active_models_dir = models_dir
        if custom_providers is None:
            type(self)._cache[(resolved, resolved_runtime)] = self

    @classmethod
    def _assemble_models(
        cls,
        models_dir: Path,
        custom_providers: Mapping[str, Mapping[str, Any]],
    ) -> dict[tuple[str, str], Model]:
        """Assemble every effective model from the on-disk layers (no cache).

        ``models_dir`` is one already-selected complete database root. Shared
        by ``load`` and ``reload`` so both paths assemble identically without
        combining system and runtime files.
        """

        canonical_layer = load_canonical_layer(models_dir)
        models: dict[tuple[str, str], Model] = {}
        provider_layers: dict[str, dict[str, Any]] = {}
        provider_sources: dict[str, Path] = {}
        override_layers: dict[str, dict[str, Any]] = {}
        override_sources: dict[str, Path] = {}

        try:
            provider_files = sorted(models_dir.glob("*.json"))
        except OSError as exc:
            _LOGGER.warning("Could not scan Model DB provider files in '%s': %s", models_dir, exc)
            provider_files = []

        for json_file in provider_files:
            if not is_provider_file(json_file.name):
                continue

            try:
                provider_id, provider_models = cls._read_provider_file(json_file)
            except _MODEL_DATA_ERRORS as exc:
                _LOGGER.warning(
                    "Ignoring invalid Model DB provider file '%s': %s",
                    json_file,
                    exc,
                )
                continue
            provider_layers[provider_id] = provider_models
            provider_sources[provider_id] = json_file

        # A provider with no refreshable/static catalog can still be defined by
        # its hand layer alone. Include those provider ids without manufacturing
        # an empty generated ``<provider>.json`` file just to make the override
        # discoverable.
        try:
            override_files = sorted(models_dir.glob(f"*{OVERRIDES_FILE_SUFFIX}"))
        except OSError as exc:
            _LOGGER.warning("Could not scan Model DB override files in '%s': %s", models_dir, exc)
            override_files = []

        for overrides_file in override_files:
            if overrides_file.name == CANONICAL_OVERRIDES_FILE_NAME:
                continue
            try:
                provider_id, override_models = cls._read_override_file(overrides_file)
            except _MODEL_DATA_ERRORS as exc:
                _LOGGER.warning(
                    "Ignoring invalid Model DB override file '%s': %s",
                    overrides_file,
                    exc,
                )
                continue
            override_layers[provider_id] = override_models
            override_sources[provider_id] = overrides_file
            provider_layers.setdefault(provider_id, {})

        for provider_id, provider_models in sorted(provider_layers.items()):
            override_models = override_layers.get(provider_id, {})
            wire_ids = sorted(set(provider_models) | set(override_models))
            for wire_id in wire_ids:
                provider_entry_present = wire_id in provider_models
                provider_model = provider_models.get(wire_id, {})
                override_model = override_models.get(wire_id)
                if provider_entry_present and not isinstance(provider_model, Mapping):
                    _LOGGER.warning(
                        "Ignoring invalid Model DB provider entry '%s/%s' in '%s': "
                        "record must be an object",
                        provider_id,
                        wire_id,
                        provider_sources.get(provider_id, models_dir),
                    )
                    provider_entry_present = False
                    provider_model = {}
                if override_model is not None and not isinstance(override_model, Mapping):
                    _LOGGER.warning(
                        "Ignoring invalid Model DB override entry '%s/%s' in '%s': "
                        "record must be an object",
                        provider_id,
                        wire_id,
                        override_sources.get(provider_id, models_dir),
                    )
                    override_model = None
                if not provider_entry_present and override_model is None:
                    continue

                try:
                    record = assemble_provider_model(
                        wire_id,
                        provider_model,
                        override_model,
                        canonical_layer,
                    )
                    models[(provider_id, wire_id)] = _model_from_record(wire_id, record)
                except _MODEL_DATA_ERRORS as exc:
                    sources = [
                        str(source)
                        for source in (
                            provider_sources.get(provider_id),
                            override_sources.get(provider_id),
                        )
                        if source is not None
                    ]
                    _LOGGER.warning(
                        "Ignoring invalid Model DB model '%s/%s' from '%s': %s",
                        provider_id,
                        wire_id,
                        "', '".join(sources),
                        exc,
                    )

        for provider_id, provider in custom_providers.items():
            for model_id, custom_model in provider.get("models", {}).items():
                try:
                    models[(provider_id, model_id)] = _model_from_record(
                        model_id,
                        cls._custom_model_record(custom_model),
                    )
                except _MODEL_DATA_ERRORS as exc:
                    _LOGGER.warning(
                        "Ignoring invalid custom Model '%s/%s' from Settings: %s",
                        provider_id,
                        model_id,
                        exc,
                    )

        return models

    @staticmethod
    def _custom_model_record(model: Mapping[str, Any]) -> dict[str, Any]:
        """Convert a normalized Settings Model into the registry record shape."""

        capabilities = model["capabilities"]
        return {
            "name": model["name"],
            "context_window": model.get("context_window"),
            "max_output_tokens": model.get("max_output_tokens"),
            "connections": ["default"],
            "capabilities": {
                "vision": capabilities["vision"],
                "tools": capabilities["tools"],
                "json_mode": capabilities["json_mode"],
                "reasoning": {"supported": capabilities["reasoning"]},
                "input_modalities": list(capabilities["input_modalities"]),
                "output_modalities": list(capabilities["output_modalities"]),
                "supported_parameters": list(capabilities["supported_parameters"]),
                "supported_voices": list(capabilities["supported_voices"]),
                "task_types": list(capabilities["task_types"]),
                "task_options": dict(capabilities["task_options"]),
            },
        }

    @staticmethod
    def _read_provider_file(path: Path) -> tuple[str, dict[str, Any]]:
        """Read and validate one generated Provider layer."""

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("the root must be an object")
        provider_id = data.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("'provider_id' must be a non-empty string")
        provider_models = data.get("models")
        if not isinstance(provider_models, dict):
            raise ValueError("'models' must be an object")
        return provider_id, provider_models

    @staticmethod
    def _read_override_file(path: Path) -> tuple[str, dict[str, Any]]:
        """Read and validate one optional Provider override layer."""

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("the root must be an object")
        provider_id = data.get("provider_id") or path.name.removesuffix(OVERRIDES_FILE_SUFFIX)
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("'provider_id' must be a non-empty string")
        override_models = data.get("models", {})
        if not isinstance(override_models, dict):
            raise ValueError("'models' must be an object")
        return provider_id, override_models

    @classmethod
    def invalidate(
        cls,
        resources_dir: Path,
        *,
        runtime_models_dir: Path | None = None,
    ) -> None:
        """Remove cached registries that use the supplied system/runtime root."""

        resolved = resources_dir.resolve()
        if runtime_models_dir is not None:
            cls._cache.pop((resolved, runtime_models_dir.resolve()), None)
            return
        for cache_key in list(cls._cache):
            system_root, cached_runtime = cache_key
            if system_root == resolved or cached_runtime in {resolved, resolved / "models"}:
                cls._cache.pop(cache_key, None)

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


def _model_from_record(model_id: str, record: Mapping[str, Any]) -> Model:
    """Construct a typed ``Model`` from an assembled effective-model record.

    The record is the field-level merge of the canonical, provider, and override
    layers (see :mod:`core.models.assembly`) with the internal ``canonical``
    pointer already stripped. It must carry the loader's required fields; a layer
    set that fails to supply one (e.g. a model missing ``name`` or
    ``capabilities``) surfaces as a ``KeyError`` here, which is the correct "the
    data is incomplete" signal.

    ``context_window`` and ``max_output_tokens`` are the deliberate exceptions:
    an absent value is the honest "this fact is unknown" signal, not a load
    error. It stays ``None`` in the data (the read-side default chain — model
    value → provider-config default → global floor — fills the gap at use time,
    see :func:`core.providers.providers.resolve_context_window`).
    """

    caps = record["capabilities"]
    reasoning_data = caps["reasoning"]
    reasoning = ReasoningCapabilities(
        supported=reasoning_data["supported"],
        control=reasoning_data.get("control"),
        levels=tuple(reasoning_data.get("levels", ())),
        budget_max=reasoning_data.get("budget_max"),
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
        task_options=caps.get("task_options", {}),
    )
    return Model(
        model_id=model_id,
        name=record["name"],
        capabilities=capabilities,
        context_window=record.get("context_window"),
        max_output_tokens=record.get("max_output_tokens"),
        family=record.get("family", ""),
        metadata=record.get("metadata", {}),
        connections=tuple(record.get("connections", ())),
    )


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
