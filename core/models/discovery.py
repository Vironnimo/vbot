"""Dynamic model discovery pipeline.

The discovery pipeline fetches provider model metadata, normalizes it into the
same JSON shape consumed by :class:`ModelRegistry`, and writes the resulting
provider model file.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from core.models._discovery_http import (
    DiscoveryRequest as DiscoveryRequest,
)
from core.models._discovery_http import (
    _append_query_params,
    _fetch_json_payload,
    _fetch_raw_models,
    _get_supplementary_params,
    _join_url,
    _post_json_payload,
    _require_discovery_target,
)
from core.models._discovery_http import (
    build_discovery_request as build_discovery_request,
)
from core.models._discovery_projection import (
    _admitted_models,
    _merge_models_by_connection,
    _model_to_data,
    _models_for_connection_enrichment,
    _project_provider_models,
    _read_existing_provider_models,
    _tag_fresh_models,
)
from core.models.assembly import load_canonical_layer
from core.models.models import (
    Model,
    ModelRegistry,
)
from core.models.models_dev import (
    ModelsDevCatalog,
)
from core.providers._wire_profile_files import WireProfileFile
from core.providers.adapter import ProviderAdapter
from core.providers.errors import CatalogEntrySkipped, NetworkError
from core.providers.providers import ConnectionConfig, ProviderConfig
from core.providers.token_getter import OAuthRequestRecovery, StaticTokenGetter, TokenGetter
from core.utils.errors import ProviderError, VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("models.discovery")
_HeaderSource = dict[str, str] | Callable[[], Awaitable[dict[str, str]]]
_CATALOG_IDENTITY_FIELDS = frozenset(
    {"id", "object", "created", "owned_by", "root", "parent", "permission", "name"}
)


class ModelDiscoveryError(VBotError):
    """Expected model discovery failure safe to return through RPC."""


class RawModelFilter(Protocol):
    """Pre-normalization filter for provider-native model dictionaries."""

    def accepts(self, raw_model: Mapping[str, Any]) -> bool:
        """Return whether ``raw_model`` should continue through discovery."""


class PassthroughRawFilter:
    """Raw model filter that accepts every model."""

    def accepts(self, raw_model: Mapping[str, Any]) -> bool:
        return True


class ModelFilter(Protocol):
    """Post-normalization filter for vBot Model instances."""

    def accepts(self, model: Model) -> bool:
        """Return whether ``model`` should be written to the registry file."""


class PassthroughModelFilter:
    """Normalized model filter that accepts every model."""

    def accepts(self, model: Model) -> bool:
        return True


async def refresh_models(
    provider_config: ProviderConfig,
    credential_value: str | TokenGetter,
    resources_dir: Path,
    raw_filter: RawModelFilter | None = None,
    model_filter: ModelFilter | None = None,
    credential_connection: ConnectionConfig | None = None,
    models_dev_catalog: ModelsDevCatalog | None = None,
    wire_files: Mapping[str, WireProfileFile] | None = None,
) -> dict[str, Any]:
    """Fetch, normalize, project, write, and invalidate one provider catalog.

    Refresh stays DUMB: it writes the PURE provider projection — wire facts plus
    a models.dev-derived **auto canonical pointer** and a **deviating reasoning
    ladder** where the provider's own ``reasoning_options`` differ from the lab
    spec. It does NOT bake ``<provider>.overrides.json`` into ``<provider>.json``
    (that cross-file merge is LOAD's job, Phase 2) and does NOT join across
    providers.

    Discovery targets the selected connection: the connection's
    ``models_endpoint``/``base_url`` override the provider-level values, and
    written models are tagged with the connection's local id so future
    refreshes of other connections can replace only their own entries.

    Args:
        models_dev_catalog: An optional pre-fetched models.dev catalog used to
            enrich each model with a canonical pointer / deviating ladder. When
            ``None``, enrichment is skipped and the pure wire-fact projection is
            written (the join is enrichment, not a dependency). Fetching the
            catalog ONCE per refresh is the caller's job (the RPC refresh path
            and the regen script) so it is not re-fetched per provider.
        wire_files: The wire profile data whose admission decides which Models
            are written; the bundled files when ``None``. A Runtime passes its
            own, so a Custom Provider's Settings ``wire`` block applies.
    """

    _require_discovery_target(provider_config, credential_connection)

    raw_filter = raw_filter or PassthroughRawFilter()
    model_filter = model_filter or PassthroughModelFilter()

    fetched_at = datetime.now(UTC).isoformat()
    try:
        request = await build_discovery_request(provider_config, credential_connection)
        adapter_class = request.adapter_class
        base_url = request.base_url
        url = request.url
        token_getter = (
            StaticTokenGetter(credential_value)
            if isinstance(credential_value, str)
            else credential_value
        )

        async def build_headers() -> dict[str, str]:
            return request.headers(await token_getter())

        connection = credential_connection or next(iter(provider_config.connections), None)

        def request_auth_recovery() -> OAuthRequestRecovery | None:
            return (
                OAuthRequestRecovery(token_getter, connection.auth)
                if connection is not None and connection.type == "oauth"
                else None
            )

        raw_models = await _fetch_raw_models(
            url, build_headers, auth_recovery=request_auth_recovery()
        )

        # Some providers (e.g. OpenRouter) require supplementary API calls
        # with different query parameters to discover dedicated task models
        # (STT, TTS, image/audio/video generation) excluded from the default
        # model listing.
        supplementary_params = _get_supplementary_params(adapter_class)
        if supplementary_params:
            seen_ids = {m.get("id") for m in raw_models if isinstance(m.get("id"), str)}
            for params in supplementary_params:
                supplementary_url = _append_query_params(url, params)
                try:
                    supplementary_models = await _fetch_raw_models(
                        supplementary_url, build_headers, auth_recovery=request_auth_recovery()
                    )
                except (httpx.HTTPError, ProviderError, NetworkError, ValueError) as exc:
                    _LOGGER.warning(
                        "Supplementary model fetch failed for provider '%s' with params %s: %s",
                        provider_config.id,
                        params,
                        exc,
                    )
                    continue
                for supplementary_model in supplementary_models:
                    model_id = supplementary_model.get("id")
                    if isinstance(model_id, str) and model_id not in seen_ids:
                        raw_models.append(supplementary_model)
                        seen_ids.add(model_id)

        models_dir = resources_dir / "models"
        models_dir.mkdir(parents=True, exist_ok=True)

        catalog = models_dev_catalog

        normalized_models: dict[str, Model] = {}
        sparse_models: dict[str, Model] = {}
        for raw_model in raw_models:
            if not raw_filter.accepts(raw_model):
                continue
            connection_catalog_filter = getattr(
                adapter_class,
                "accepts_discovered_model",
                None,
            )
            if callable(connection_catalog_filter) and not connection_catalog_filter(
                raw_model,
                credential_connection,
            ):
                continue
            try:
                model = adapter_class.normalize_catalog_entry(raw_model, provider_config.defaults)
                if isinstance(adapter_class, type) and issubclass(adapter_class, ProviderAdapter):
                    model = adapter_class.finalize_discovered_model(
                        model,
                        credential_connection,
                    )
            except CatalogEntrySkipped as exc:
                _LOGGER.debug(
                    "Skipping model during discovery for provider '%s': %s",
                    provider_config.id,
                    exc,
                )
                continue
            if model.model_id in provider_config.catalog_exclusions:
                _LOGGER.debug(
                    "Excluded unusable catalog model (provider=%s model=%s)",
                    provider_config.id,
                    model.model_id,
                )
                continue
            if model_filter.accepts(model):
                normalized_models[model.model_id] = model
                if (
                    set(raw_model) <= _CATALOG_IDENTITY_FIELDS
                    and "chat" in model.capabilities.task_types
                ):
                    sparse_models[model.model_id] = model

        # A provider may need per-model detail calls to fill capabilities the
        # listing endpoint omits (e.g. Ollama's POST /api/show for tool/vision/
        # thinking support and the context window). The adapter owns the
        # endpoints and the projection; discovery supplies POST plumbing with
        # the same retry semantics as the catalog GET. A failure degrades to
        # the un-enriched catalog, never a failed refresh.
        enrich_discovered_models = getattr(adapter_class, "enrich_discovered_models", None)
        if callable(enrich_discovered_models):

            async def _post_enrichment_json(endpoint: str, payload: dict[str, Any]) -> Any:
                return await _post_json_payload(
                    _join_url(base_url, endpoint),
                    build_headers,
                    payload,
                    auth_recovery=request_auth_recovery(),
                )

            try:
                enriched_models = await enrich_discovered_models(
                    normalized_models, _post_enrichment_json
                )
            except (httpx.HTTPError, ProviderError, NetworkError, ValueError) as exc:
                _LOGGER.warning(
                    "Model enrichment fetch failed for provider '%s': %s",
                    provider_config.id,
                    exc,
                )
            else:
                normalized_models.update(enriched_models)

        # A provider may expose dedicated task-capability catalogs (e.g. the
        # OpenRouter image API) whose typed per-model option schemas the
        # default models endpoint omits. The adapter owns those endpoints and
        # their projection into ``capabilities.task_options``; discovery only
        # supplies the fetch plumbing. A failure degrades to a catalog without
        # task options, never a failed refresh.
        discover_task_models = getattr(adapter_class, "discover_task_models", None)
        if callable(discover_task_models):

            async def _fetch_task_json(endpoint: str) -> Any:
                return await _fetch_json_payload(
                    _join_url(base_url, endpoint),
                    build_headers,
                    auth_recovery=request_auth_recovery(),
                )

            try:
                task_models = await discover_task_models(normalized_models, _fetch_task_json)
            except (httpx.HTTPError, ProviderError, NetworkError, ValueError) as exc:
                _LOGGER.warning(
                    "Task-model catalog fetch failed for provider '%s': %s",
                    provider_config.id,
                    exc,
                )
            else:
                normalized_models.update(task_models)

        # Refresh writes the PURE provider projection — NO override baking (that
        # cross-file merge moved to LOAD in Phase 2). Each model is serialized to
        # data and enriched with a models.dev canonical pointer / deviating ladder.
        output_path = models_dir / f"{provider_config.id}.json"
        existing_models = _read_existing_provider_models(output_path)
        projected_models = _project_provider_models(
            normalized_models,
            provider_config,
            catalog,
            _models_for_connection_enrichment(
                existing_models,
                credential_connection.id if credential_connection is not None else None,
            ),
            previous_canonical=(
                load_canonical_layer(
                    models_dir / "models.json", models_dir / "models.overrides.json"
                )
                if catalog is None
                else {}
            ),
        )
        projected_models = _admitted_models(
            projected_models,
            normalized_models,
            provider_config,
            credential_connection,
            wire_files,
        )
        connection_id = credential_connection.id if credential_connection is not None else None
        tagged_fresh = _tag_fresh_models(projected_models, connection_id)
        final_models = _merge_models_by_connection(
            existing_models,
            tagged_fresh,
            connection_id,
            capability_defaults={
                model_id: _model_to_data(model)["capabilities"]
                for model_id, model in sparse_models.items()
                if normalized_models.get(model_id) is model
            },
        )
        output_data = {
            "provider_id": provider_config.id,
            "source": "discovery",
            "fetched_at": fetched_at,
            "models": final_models,
        }
        output_path.write_text(
            json.dumps(output_data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except (
        httpx.HTTPError,
        ProviderError,
        NetworkError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as exc:
        # Callers log what they catch: the manual refresh warns per Connection,
        # the automatic local sweep warns only on a reachability transition.
        raise ModelDiscoveryError(
            f"Model discovery failed for provider '{provider_config.id}': {exc}"
        ) from exc

    ModelRegistry.invalidate(resources_dir)

    return {
        "provider_id": provider_config.id,
        "model_count": len(final_models),
        "fetched_at": fetched_at,
    }
