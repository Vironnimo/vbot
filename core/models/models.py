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
import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from core.models.assembly import (
    CANONICAL_OVERRIDES_FILE_NAME,
    ModelDataIssueReport,
    assemble_provider_model,
    load_canonical_layer,
    log_model_data_issue,
)
from core.models.database import OVERRIDES_FILE_SUFFIX, select_model_database_files
from core.models.pricing import TokenPricing
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.models.query import ModelQuery

_MODEL_DATA_ERRORS = (AttributeError, KeyError, OSError, TypeError, UnicodeError, ValueError)
# ``reload_async`` reads and assembles the catalog files here, off the Event Loop.
_RELOAD_WORKERS = BoundedWorkerPool(name="model-registry", max_workers=1)

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
    "decision",
    "video_generation",
    # Explicit-only: no modality combination derives a live voice Model.
    "live_voice",
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
    * ``mandatory`` — the provider always reasons and rejects a request that
      turns reasoning off; only meaningful when ``supported`` is ``True``.

    The fields are ordered so existing ``ReasoningCapabilities(supported=...)``
    construction sites keep working unchanged.
    """

    supported: bool
    control: str | None = None
    levels: tuple[str, ...] = ()
    budget_max: int | None = None
    mandatory: bool = False


@dataclass(frozen=True)
class Capabilities:
    """Provider-specific capability flags for a model.

    ``task_options`` holds typed option schemas for specialized task execution,
    keyed by task type (e.g. ``image_generation``). Each entry carries the
    provider-published facts the Settings option builder renders generically:

    * ``parameters`` — mapping of wire parameter name to a typed spec:
      ``{"type": "enum", "values": [...]}``, ``{"type": "range", "min": n,
      "max": n}``, ``{"type": "boolean"}`` (the parameter is supported,
      value free-form), or ``{"type": "model"}`` (a tool-capable chat Model
      id of the same Provider that the selected Connection allows). A spec
      may carry an optional ``default`` value that the option builder uses
      when it is among the offered choices.
    * ``passthrough`` — mapping of upstream provider slug to the list of
      provider-specific option keys accepted via passthrough (OpenRouter
      ``provider.options``).

    Projected at refresh from provider task-capability feeds (e.g. the
    OpenRouter image API) or hand-authored in ``<provider>.overrides.json``
    for providers whose APIs publish nothing (OpenAI native). It is frozen on
    construction and merged wholesale as one ``capabilities`` sub-field at load.
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


def text_embedding_capabilities(supported_parameters: tuple[str, ...] = ()) -> Capabilities:
    """Return the capabilities of a text-in, vectors-out embedding Model.

    The ``embeddings`` output derives only the ``text_embedding`` task, so such
    a Model is never offered as a chat Model. Provider catalogs that identify
    embedding Models use this one shape.
    """

    return Capabilities(
        vision=False,
        tools=False,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
        input_modalities=("text",),
        output_modalities=("embeddings",),
        supported_parameters=supported_parameters,
    )


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
    if "decisions" in outputs:
        tasks.add("decision")
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

    ``connection_context_windows`` carries the exceptional case where two
    Connections expose different Context limits for the same Provider wire id.
    Callers that know the selected Connection use :meth:`context_window_for`;
    other readers retain the conservative Model-wide ``context_window``.

    ``metadata`` is the sanctioned home for provider-scoped per-model wire facts.
    Conventions (keep them tight — this is not a dumping ground):

    * **Provider-scoped:** keys are provider ids (e.g.
      ``metadata.github_copilot.supported_endpoints``,
      ``metadata.opencode_go.reasoning_response_field``), so one provider's wire
      quirk never pollutes the schema for every model.
    * **Small and immutable after load:** nested mappings/lists are frozen on
      construction (see ``__post_init__``); loaded ``Model`` instances never
      mutate.
    * **Provider-reported facts only — never hand-authored wire decisions, raw
      payloads, provider policy text, credentials, or secrets.** Wire decisions
      (protocol, carriers, replay scope, media, listed Tools) live in the
      Provider's wire profile file (``resources/wire/<provider>.json``).
    """

    model_id: str
    name: str
    capabilities: Capabilities
    context_window: int | None
    max_output_tokens: int | None
    family: str = ""
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    connections: tuple[str, ...] = ()
    connection_context_windows: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    recommended_temperature: float | None = None
    recommended_top_p: float | None = None
    pricing: TokenPricing | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_metadata_value(self.metadata))
        object.__setattr__(
            self,
            "connection_context_windows",
            _freeze_connection_context_windows(self.connection_context_windows),
        )

    def allows_connection(self, connection_id: str) -> bool:
        """Whether this model may run on ``connection_id`` of its provider.

        An empty ``connections`` allowlist permits every connection; a non-empty
        one restricts the model to the listed connection ids. This is the single
        source of the per-model connection rule — target expansion and the
        save-time guards read it so the catalog cannot offer a model on a
        connection it forbids.
        """
        return not self.connections or connection_id in self.connections

    def context_window_for(self, connection_id: str) -> int | None:
        """Return the selected Connection's window or the Model-wide fallback."""

        return self.connection_context_windows.get(connection_id, self.context_window)


