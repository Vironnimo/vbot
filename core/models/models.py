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
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from core.models._model_record import (
    INPUT_TASK_TYPES as INPUT_TASK_TYPES,
)
from core.models._model_record import (
    MODEL_TASK_ORDER as MODEL_TASK_ORDER,
)
from core.models._model_record import (
    REASONING_CONTROL_BUDGET as REASONING_CONTROL_BUDGET,
)
from core.models._model_record import (
    REASONING_CONTROL_LEVELS as REASONING_CONTROL_LEVELS,
)
from core.models._model_record import (
    REASONING_CONTROL_ON_OFF as REASONING_CONTROL_ON_OFF,
)
from core.models._model_record import (
    REASONING_CONTROLS as REASONING_CONTROLS,
)
from core.models._model_record import (
    Capabilities as Capabilities,
)
from core.models._model_record import (
    Model as Model,
)
from core.models._model_record import (
    ReasoningCapabilities as ReasoningCapabilities,
)
from core.models._model_record import (
    _describe_model_data_error,
    _model_from_record,
    _settle_task_types,
)
from core.models._model_record import (
    derive_model_task_types as derive_model_task_types,
)
from core.models._model_record import (
    text_embedding_capabilities as text_embedding_capabilities,
)
from core.models.assembly import (
    CANONICAL_OVERRIDES_FILE_NAME,
    ModelDataIssueReport,
    assemble_provider_model,
    load_canonical_layer,
    log_model_data_issue,
)
from core.models.database import OVERRIDES_FILE_SUFFIX, select_model_database_files
from core.models.pricing import TokenPricing
from core.utils.file_status import is_dir_strict
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.models.query import ModelQuery

_MODEL_DATA_ERRORS = (AttributeError, KeyError, OSError, TypeError, UnicodeError, ValueError)
# ``reload_async`` reads and assembles the catalog files here, off the Event Loop.
_RELOAD_WORKERS = BoundedWorkerPool(name="model-registry", max_workers=1)


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
            override_files = (
                sorted(
                    path
                    for path in bundled_models_dir.iterdir()
                    if path.name.endswith(OVERRIDES_FILE_SUFFIX)
                )
                if is_dir_strict(bundled_models_dir)
                else []
            )
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
                    _settle_task_types(record, override_model)
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
