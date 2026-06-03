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

from core.models.models import Model, ModelRegistry
from core.providers.errors import CatalogEntrySkipped
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openai_subscription import OpenAISubscriptionAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import ConnectionConfig, ProviderConfig
from core.utils.errors import VBotError
from core.utils.logging import get_logger

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
) -> dict[str, Any]:
    """Fetch, normalize, override-merge, write, and invalidate one provider catalog."""

    if not provider_config.models_endpoint:
        raise ValueError(f"Provider '{provider_config.id}' does not define a models_endpoint")

    raw_filter = raw_filter or PassthroughRawFilter()
    model_filter = model_filter or PassthroughModelFilter()

    fetched_at = datetime.now(UTC).isoformat()
    url = _join_url(provider_config.base_url, provider_config.models_endpoint)
    try:
        adapter_class = _adapter_class_for_discovery(provider_config.adapter)

        raw_payload, raw_models = await _fetch_raw_models(
            url,
            provider_config,
            credential_value,
            adapter_class,
            credential_connection,
        )

        # Some providers (e.g. OpenRouter) require supplementary API calls
        # with different query parameters to discover dedicated task models
        # (STT, TTS) that are excluded from the default model listing.
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
                except (httpx.HTTPError, ValueError) as exc:
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
                        raw_models.append(model)
                        seen_ids.add(model_id)
                        # Also merge into the raw payload so the dump is complete.
                        if isinstance(raw_payload, dict) and "data" in raw_payload:
                            raw_payload["data"].append(model)

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

        overrides_path = _overrides_path(resources_dir, provider_config.id)
        merged_models = apply_overrides(normalized_models, overrides_path)

        output_path = models_dir / f"{provider_config.id}.json"
        output_data = {
            "provider_id": provider_config.id,
            "source": "discovery",
            "fetched_at": fetched_at,
            "models": merged_models,
        }
        output_path.write_text(
            json.dumps(output_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (httpx.HTTPError, json.JSONDecodeError, OSError, ValueError) as exc:
        raise ModelDiscoveryError(
            f"Model discovery failed for provider '{provider_config.id}': {exc}"
        ) from exc

    ModelRegistry.invalidate(resources_dir)

    return {
        "provider_id": provider_config.id,
        "model_count": len(merged_models),
        "fetched_at": fetched_at,
    }


def apply_overrides(
    models: Mapping[str, Model | Mapping[str, Any]], overrides_path: Path
) -> dict[str, dict[str, Any]]:
    """Apply optional field-level model overrides to normalized model data."""

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
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    raw_models = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        raise ValueError("Models response must contain a list or a data list")

    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise ValueError("Every raw model entry must be an object")
    return payload, raw_models


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


def _model_to_data(model: Model | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(model, Model):
        data = {
            "name": model.name,
            "capabilities": {
                "vision": model.capabilities.vision,
                "tools": model.capabilities.tools,
                "json_mode": model.capabilities.json_mode,
                "reasoning": {"supported": model.capabilities.reasoning.supported},
                "input_modalities": list(model.capabilities.input_modalities),
                "output_modalities": list(model.capabilities.output_modalities),
                "supported_parameters": list(model.capabilities.supported_parameters),
                "task_types": list(model.capabilities.task_types),
            },
            "context_window": model.context_window,
            "max_output_tokens": model.max_output_tokens,
        }
        metadata = _plain_data(model.metadata)
        if metadata:
            data["metadata"] = metadata
        return data
    return dict(model)


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
    _read_bool(reasoning, "supported")
    _read_string_list(caps, "input_modalities")
    _read_string_list(caps, "output_modalities")
    _read_string_list(caps, "supported_parameters")
    _read_string_list(caps, "task_types")
    _read_int(model_data, "context_window")
    _read_optional_int(model_data, "max_output_tokens")
    if not model_id:
        raise ValueError("Override-only model id must not be empty")


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
    "opencode_go": OpenCodeGoAdapter,
    "openrouter": OpenRouterAdapter,
    "minimax": MiniMaxAdapter,
    "mistral": MistralAdapter,
    "github_copilot": GitHubCopilotAdapter,
    "openai_subscription": OpenAISubscriptionAdapter,
}