@dataclass(frozen=True)
class _AssembledCatalog:
    """One reload's assembled contents, ready to swap into a registry."""

    models: dict[tuple[str, str], Model]
    cache_key: tuple[Path, Path | None] | None


class ModelRegistry:
    """Registry of model data, indexed by (provider_id, model_id).

    The single public read surface for model data. ``load()`` assembles each
    effective model at load time from the selected canonical/provider catalogs
    plus the current bundled override layers (see :mod:`core.models.assembly`);
    ``get()`` / ``list_for_provider()`` / ``query()`` read the assembled result.
    Caches after first load —
    subsequent calls with the same system/runtime root pair return the cached
    instance until ``invalidate()`` clears it (e.g. after a refresh publishes a
    new database root).
    """

    _cache: ClassVar[dict[tuple[Path, Path | None], ModelRegistry]] = {}

    def __init__(self, models: dict[tuple[str, str], Model]) -> None:
        self._models = models
        self._reload_lock = threading.Lock()
        self._reload_generation = 0

    def pricing_for(self, model_reference: str) -> TokenPricing | None:
        """Read exact catalog pricing without Connection/Account identifiers."""
        bare = model_reference.split("::", 1)[0]
        provider, _, model_id = bare.partition("/")
        model = self._models.get((provider, model_id))
        return model.pricing if model is not None else None

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
        absent; assembly then runs on provider + override data alone. No network
        and no key are involved.

        Args:
            resources_dir: Path to the system resources directory containing
                the bundled complete ``models/`` database.
            runtime_models_dir: Optional data-dir Model DB. Each generated
                catalog it fetched more recently than the system DB, or that the
                system DB does not ship, is loaded from it (see
                :mod:`core.models.database`); the bundled overrides remain
                authoritative.

        Returns:
            A populated ModelRegistry instance.
        """
        resolved = resources_dir.resolve()
        resolved_runtime = runtime_models_dir.resolve() if runtime_models_dir is not None else None
        if custom_providers is not None:
            return cls(cls._assemble_models(resolved, resolved_runtime, custom_providers))

        cache_key = (resolved, resolved_runtime)
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        registry = cls(cls._assemble_models(resolved, resolved_runtime, {}))
        cls._cache[cache_key] = registry
        return registry

    @classmethod
    def validate(cls, resources_dir: Path) -> list[str]:
        """Assemble the Model DB under ``resources_dir`` and return what Load would ignore.

        Validates a staged working copy before it is published: the root is
        assembled exactly like :meth:`load` without a runtime root or Custom
        Providers, but nothing is cached or logged. Each returned message names
        one invalid file, entry or value that Load omits; the live reload after
        publication logs them, so a refresh reports each issue once.
        """

        issues: list[str] = []
        cls._assemble_models(resources_dir.resolve(), None, {}, issues.append)
        return issues

    def reload(
        self,
        resources_dir: Path,
        *,
        runtime_models_dir: Path | None = None,
        custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> bool:
        """Re-assemble the registry in place from disk, keeping object identity.

        ``load`` rebinds nothing here: refresh writes new layer files, then
        reload swaps *this* registry's contents so every holder that captured the
        instance at construction — task-model targets (speech/image/embeddings),
        the ``/status`` display, and the recall backend — sees the new catalog
        without re-wiring. The class cache entry is repointed at this same
        (now-updated) instance, so a later ``load`` returns it too.

        Returns whether the swap changed the assembled catalog; a reload that a
        newer one superseded changed nothing.
        """

        generation = self._begin_reload()
        assembled = self._assemble_reload(resources_dir, runtime_models_dir, custom_providers)
        return self._adopt(assembled, generation)

    async def reload_async(
        self,
        resources_dir: Path,
        *,
        runtime_models_dir: Path | None = None,
        custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> bool:
        """Event-Loop-safe :meth:`reload`, with the same result.

        The catalog files are read and assembled on a worker thread; the swap
        then happens on the calling Event Loop, so a reader there never sees a
        half-replaced registry. A newer synchronous or asynchronous reload
        supersedes this assembly even if this worker finishes last.
        """

        generation = self._begin_reload()
        assembled = await _RELOAD_WORKERS.run(
            self._assemble_reload,
            resources_dir,
            runtime_models_dir,
            custom_providers,
        )
        return self._adopt(assembled, generation)

    def _begin_reload(self) -> int:
        with self._reload_lock:
            self._reload_generation += 1
            return self._reload_generation

    @classmethod
    def _assemble_reload(
        cls,
        resources_dir: Path,
        runtime_models_dir: Path | None,
        custom_providers: Mapping[str, Mapping[str, Any]] | None,
    ) -> _AssembledCatalog:
        resolved = resources_dir.resolve()
        resolved_runtime = runtime_models_dir.resolve() if runtime_models_dir is not None else None
        models = cls._assemble_models(resolved, resolved_runtime, custom_providers or {})
        return _AssembledCatalog(
            models=models,
            cache_key=(resolved, resolved_runtime) if custom_providers is None else None,
        )

    def _adopt(self, assembled: _AssembledCatalog, generation: int) -> bool:
        with self._reload_lock:
            if generation != self._reload_generation:
                return False
            changed = assembled.models != self._models
            self._models = assembled.models
            if assembled.cache_key is not None:
                type(self)._cache[assembled.cache_key] = self
            return changed

    @classmethod
    def _assemble_models(
        cls,
        resources_dir: Path,
        runtime_models_dir: Path | None,
        custom_providers: Mapping[str, Mapping[str, Any]],
        report: ModelDataIssueReport = log_model_data_issue,
    ) -> dict[tuple[str, str], Model]:
        """Assemble every effective model from the on-disk layers (no cache).

        Each generated catalog comes from the system root under ``resources_dir``
        or from ``runtime_models_dir`` (:func:`select_model_database_files`); the
        system root owns the authoritative hand-maintained overrides. Shared by
        ``load``, ``reload`` and ``validate`` so every path assembles
        identically; ``report`` receives each ignored file, entry or value.
        """

        files = select_model_database_files(resources_dir, runtime_models_dir, report=report)
        bundled_models_dir = files.overrides_dir
        canonical_layer = load_canonical_layer(
            files.canonical,
            bundled_models_dir / CANONICAL_OVERRIDES_FILE_NAME,
            report=report,
        )
        models: dict[tuple[str, str], Model] = {}
        provider_layers: dict[str, dict[str, Any]] = {}
        provider_sources: dict[str, Path] = {}
        override_layers: dict[str, dict[str, Any]] = {}
        override_sources: dict[str, Path] = {}

        for json_file in files.providers:
            try:
                provider_id, provider_models = cls._read_provider_file(json_file)
            except _MODEL_DATA_ERRORS as exc:
                report(f"Ignoring invalid Model DB provider file '{json_file}': {exc}")
                continue
            provider_layers[provider_id] = provider_models
            provider_sources[provider_id] = json_file

        # A provider with no refreshable/static catalog can still be defined by
        # its hand layer alone. Include those provider ids without manufacturing
        # an empty generated ``<provider>.json`` file just to make the override
        # discoverable.
        try:
            override_files = sorted(bundled_models_dir.glob(f"*{OVERRIDES_FILE_SUFFIX}"))
        except OSError as exc:
            report(f"Could not scan Model DB override files in '{bundled_models_dir}': {exc}")
            override_files = []

        for overrides_file in override_files:
            if overrides_file.name == CANONICAL_OVERRIDES_FILE_NAME:
                continue
            try:
                provider_id, override_models = cls._read_override_file(overrides_file)
            except _MODEL_DATA_ERRORS as exc:
                report(f"Ignoring invalid Model DB override file '{overrides_file}': {exc}")
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
                    report(
                        f"Ignoring invalid Model DB provider entry '{provider_id}/{wire_id}' "
                        f"in '{provider_sources[provider_id]}': "
                        "record must be an object"
                    )
                    provider_entry_present = False
                    provider_model = {}
                if override_model is not None and not isinstance(override_model, Mapping):
                    report(
                        f"Ignoring invalid Model DB override entry '{provider_id}/{wire_id}' "
                        f"in '{override_sources[provider_id]}': "
                        "record must be an object"
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
                    models[(provider_id, wire_id)] = _model_from_record(wire_id, record, report)
                except _MODEL_DATA_ERRORS as exc:
                    layer_sources: tuple[Path | None, ...]
                    if provider_entry_present:
                        layer_sources = (
                            provider_sources.get(provider_id),
                            override_sources.get(provider_id),
                        )
                        origin = ""
                    else:
                        # Assembly had only the hand layer (plus any canonical
                        # join), so the Override entry is what lacks the data.
                        layer_sources = (override_sources.get(provider_id),)
                        origin = (
                            "; it is an Override-only entry the Provider catalog no longer lists"
                            if provider_id in provider_sources
                            else "; it is an Override-only entry and the Provider has no "
                            "generated catalog"
                        )
                    sources = "', '".join(
                        str(source) for source in layer_sources if source is not None
                    )
                    report(
                        f"Ignoring invalid Model DB model '{provider_id}/{wire_id}' "
                        f"from '{sources}': {_describe_model_data_error(exc)}{origin}"
                    )

        for provider_id, provider in custom_providers.items():
            for model_id, custom_model in provider.get("models", {}).items():
                try:
                    models[(provider_id, model_id)] = _model_from_record(
                        model_id,
                        cls._custom_model_record(custom_model),
                        report,
                    )
                except _MODEL_DATA_ERRORS as exc:
                    report(
                        f"Ignoring invalid custom Model '{provider_id}/{model_id}' from "
                        f"Settings: {_describe_model_data_error(exc)}"
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


def _model_from_record(
    model_id: str,
    record: Mapping[str, Any],
    report: ModelDataIssueReport = log_model_data_issue,
) -> Model:
    """Construct a typed ``Model`` from an assembled effective-model record.

    The record is the field-level merge of the canonical, provider, and override
    layers (see :mod:`core.models.assembly`) with the internal ``canonical``
    pointer already stripped. It must carry the loader's required fields; a layer
    set that fails to supply one (e.g. a model missing ``name`` or
    ``capabilities``) surfaces as a ``KeyError`` naming the missing field, which
    is the correct "the data is incomplete" signal. ``report`` receives optional
    values that are ignored without dropping the Model.

    ``context_window`` and ``max_output_tokens`` are the deliberate exceptions:
    an absent value is the honest "this fact is unknown" signal, not a load
    error. It stays ``None`` in the data (the read-side default chain — model
    value → provider-config default → global floor — fills the gap at use time,
    see :func:`core.providers.providers.resolve_context_window`).
    """

    caps = _required_field(record, "capabilities")
    reasoning_data = _required_field(record, "capabilities", "reasoning")
    supported = _required_field(record, "capabilities", "reasoning", "supported")
    reasoning = ReasoningCapabilities(
        supported=supported,
        control=reasoning_data.get("control"),
        levels=tuple(reasoning_data.get("levels", ())),
        budget_max=reasoning_data.get("budget_max"),
        mandatory=_coerce_reasoning_mandatory(
            reasoning_data.get("mandatory"), supported=supported, report=report
        ),
    )
    capabilities = Capabilities(
        vision=_required_field(record, "capabilities", "vision"),
        tools=_required_field(record, "capabilities", "tools"),
        json_mode=_required_field(record, "capabilities", "json_mode"),
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
        name=_required_field(record, "name"),
        capabilities=capabilities,
        context_window=record.get("context_window"),
        max_output_tokens=record.get("max_output_tokens"),
        family=record.get("family", ""),
        metadata=record.get("metadata", {}),
        connections=tuple(record.get("connections", ())),
        connection_context_windows=record.get("connection_context_windows", {}),
        recommended_temperature=_coerce_recommended_temperature(
            record.get("recommended_temperature"),
            report,
        ),
        recommended_top_p=_coerce_recommended_top_p(record.get("recommended_top_p"), report),
        pricing=TokenPricing.from_dict(record.get("pricing")),
    )


def _required_field(record: Mapping[str, Any], *keys: str) -> Any:
    """Return the nested required value at ``keys``; a missing one raises ``KeyError(path)``."""

    value: Any = record
    for depth, key in enumerate(keys):
        if not isinstance(value, Mapping):
            raise ValueError(f"field '{'.'.join(keys[:depth])}' must be an object")
        if key not in value:
            raise KeyError(".".join(keys[: depth + 1]))
        value = value[key]
    return value


def _describe_model_data_error(exc: Exception) -> str:
    """Render a caught Model data error; a bare ``KeyError`` names its missing field."""

    if type(exc) is KeyError and exc.args:
        return f"missing required field '{exc.args[0]}'"
    return str(exc)


def _freeze_connection_context_windows(value: Any) -> Mapping[str, int]:
    """Validate and freeze optional per-Connection Context limits."""

    if not isinstance(value, Mapping):
        raise ValueError("connection_context_windows must be an object")
    windows: dict[str, int] = {}
    for connection_id, context_window in value.items():
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_context_windows keys must be non-empty strings")
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            raise ValueError("connection_context_windows values must be positive integers")
        windows[connection_id] = context_window
    return MappingProxyType(windows)


def _coerce_reasoning_mandatory(
    value: Any, *, supported: Any, report: ModelDataIssueReport
) -> bool:
    """Validate the optional ``capabilities.reasoning.mandatory`` fact.

    Absent means optional reasoning. A non-boolean value, or ``true`` on a Model
    whose reasoning is not supported, is reported and treated as ``False`` so
    one bad fact never hides the Model.
    """

    if value is None or value is False:
        return False
    if value is not True:
        report(f"capabilities.reasoning.mandatory is not a boolean ({value!r}); ignoring")
        return False
    if supported is not True:
        report("capabilities.reasoning.mandatory is true but reasoning is not supported; ignoring")
        return False
    return True


def _coerce_recommended_temperature(value: Any, report: ModelDataIssueReport) -> float | None:
    """Coerce a ``recommended_temperature`` record value into a valid float or None.

    Like ``context_window`` and ``max_output_tokens``, this is an optional
    model fact that stays ``None`` when absent. A non-numeric or out-of-range
    value is logged and treated as unknown rather than failing assembly, so one
    bad override never hides valid sibling models.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | float) and not isinstance(value, bool):
        temperature = float(value)
    else:
        report(f"recommended_temperature is not a number ({value!r}); ignoring")
        return None
    if not math.isfinite(temperature):
        report(f"recommended_temperature is not finite ({value!r}); ignoring")
        return None
    if temperature < 0.0 or temperature > 2.0:
        report(f"recommended_temperature {temperature:g} is outside [0.0, 2.0]; ignoring")
        return None
    return temperature


def _coerce_recommended_top_p(value: Any, report: ModelDataIssueReport) -> float | None:
    """Coerce a ``recommended_top_p`` record value into a valid float or None.

    Like ``recommended_temperature``, this is an optional model fact that stays
    ``None`` when absent. A non-numeric or out-of-range value is logged and
    treated as unknown rather than failing assembly, so one bad override never
    hides valid sibling models.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | float) and not isinstance(value, bool):
        top_p = float(value)
    else:
        report(f"recommended_top_p is not a number ({value!r}); ignoring")
        return None
    if not math.isfinite(top_p):
        report(f"recommended_top_p is not finite ({value!r}); ignoring")
        return None
    if top_p < 0.0 or top_p > 1.0:
        report(f"recommended_top_p {top_p:g} is outside [0.0, 1.0]; ignoring")
        return None
    return top_p


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
