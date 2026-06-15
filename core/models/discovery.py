"""Dynamic model discovery pipeline.

The discovery pipeline fetches provider model metadata, normalizes it into the
same JSON shape consumed by :class:`ModelRegistry`, applies optional overrides,
and writes the resulting provider model file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from core.models.models import (
    REASONING_CONTROLS,
    Model,
    ModelRegistry,
    ReasoningCapabilities,
)
from core.models.models_dev import (
    ModelsDevCatalog,
    auto_canonical_pointer,
    provider_reasoning_block,
)
from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.errors import CatalogEntrySkipped, NetworkError
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import ConnectionConfig, ProviderConfig
from core.providers.reasoning import THINKING_EFFORT_ORDER
from core.utils.errors import ProviderError, VBotError
from core.utils.logging import get_logger
from core.utils.retry import retry_async

OVERRIDE_FILE_SUFFIX = ".overrides.json"
_LOGGER = get_logger("models.discovery")


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
    credential_value: str,
    resources_dir: Path,
    raw_filter: RawModelFilter | None = None,
    model_filter: ModelFilter | None = None,
    credential_connection: ConnectionConfig | None = None,
    models_dev_catalog: ModelsDevCatalog | None = None,
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
    """

    base_url, models_endpoint = _resolve_discovery_target(provider_config, credential_connection)
    if not models_endpoint:
        raise ValueError(
            f"Provider '{provider_config.id}' connection "
            f"'{credential_connection.id if credential_connection else None}' "
            "does not define a models_endpoint"
        )

    raw_filter = raw_filter or PassthroughRawFilter()
    model_filter = model_filter or PassthroughModelFilter()

    fetched_at = datetime.now(UTC).isoformat()
    try:
        adapter_class = _adapter_class_for_discovery(provider_config.adapter)
        url = _append_query_params(
            _join_url(base_url, models_endpoint),
            _get_discovery_params(adapter_class),
        )

        raw_payload, raw_models = await _fetch_raw_models(
            url,
            provider_config,
            credential_value,
            adapter_class,
            credential_connection,
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
                    _, supplementary_models = await _fetch_raw_models(
                        supplementary_url,
                        provider_config,
                        credential_value,
                        adapter_class,
                        credential_connection,
                    )
                except (httpx.HTTPError, ProviderError, NetworkError, ValueError) as exc:
                    _LOGGER.warning(
                        "Supplementary model fetch failed for provider '%s' with params %s: %s",
                        provider_config.id,
                        params,
                        exc,
                    )
                    continue
                for model in supplementary_models:
                    model_id = model.get("id")
                    if isinstance(model_id, str) and model_id not in seen_ids:
                        # ``raw_models`` is the same list object held inside
                        # ``raw_payload`` (see ``_fetch_raw_models``), so this
                        # single append also extends the persisted raw payload.
                        raw_models.append(model)
                        seen_ids.add(model_id)

        models_dir = resources_dir / "models"
        models_dir.mkdir(parents=True, exist_ok=True)

        raw_output_path = models_dir / f"{provider_config.id}.raw.json"
        raw_output_data = {
            "provider_id": provider_config.id,
            "fetched_at": fetched_at,
            "raw_response": raw_payload,
        }
        raw_output_path.write_text(
            json.dumps(raw_output_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        catalog = models_dev_catalog

        normalized_models: dict[str, Model] = {}
        for raw_model in raw_models:
            if not raw_filter.accepts(raw_model):
                continue
            try:
                model = adapter_class.normalize_catalog_entry(raw_model, provider_config.defaults)
            except CatalogEntrySkipped as exc:
                _LOGGER.debug(
                    "Skipping model during discovery for provider '%s': %s",
                    provider_config.id,
                    exc,
                )
                continue
            if model_filter.accepts(model):
                normalized_models[model.model_id] = model

        # Refresh writes the PURE provider projection — NO override baking (that
        # cross-file merge moved to LOAD in Phase 2). Each model is serialized to
        # data and enriched with a models.dev canonical pointer / deviating ladder.
        projected_models = _project_provider_models(
            normalized_models,
            provider_config,
            catalog,
        )

        output_path = models_dir / f"{provider_config.id}.json"
        existing_models = _read_existing_provider_models(output_path)
        connection_id = credential_connection.id if credential_connection is not None else None
        tagged_fresh = _tag_fresh_models(projected_models, connection_id)
        final_models = _merge_models_by_connection(existing_models, tagged_fresh, connection_id)
        output_data = {
            "provider_id": provider_config.id,
            "source": "discovery",
            "fetched_at": fetched_at,
            "models": final_models,
        }
        output_path.write_text(
            json.dumps(output_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (
        httpx.HTTPError,
        ProviderError,
        NetworkError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as exc:
        _LOGGER.warning(
            "Model catalog refresh failed for provider %s: %s",
            provider_config.id,
            exc,
        )
        raise ModelDiscoveryError(
            f"Model discovery failed for provider '{provider_config.id}': {exc}"
        ) from exc

    ModelRegistry.invalidate(resources_dir)

    return {
        "provider_id": provider_config.id,
        "model_count": len(final_models),
        "fetched_at": fetched_at,
    }


def _project_provider_models(
    normalized_models: Mapping[str, Model],
    provider_config: ProviderConfig,
    catalog: ModelsDevCatalog | None,
) -> dict[str, dict[str, Any]]:
    """Serialize provider models to data and enrich each from models.dev.

    Each model's wire facts are serialized (``_model_to_data``). When the
    models.dev catalog is available, the provider's section (looked up by
    ``models_dev_id``) supplies an **auto canonical pointer** for an exact
    wire-id match and a **deviating reasoning ladder** when the provider's own
    ``reasoning_options`` differ from the lab spec. No cross-file merge — this is
    the pure provider projection.
    """

    projected: dict[str, dict[str, Any]] = {}
    models_dev_id = provider_config.effective_models_dev_id()
    for wire_id, model in normalized_models.items():
        data = _model_to_data(model)
        if catalog is not None:
            _enrich_provider_model(data, models_dev_id, wire_id, catalog)
        projected[wire_id] = data
    return projected


def _enrich_provider_model(
    data: dict[str, Any],
    models_dev_id: str,
    wire_id: str,
    catalog: ModelsDevCatalog,
) -> None:
    """Stamp the auto canonical pointer + deviating ladder onto one model dict.

    Mutates ``data`` in place:

    * adds a top-level ``canonical`` pointer for an exact lab-section wire-id
      match;
    * when models.dev reports a ladder that *deviates* from the lab spec, sets
      ``capabilities.reasoning`` to that deviating block (provider layer wins at
      load);
    * when the model joins the canonical layer and does NOT deviate, REMOVES the
      provider's bare ``reasoning`` sub-field so the canonical lifted ladder is
      inherited at load (the assembly merges ``capabilities`` one level deep, so
      a present-but-bare provider ``reasoning`` would otherwise shadow the
      canonical one — handoff: non-deviating provider layer is empty).
    """

    pointer = auto_canonical_pointer(catalog, models_dev_id=models_dev_id, wire_id=wire_id)
    if pointer is not None:
        data["canonical"] = pointer

    deviating = provider_reasoning_block(
        catalog,
        models_dev_id=models_dev_id,
        wire_id=wire_id,
    )
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, dict):
        return
    if deviating is not None:
        capabilities["reasoning"] = deviating
        return
    # No deviation: let the canonical ladder flow through at load by dropping the
    # provider's own reasoning block — but only when a canonical join exists to
    # inherit from (an explicit pointer, or the wire-id is itself a canonical id).
    if _has_canonical_join(catalog, pointer, wire_id):
        capabilities.pop("reasoning", None)


def _has_canonical_join(catalog: ModelsDevCatalog, pointer: str | None, wire_id: str) -> bool:
    """Return whether the model resolves to a canonical record at load.

    Mirrors the at-load deterministic join: an explicit auto pointer, or the
    wire-id being itself an exact canonical-id key. Used to decide whether it is
    safe to drop the provider's bare reasoning block for inheritance.
    """

    if pointer is not None:
        return True
    return wire_id in catalog.models


def apply_overrides(
    models: Mapping[str, Model | Mapping[str, Any]], overrides_path: Path
) -> dict[str, dict[str, Any]]:
    """Apply optional field-level model overrides to normalized model data.

    Retained for callers/tests that still want the explicit merge helper.
    Refresh itself no longer bakes overrides into ``<provider>.json`` — that
    cross-file merge moved to LOAD (Phase 2). The override file is now read at
    load time, not at refresh time.
    """

    merged = {model_id: _model_to_data(model) for model_id, model in models.items()}
    if not overrides_path.exists():
        return merged

    override_data = json.loads(overrides_path.read_text(encoding="utf-8"))
    override_models = override_data.get("models", {})
    if not isinstance(override_models, dict):
        raise ValueError(f"Override file '{overrides_path}' must contain a models object")

    for model_id, model_override in override_models.items():
        if not isinstance(model_override, dict):
            raise ValueError(f"Override for model '{model_id}' must be an object")

        if model_id in merged:
            merged[model_id] = {**merged[model_id], **model_override}
        else:
            merged[model_id] = dict(model_override)

        _validate_override_model_data(model_id, merged[model_id], overrides_path)

    return merged


async def _fetch_raw_models(
    url: str,
    provider_config: ProviderConfig,
    credential_value: str,
    adapter_class: Any,
    credential_connection: ConnectionConfig | None = None,
) -> tuple[Any, list[Mapping[str, Any]]]:
    headers = _build_headers(
        provider_config,
        credential_value,
        adapter_class,
        credential_connection,
    )

    async def _request_models() -> tuple[Any, list[Mapping[str, Any]]]:
        # Catalog refresh now shares the provider chat path's transient-failure
        # handling: transport/timeout errors and retryable statuses (429/502/503/
        # 504) are re-issued with backoff + Retry-After; a malformed body or a
        # fatal status (401/403/404/500) raises a non-retryable error that aborts
        # immediately.
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.get(url, headers=headers)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            if response.status_code >= 400:
                error_body = response.text
                detail = (
                    f"{response.status_code} {error_body}".strip()
                    if error_body
                    else str(response.status_code)
                )
                classify_http_status(
                    response.status_code,
                    detail=detail,
                    response_headers=response.headers,
                )
            payload = response.json()

        raw_models = _raw_models_from_payload(payload)
        if not isinstance(raw_models, list):
            raise ValueError("Models response must contain a list, a data list, or a models list")

        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                raise ValueError("Every raw model entry must be an object")
        return payload, raw_models

    return await retry_async(_request_models)


def _raw_models_from_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if "data" in payload:
        return payload.get("data")
    return payload.get("models")


def _build_headers(
    provider_config: ProviderConfig,
    credential_value: str,
    adapter_class: Any,
    credential_connection: ConnectionConfig | None = None,
) -> dict[str, str]:
    headers = dict(provider_config.extra_headers or {})
    connection = credential_connection or next(iter(provider_config.connections), None)
    if connection is not None:
        auth = connection.auth
        headers[auth.header] = f"{auth.prefix}{credential_value}"
    discovery_headers = getattr(adapter_class, "discovery_headers", None)
    if callable(discovery_headers):
        return dict(discovery_headers(provider_config, credential_value, headers))
    return headers


def _join_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def _resolve_discovery_target(
    provider_config: ProviderConfig,
    credential_connection: ConnectionConfig | None,
) -> tuple[str, str | None]:
    """Return the ``(base_url, models_endpoint)`` pair for the selected connection.

    Connection-level ``base_url`` and ``models_endpoint`` override the
    provider-level values so per-connection wire variants (e.g. Codex's
    ``chatgpt.com/backend-api``) feed discovery. Falls back to provider-level
    settings when the connection does not declare its own.
    """

    base_url = provider_config.base_url
    models_endpoint = provider_config.models_endpoint
    if credential_connection is not None:
        if credential_connection.base_url:
            base_url = credential_connection.base_url
        if credential_connection.models_endpoint:
            models_endpoint = credential_connection.models_endpoint
    return base_url, models_endpoint


def _read_existing_provider_models(output_path: Path) -> dict[str, dict[str, Any]]:
    """Return the existing ``<provider>.json`` ``models`` mapping, or empty.

    A missing file, an unreadable payload, or an entry that is not a mapping
    is treated as "no existing models" so a single bad write never blocks a
    future merge.
    """

    if not output_path.exists():
        return {}
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.warning(
            "Could not read existing provider catalog %s for merge: %s",
            output_path,
            exc,
        )
        return {}
    models = payload.get("models") if isinstance(payload, Mapping) else None
    if not isinstance(models, dict):
        return {}
    return {
        model_id: dict(model_data)
        for model_id, model_data in models.items()
        if isinstance(model_data, Mapping)
    }


def _merge_models_by_connection(
    existing_models: Mapping[str, dict[str, Any]],
    fresh_models: Mapping[str, dict[str, Any]],
    connection_id: str | None,
) -> dict[str, dict[str, Any]]:
    """Combine fresh discovery output with existing catalog models.

    Models are partitioned by the ``connections`` allowlist. Models tagged
    with ``connection_id`` are replaced by the fresh fetch; models tagged
    with any other connection are kept untouched. Passing
    ``connection_id=None`` (no selected connection) falls back to a full
    overwrite for backward compatibility with non-connection-aware callers.
    """

    merged: dict[str, dict[str, Any]] = {}
    if connection_id is None:
        merged.update(fresh_models)
        return merged

    for model_id, model_data in existing_models.items():
        model_connections = model_data.get("connections")
        if not isinstance(model_connections, list) or connection_id not in model_connections:
            merged[model_id] = model_data
    merged.update(fresh_models)
    return merged


def _tag_fresh_models(
    fresh_models: Mapping[str, dict[str, Any]],
    connection_id: str | None,
) -> dict[str, dict[str, Any]]:
    """Stamp every fresh model dict with ``connections: [connection_id]``.

    Each refreshed entry is scoped to the connection that produced it so a
    later refresh of a *different* connection can replace only its own
    entries. When no connection is selected (``connection_id=None``) the
    fresh models are returned untouched, which keeps the merge step a
    full overwrite in that case.
    """

    if connection_id is None:
        return dict(fresh_models)
    tagged: dict[str, dict[str, Any]] = {}
    for model_id, model_data in fresh_models.items():
        data = dict(model_data)
        data["connections"] = [connection_id]
        tagged[model_id] = data
    return tagged


def _model_to_data(model: Model | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(model, Model):
        data = {
            "name": model.name,
            "capabilities": {
                "vision": model.capabilities.vision,
                "tools": model.capabilities.tools,
                "json_mode": model.capabilities.json_mode,
                "reasoning": _reasoning_to_data(model.capabilities.reasoning),
                "input_modalities": list(model.capabilities.input_modalities),
                "output_modalities": list(model.capabilities.output_modalities),
                "supported_parameters": list(model.capabilities.supported_parameters),
                "supported_voices": list(model.capabilities.supported_voices),
                "task_types": list(model.capabilities.task_types),
            },
            "context_window": model.context_window,
            "max_output_tokens": model.max_output_tokens,
        }
        # ``family`` is written only when known, mirroring how connections /
        # metadata are omitted when empty so generated catalogs stay clean.
        if model.family:
            data["family"] = model.family
        if model.connections:
            data["connections"] = list(model.connections)
        metadata = _plain_data(model.metadata)
        if metadata:
            data["metadata"] = metadata
        return data
    data = dict(model)
    connections = data.get("connections")
    if connections is not None and not isinstance(connections, list):
        raise ValueError("connections must be a list when set")
    if isinstance(connections, list) and not connections:
        data.pop("connections", None)
    return data


def _reasoning_to_data(reasoning: ReasoningCapabilities) -> dict[str, Any]:
    """Serialize the typed reasoning block, omitting unset control fields.

    ``control``/``levels``/``budget_max`` are emitted only when present so the
    on-disk form stays minimal — a model with no projected ladder serializes
    back to the bare ``{"supported": bool}`` shape, matching "absent when not
    supported (or not yet known)".
    """

    data: dict[str, Any] = {"supported": reasoning.supported}
    if reasoning.control is not None:
        data["control"] = reasoning.control
    if reasoning.levels:
        data["levels"] = list(reasoning.levels)
    if reasoning.budget_max is not None:
        data["budget_max"] = reasoning.budget_max
    return data


def _plain_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_data(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_data(item) for item in value]
    return value


def _overrides_path(resources_dir: Path, provider_id: str) -> Path:
    return resources_dir / "models" / f"{provider_id}{OVERRIDE_FILE_SUFFIX}"


def _validate_override_model_data(
    model_id: str,
    model_data: Mapping[str, Any],
    overrides_path: Path,
) -> None:
    try:
        _validate_model_data(model_id, model_data)
    except ValueError as exc:
        raise ValueError(
            f"Invalid override for model '{model_id}' in '{overrides_path}': {exc}"
        ) from exc


def _validate_model_data(model_id: str, model_data: Mapping[str, Any]) -> None:
    caps = _read_mapping(model_data, "capabilities")
    reasoning = _read_mapping(caps, "reasoning")
    _read_string(model_data, "name")
    _read_bool(caps, "vision")
    _read_bool(caps, "tools")
    _read_bool(caps, "json_mode")
    _validate_reasoning(reasoning)
    _read_string_list(caps, "input_modalities")
    _read_string_list(caps, "output_modalities")
    _read_string_list(caps, "supported_parameters")
    _read_string_list(caps, "task_types")
    _read_int(model_data, "context_window")
    _read_optional_int(model_data, "max_output_tokens")
    _read_optional_string(model_data, "family")
    if not model_id:
        raise ValueError("Override-only model id must not be empty")


def _validate_reasoning(reasoning: Mapping[str, Any]) -> None:
    """Validate the typed reasoning block, accepting the minimal form.

    ``supported`` is required; ``control``/``levels``/``budget_max`` are all
    optional (Phase 1 carries no ladder data, so ``{"supported": true}`` with
    no control is valid). When present, ``control`` must be one of the allowed
    kinds, every ``levels`` value must be a known thinking effort, and
    ``budget_max`` must be an int.
    """

    _read_bool(reasoning, "supported")
    control = reasoning.get("control")
    if control is not None and (not isinstance(control, str) or control not in REASONING_CONTROLS):
        allowed = ", ".join(REASONING_CONTROLS)
        raise ValueError(f"Expected 'control' to be one of [{allowed}] or null")
    levels = reasoning.get("levels")
    if levels is not None:
        if not isinstance(levels, list) or not all(isinstance(item, str) for item in levels):
            raise ValueError("Expected 'levels' to be a list of strings")
        unknown = [level for level in levels if level not in THINKING_EFFORT_ORDER]
        if unknown:
            allowed = ", ".join(THINKING_EFFORT_ORDER)
            raise ValueError(
                f"Expected 'levels' values to be thinking efforts [{allowed}]; "
                f"got unknown {unknown}"
            )
    if "budget_max" in reasoning:
        _read_optional_int(reasoning, "budget_max")


def _adapter_class_for_discovery(adapter: str):
    adapter_class = _DISCOVERY_ADAPTER_MAP.get(adapter)
    if adapter_class is None:
        raise ValueError(f"No model normalizer registered for adapter '{adapter}'")
    return adapter_class


def _get_supplementary_params(adapter_class: Any) -> list[dict[str, str]]:
    """Return supplementary query-parameter dicts from the adapter, if any.

    Adapters that require additional API calls to discover task-specific
    models (e.g. OpenRouter's STT/TTS models) can define a class method
    ``supplementary_discovery_params()`` returning a list of query-param
    dicts.  Each dict is appended to the models endpoint URL for a
    supplementary fetch.
    """
    method = getattr(adapter_class, "supplementary_discovery_params", None)
    if callable(method):
        result: list[dict[str, str]] = method()
        return result
    return []


def _get_discovery_params(adapter_class: Any) -> dict[str, str]:
    """Return query parameters for the primary model-discovery request."""

    method = getattr(adapter_class, "discovery_params", None)
    if callable(method):
        result: dict[str, str] = method()
        return result
    return {}


def _append_query_params(url: str, params: dict[str, str]) -> str:
    """Append query parameters to a URL, preserving any existing query string."""
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    encoded = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{url}{separator}{encoded}"


def _read_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Expected '{key}' to be an object")
    return value


def _read_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Expected '{key}' to be a string")
    return value


def _read_optional_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Expected '{key}' to be a string or absent")
    return value


def _read_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Expected '{key}' to be an integer")
    return value


def _read_optional_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Expected '{key}' to be an integer or null")
    return value


def _read_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Expected '{key}' to be a boolean")
    return value


def _read_string_list(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected '{key}' to be a list of strings")
    return value


_DISCOVERY_ADAPTER_MAP = {
    "openai_compatible": OpenAICompatibleAdapter,
    "openai": OpenAIAdapter,
    "opencode_go": OpenCodeGoAdapter,
    "openrouter": OpenRouterAdapter,
    "minimax": MiniMaxAdapter,
    "mistral": MistralAdapter,
    "github_copilot": GitHubCopilotAdapter,
}
