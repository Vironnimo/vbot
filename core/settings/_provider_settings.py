"""Settings-owned Provider records, Model capabilities and local context-window normalization."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from core.models.models import MODEL_TASK_ORDER
from core.settings._json_settings import (
    normalize_json_object,
)
from core.settings.settings import (
    SettingsValidationError,
    parse_openrouter_routing,
)
from core.utils.errors import StorageError

CUSTOM_PROVIDER_ADAPTERS = frozenset({"openai_compatible"})


CUSTOM_PROVIDER_AUTH_TYPES = frozenset({"api_key", "none"})


CUSTOM_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


CUSTOM_PROVIDER_CONNECTION_ID = "default"


CUSTOM_PROVIDER_MODEL_FIELDS = frozenset(
    {
        "name",
        "context_window",
        "max_output_tokens",
        "capabilities",
    }
)


CUSTOM_MODEL_CAPABILITY_FIELDS = frozenset(
    {
        "vision",
        "tools",
        "json_mode",
        "reasoning",
        "input_modalities",
        "output_modalities",
        "supported_parameters",
        "supported_voices",
        "task_types",
        "task_options",
    }
)


def normalize_local_models_settings(local_models: Any) -> dict[str, Any]:
    """Return the normalized local-models settings section.

    Shape: ``{"context_windows": {"<provider>/<model_id>": positive int}}`` —
    the user-configured effective context window per flagged-local model
    (see ``resolve_effective_context_window`` in ``core/providers``).
    """

    section = _coerce_local_models_section(local_models)
    raw_windows = section.get("context_windows")
    if raw_windows is None:
        return {"context_windows": {}}
    if not isinstance(raw_windows, Mapping):
        raise StorageError("Expected settings.local_models.context_windows to be an object")

    context_windows: dict[str, int] = {}
    for key, value in raw_windows.items():
        if not isinstance(key, str) or "/" not in key or not key.strip():
            raise StorageError(
                "local_models.context_windows keys must be '<provider>/<model_id>' strings"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StorageError(f"local_models.context_windows['{key}'] must be a positive integer")
        context_windows[key] = value
    return {"context_windows": context_windows}


def _coerce_local_models_section(local_models: Any) -> dict[str, Any]:
    if local_models is None:
        return {}
    if not isinstance(local_models, Mapping):
        raise StorageError("Expected settings.local_models to be an object")
    return dict(local_models)


def normalize_providers_settings(providers: Any) -> dict[str, Any]:
    """Return the normalized providers settings section.

    ``connections`` carries per-Connection enabled overrides. ``openrouter``
    carries vBot-owned routing policy rendered by ``OpenRouterAdapter`` onto
    each request; it is independent from Connection and Account identity.
    ``custom`` carries user-owned Provider definitions and manual Model facts;
    credential values never belong in this section.
    """

    if providers is None:
        section: dict[str, Any] = {}
    elif isinstance(providers, Mapping):
        section = dict(providers)
    else:
        raise StorageError("Expected settings.providers to be an object")

    unsupported_fields = sorted(set(section) - {"connections", "custom", "openrouter"})
    if unsupported_fields:
        raise StorageError(f"Unsupported providers settings: {', '.join(unsupported_fields)}")

    raw_connections = section.get("connections", {})
    if not isinstance(raw_connections, Mapping):
        raise StorageError("Expected settings.providers.connections to be an object")

    connections: dict[str, bool] = {}
    for key, enabled in raw_connections.items():
        if not isinstance(key, str) or ":" not in key or not key.strip():
            raise StorageError(
                "providers.connections keys must be '<provider>:<connection>' strings"
            )
        if not isinstance(enabled, bool):
            raise StorageError(f"providers.connections['{key}'] must be a boolean")
        connections[key] = enabled
    raw_openrouter = section.get("openrouter", {})
    if not isinstance(raw_openrouter, Mapping):
        raise StorageError("Expected settings.providers.openrouter to be an object")
    unsupported_openrouter_fields = sorted(set(raw_openrouter) - {"routing"})
    if unsupported_openrouter_fields:
        raise StorageError(
            f"Unsupported providers.openrouter settings: {', '.join(unsupported_openrouter_fields)}"
        )
    try:
        routing = parse_openrouter_routing(raw_openrouter.get("routing", {}))
    except SettingsValidationError as exc:
        raise StorageError(str(exc).replace("params.", "settings.", 1)) from exc

    return {
        "connections": connections,
        "custom": normalize_custom_providers_settings(section.get("custom")),
        "openrouter": {"routing": routing},
    }


def normalize_custom_providers_settings(custom: Any) -> dict[str, dict[str, Any]]:
    """Normalize the secret-free ``providers.custom`` map."""

    if custom is None:
        return {}
    if not isinstance(custom, Mapping):
        raise StorageError("Expected settings.providers.custom to be an object")

    normalized: dict[str, dict[str, Any]] = {}
    for raw_provider_id, raw_provider in custom.items():
        provider_id = normalize_custom_provider_id(raw_provider_id)
        normalized[provider_id] = normalize_custom_provider_settings(
            provider_id,
            raw_provider,
        )
    return normalized


def normalize_custom_provider_id(value: Any) -> str:
    """Return a canonical Custom Provider id or raise."""

    if not isinstance(value, str) or CUSTOM_PROVIDER_ID_PATTERN.fullmatch(value) is None:
        raise StorageError(
            "Custom Provider ids must use lowercase letters and digits in hyphen-separated segments"
        )
    return value


def normalize_custom_provider_settings(
    provider_id: str,
    provider: Any,
) -> dict[str, Any]:
    """Normalize one secret-free Custom Provider record."""

    normalize_custom_provider_id(provider_id)
    path = f"settings.providers.custom.{provider_id}"
    if not isinstance(provider, Mapping):
        raise StorageError(f"Expected {path} to be an object")
    unsupported = sorted(
        set(provider)
        - {
            "name",
            "adapter",
            "base_url",
            "auth",
            "models_endpoint",
            "defaults",
            "models",
        }
    )
    if unsupported:
        raise StorageError(f"Unsupported {path} fields: {', '.join(unsupported)}")

    name = _non_empty_string(provider.get("name"), f"{path}.name")
    adapter = _non_empty_string(provider.get("adapter"), f"{path}.adapter")
    if adapter not in CUSTOM_PROVIDER_ADAPTERS:
        supported = ", ".join(sorted(CUSTOM_PROVIDER_ADAPTERS))
        raise StorageError(f"{path}.adapter must be one of: {supported}")

    base_url = _normalize_custom_provider_base_url(provider.get("base_url"), path=path)
    auth = _non_empty_string(provider.get("auth", "api_key"), f"{path}.auth")
    if auth not in CUSTOM_PROVIDER_AUTH_TYPES:
        supported = ", ".join(sorted(CUSTOM_PROVIDER_AUTH_TYPES))
        raise StorageError(f"{path}.auth must be one of: {supported}")

    raw_models_endpoint = provider.get("models_endpoint")
    models_endpoint: str | None = None
    if raw_models_endpoint is not None:
        models_endpoint = _non_empty_string(
            raw_models_endpoint,
            f"{path}.models_endpoint",
        )
        if not models_endpoint.startswith("/") or models_endpoint.startswith("//"):
            raise StorageError(f"{path}.models_endpoint must be an absolute URL path")
        endpoint_parts = urlsplit(models_endpoint)
        if endpoint_parts.scheme or endpoint_parts.netloc or endpoint_parts.fragment:
            raise StorageError(f"{path}.models_endpoint must be an absolute URL path")

    defaults = normalize_json_object(provider.get("defaults", {}), f"{path}.defaults")
    models = _normalize_custom_provider_models(provider.get("models", {}), path=path)
    return {
        "name": name,
        "adapter": adapter,
        "base_url": base_url,
        "auth": auth,
        "models_endpoint": models_endpoint,
        "defaults": defaults,
        "models": models,
    }


def _normalize_custom_provider_base_url(value: Any, *, path: str) -> str:
    base_url = _non_empty_string(value, f"{path}.base_url").rstrip("/")
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise StorageError(
            f"{path}.base_url must be an absolute HTTP(S) URL without "
            "credentials, query, or fragment"
        )
    return base_url


def _normalize_custom_provider_models(
    models: Any,
    *,
    path: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(models, Mapping):
        raise StorageError(f"Expected {path}.models to be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_model_id, raw_model in models.items():
        if not isinstance(raw_model_id, str) or not raw_model_id.strip() or "::" in raw_model_id:
            raise StorageError(f"{path}.models keys must be non-empty wire Model ids without '::'")
        model_id = raw_model_id.strip()
        normalized[model_id] = _normalize_custom_model(
            model_id,
            raw_model,
            path=f"{path}.models",
        )
    return normalized


def _normalize_custom_model(
    model_id: str,
    model: Any,
    *,
    path: str,
) -> dict[str, Any]:
    model_path = f"{path}['{model_id}']"
    if not isinstance(model, Mapping):
        raise StorageError(f"Expected {model_path} to be an object")
    unsupported = sorted(set(model) - CUSTOM_PROVIDER_MODEL_FIELDS)
    if unsupported:
        raise StorageError(f"Unsupported {model_path} fields: {', '.join(unsupported)}")

    context_window = _optional_positive_integer(
        model.get("context_window"),
        f"{model_path}.context_window",
    )
    max_output_tokens = _optional_positive_integer(
        model.get("max_output_tokens"),
        f"{model_path}.max_output_tokens",
    )
    return {
        "name": _non_empty_string(model.get("name", model_id), f"{model_path}.name"),
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        "capabilities": _normalize_custom_model_capabilities(
            model.get("capabilities", {}),
            path=f"{model_path}.capabilities",
        ),
    }


def _normalize_custom_model_capabilities(
    capabilities: Any,
    *,
    path: str,
) -> dict[str, Any]:
    if not isinstance(capabilities, Mapping):
        raise StorageError(f"Expected {path} to be an object")
    unsupported = sorted(set(capabilities) - CUSTOM_MODEL_CAPABILITY_FIELDS)
    if unsupported:
        raise StorageError(f"Unsupported {path} fields: {', '.join(unsupported)}")

    input_modalities = _custom_string_list(
        capabilities.get("input_modalities", ["text"]),
        f"{path}.input_modalities",
    )
    output_modalities = _custom_string_list(
        capabilities.get("output_modalities", ["text"]),
        f"{path}.output_modalities",
    )
    task_types = _custom_string_list(
        capabilities.get("task_types", []),
        f"{path}.task_types",
    )
    unknown_tasks = sorted(set(task_types) - set(MODEL_TASK_ORDER))
    if unknown_tasks:
        raise StorageError(
            f"{path}.task_types contains unsupported values: {', '.join(unknown_tasks)}"
        )

    return {
        "vision": _custom_bool(capabilities.get("vision", False), f"{path}.vision"),
        "tools": _custom_bool(capabilities.get("tools", True), f"{path}.tools"),
        "json_mode": _custom_bool(
            capabilities.get("json_mode", False),
            f"{path}.json_mode",
        ),
        "reasoning": _custom_bool(
            capabilities.get("reasoning", False),
            f"{path}.reasoning",
        ),
        "input_modalities": input_modalities,
        "output_modalities": output_modalities,
        "supported_parameters": _custom_string_list(
            capabilities.get("supported_parameters", []),
            f"{path}.supported_parameters",
        ),
        "supported_voices": _custom_string_list(
            capabilities.get("supported_voices", []),
            f"{path}.supported_voices",
        ),
        "task_types": task_types,
        "task_options": normalize_json_object(
            capabilities.get("task_options", {}),
            f"{path}.task_options",
        ),
    }


def _custom_string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise StorageError(f"Expected {path} to be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise StorageError(f"{path} must contain only non-empty strings")
        stripped = item.strip()
        if stripped not in normalized:
            normalized.append(stripped)
    return normalized


def _custom_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise StorageError(f"{path} must be a boolean")
    return value


def _optional_positive_integer(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StorageError(f"{path} must be a positive integer or null")
    return value


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"{path} must be a non-empty string")
    return value.strip()
