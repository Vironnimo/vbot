"""Dynamic model discovery pipeline.

The discovery pipeline fetches provider model metadata, normalizes it into the
same JSON shape consumed by :class:`ModelRegistry`, applies optional overrides,
and writes the resulting provider model file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.providers import ConnectionConfig, ProviderConfig
from core.utils.errors import VBotError

DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_CONTEXT_WINDOW = 0
OVERRIDE_FILE_SUFFIX = ".overrides.json"
CONTEXT_WINDOW_KEYS = ("context_length", "context_window", "contextWindow")
MAX_OUTPUT_TOKEN_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "maxOutputTokens",
    "maxCompletionTokens",
)
JSON_MODE_PARAMETER_NAMES = {"response_format", "structured_outputs", "json_mode"}
REASONING_PARAMETER_NAMES = {"reasoning", "include_reasoning", "reasoning_effort"}


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


def normalize_openrouter(
    raw_model: Mapping[str, Any], provider_defaults: Mapping[str, Any] | None
) -> Model:
    """Normalize one OpenRouter ``/models`` entry into a vBot ``Model``."""

    architecture = _read_mapping(raw_model, "architecture")
    top_provider = _read_mapping(raw_model, "top_provider")
    supported_parameters = _read_string_list(raw_model, "supported_parameters")
    input_modalities = _read_string_list(architecture, "input_modalities")

    max_completion_tokens = top_provider.get("max_completion_tokens")
    if max_completion_tokens is None:
        max_completion_tokens = _provider_default_max_tokens(provider_defaults)

    return Model(
        model_id=_read_string(raw_model, "id"),
        name=_read_string(raw_model, "name"),
        capabilities=Capabilities(
            vision="image" in input_modalities,
            tools="tools" in supported_parameters,
            json_mode=(
                "response_format" in supported_parameters
                or "structured_outputs" in supported_parameters
            ),
            reasoning=ReasoningCapabilities(
                supported=(
                    "reasoning" in supported_parameters
                    or "include_reasoning" in supported_parameters
                ),
            ),
        ),
        context_window=_read_int(raw_model, "context_length"),
        max_output_tokens=int(max_completion_tokens),
    )


def normalize_openai_compatible_tolerant(
    raw_model: Mapping[str, Any], provider_defaults: Mapping[str, Any] | None
) -> Model:
    """Normalize a tolerant OpenAI-compatible ``/models`` entry into a vBot ``Model``."""

    architecture = _read_optional_mapping(raw_model, "architecture")
    top_provider = _read_optional_mapping(raw_model, "top_provider")
    supported_parameters = _read_optional_string_set(raw_model, "supported_parameters")

    model_id = _read_non_empty_string(raw_model, "id")
    name = _read_optional_non_empty_string(raw_model, "name") or model_id

    return Model(
        model_id=model_id,
        name=name,
        capabilities=Capabilities(
            vision=_has_image_modality(raw_model, architecture),
            tools=_supports_tools_by_default(raw_model, top_provider, architecture),
            json_mode=_supports_json_mode(
                raw_model, top_provider, architecture, supported_parameters
            ),
            reasoning=ReasoningCapabilities(
                supported=_supports_reasoning(
                    raw_model,
                    top_provider,
                    architecture,
                    supported_parameters,
                ),
            ),
        ),
        context_window=_read_first_optional_int(raw_model, CONTEXT_WINDOW_KEYS)
        or _read_first_optional_int(architecture, CONTEXT_WINDOW_KEYS)
        or DEFAULT_CONTEXT_WINDOW,
        max_output_tokens=_read_first_optional_int(top_provider, MAX_OUTPUT_TOKEN_KEYS)
        or _read_first_optional_int(raw_model, MAX_OUTPUT_TOKEN_KEYS)
        or _read_first_optional_int(architecture, MAX_OUTPUT_TOKEN_KEYS)
        or _provider_default_max_tokens(provider_defaults),
    )


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
        normalizer = _normalizer_for_provider(provider_config)
        if normalizer is None:
            raise ValueError(
                f"No model normalizer registered for adapter '{provider_config.adapter}'"
            )

        raw_models = await _fetch_raw_models(
            url,
            provider_config,
            credential_value,
            credential_connection,
        )

        normalized_models: dict[str, Model] = {}
        for raw_model in raw_models:
            if not raw_filter.accepts(raw_model):
                continue
            model = normalizer(raw_model, provider_config.defaults)
            if model_filter.accepts(model):
                normalized_models[model.model_id] = model

        overrides_path = _overrides_path(resources_dir, provider_config.id)
        merged_models = apply_overrides(normalized_models, overrides_path)

        models_dir = resources_dir / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
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
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
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
    credential_connection: ConnectionConfig | None = None,
) -> list[Mapping[str, Any]]:
    headers = _build_headers(provider_config, credential_value, credential_connection)
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
    return raw_models


def _build_headers(
    provider_config: ProviderConfig,
    credential_value: str,
    credential_connection: ConnectionConfig | None = None,
) -> dict[str, str]:
    headers = dict(provider_config.extra_headers or {})
    connection = credential_connection or next(iter(provider_config.connections), None)
    if connection is not None:
        auth = connection.auth
        headers[auth.header] = f"{auth.prefix}{credential_value}"
    return headers


def _join_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def _model_to_data(model: Model | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(model, Model):
        data = asdict(model)
        data.pop("model_id")
        return data
    return dict(model)


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
    _read_int(model_data, "context_window")
    _read_int(model_data, "max_output_tokens")
    if not model_id:
        raise ValueError("Override-only model id must not be empty")


def _provider_default_max_tokens(provider_defaults: Mapping[str, Any] | None) -> int:
    if provider_defaults is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    max_tokens = provider_defaults.get("max_tokens")
    if max_tokens is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    return int(max_tokens)


def _normalizer_for_provider(provider_config: ProviderConfig):
    discovery_strategy = provider_config.model_discovery or provider_config.adapter
    return _NORMALIZER_MAP.get(discovery_strategy)


def _read_optional_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _read_non_empty_string(data: Mapping[str, Any], key: str) -> str:
    value = _read_string(data, key)
    if not value:
        raise ValueError(f"Expected '{key}' to be a non-empty string")
    return value


def _read_optional_non_empty_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _read_optional_string_set(data: Mapping[str, Any], key: str) -> set[str]:
    value = data.get(key)
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _read_first_optional_int(data: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        parsed_value = _parse_optional_int(value)
        if parsed_value is not None:
            return parsed_value
    return None


def _parse_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _has_image_modality(raw_model: Mapping[str, Any], architecture: Mapping[str, Any]) -> bool:
    return _modalities_include_image(raw_model) or _modalities_include_image(architecture)


def _modalities_include_image(data: Mapping[str, Any]) -> bool:
    for key in ("input_modalities", "inputModalities", "modalities"):
        value = data.get(key)
        if isinstance(value, list) and any(_modality_is_image(item) for item in value):
            return True
    return False


def _modality_is_image(value: Any) -> bool:
    if isinstance(value, str):
        return "image" in value.lower()
    if isinstance(value, dict):
        return any(_modality_is_image(item) for item in value.values())
    return False


def _supports_tools_by_default(*metadata_sources: Mapping[str, Any]) -> bool:
    explicit_value = _read_first_optional_bool(
        metadata_sources,
        ("supports_tools", "tools", "tool_calls", "function_calling"),
    )
    return explicit_value is not False


def _supports_json_mode(
    raw_model: Mapping[str, Any],
    top_provider: Mapping[str, Any],
    architecture: Mapping[str, Any],
    supported_parameters: set[str],
) -> bool:
    if supported_parameters & JSON_MODE_PARAMETER_NAMES:
        return True
    explicit_value = _read_first_optional_bool(
        (raw_model, top_provider, architecture),
        (
            "supports_json_mode",
            "json_mode",
            "supports_structured_outputs",
            "structured_outputs",
        ),
    )
    return explicit_value is True


def _supports_reasoning(
    raw_model: Mapping[str, Any],
    top_provider: Mapping[str, Any],
    architecture: Mapping[str, Any],
    supported_parameters: set[str],
) -> bool:
    if supported_parameters & REASONING_PARAMETER_NAMES:
        return True
    if _read_reasoning_supported(raw_model) or _read_reasoning_supported(architecture):
        return True
    explicit_value = _read_first_optional_bool(
        (raw_model, top_provider, architecture),
        ("supports_reasoning", "reasoning_supported"),
    )
    if explicit_value is True:
        return True
    return _has_non_empty_list(raw_model, "reasoning_efforts") or _has_non_empty_list(
        raw_model,
        "reasoningEfforts",
    )


def _read_reasoning_supported(data: Mapping[str, Any]) -> bool:
    reasoning = data.get("reasoning")
    return isinstance(reasoning, dict) and reasoning.get("supported") is True


def _read_first_optional_bool(
    metadata_sources: tuple[Mapping[str, Any], ...], keys: tuple[str, ...]
) -> bool | None:
    for source in metadata_sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
    return None


def _has_non_empty_list(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    return isinstance(value, list) and len(value) > 0


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


def _read_string_list(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Expected '{key}' to be a list of strings")
    return value


def _read_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Expected '{key}' to be an integer")
    return value


def _read_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Expected '{key}' to be a boolean")
    return value


_NORMALIZER_MAP = {
    "openai_compatible": normalize_openai_compatible_tolerant,
    "openrouter": normalize_openrouter,
}
