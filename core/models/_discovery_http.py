"""Provider catalog request construction, authenticated HTTP fetches and discovery routing."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from core.providers.adapter import ProviderAdapter
from core.providers.adapter_types import discovery_adapter_type
from core.providers.errors import NetworkError
from core.providers.http import classify_http_status, wrap_network_error
from core.providers.providers import ConnectionConfig, ProviderConfig
from core.providers.token_getter import OAuthRequestRecovery
from core.utils.errors import ProviderError
from core.utils.logging import get_logger
from core.utils.retry import retry_async
from core.utils.tls import shared_ssl_context

_LOGGER = get_logger("models.discovery")
_HeaderSource = dict[str, str] | Callable[[], Awaitable[dict[str, str]]]


@dataclass(frozen=True)
class DiscoveryRequest:
    """The primary catalog request Model discovery sends for one Connection.

    ``url`` is the effective models endpoint - Connection ``base_url`` and
    ``models_endpoint`` over Provider values - with the Adapter's discovery
    query parameters. ``base_url`` is the effective base for follow-up catalog
    requests. :meth:`headers` renders the request headers for one credential.
    """

    provider_config: ProviderConfig
    connection: ConnectionConfig | None
    adapter_class: Any
    base_url: str
    url: str

    def headers(self, credential_value: str) -> dict[str, str]:
        """Return the catalog request headers for *credential_value*.

        Provider extra headers, the Connection auth header (omitted for a
        keyless Connection or an empty credential), then the Adapter's
        discovery headers. An Adapter that derives required routing from the
        credential may raise ``ProviderAuthError``.
        """

        return _build_headers(
            self.provider_config, credential_value, self.adapter_class, self.connection
        )


async def build_discovery_request(
    provider_config: ProviderConfig,
    connection: ConnectionConfig | None = None,
) -> DiscoveryRequest:
    """Resolve the primary catalog request exactly as :func:`refresh_models` sends it.

    Time-sensitive query parameters an Adapter resolves from a public source
    fall back to its static parameters on failure, as during refresh.

    Raises:
        ValueError: The Connection has no effective ``models_endpoint`` or the
            Provider's Adapter has no discovery normalizer.
    """

    base_url, models_endpoint = _require_discovery_target(provider_config, connection)
    adapter_class = _adapter_class_for_discovery(provider_config.adapter)
    discovery_params = await _resolve_discovery_params(adapter_class, connection)
    return DiscoveryRequest(
        provider_config=provider_config,
        connection=connection,
        adapter_class=adapter_class,
        base_url=base_url,
        url=_append_query_params(_join_url(base_url, models_endpoint), discovery_params),
    )


async def _fetch_raw_models(
    url: str,
    headers: _HeaderSource,
    *,
    auth_recovery: OAuthRequestRecovery | None = None,
) -> list[Mapping[str, Any]]:
    payload = await _fetch_json_payload(url, headers, auth_recovery=auth_recovery)

    raw_models = _raw_models_from_payload(payload)
    if not isinstance(raw_models, list):
        raise ValueError("Models response must contain a list, a data list, or a models list")

    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise ValueError("Every raw model entry must be an object")
    return raw_models


async def _fetch_json_payload(
    url: str,
    headers: _HeaderSource,
    *,
    auth_recovery: OAuthRequestRecovery | None = None,
) -> Any:
    """GET *url* and return the parsed JSON body with retry semantics.

    Catalog fetches share the provider path's transient-failure handling:
    transport/timeout errors and retryable statuses (429/500/502/503/504) are
    re-issued with backoff + Retry-After. An opted-in Connection can refresh a
    rejected token once after 401; recovery failure, a second 401, and other
    fatal statuses propagate without another auth recovery attempt.
    """

    return await _request_json_payload(
        "GET", url, headers, idempotent=True, auth_recovery=auth_recovery
    )


async def _post_json_payload(
    url: str,
    headers: _HeaderSource,
    payload: dict[str, Any],
    *,
    auth_recovery: OAuthRequestRecovery | None = None,
) -> Any:
    """POST *payload* to *url* and return the parsed JSON body with retry semantics.

    The POST twin of :func:`_fetch_json_payload`, used by adapter enrichment
    hooks whose detail endpoints are POST-only (e.g. Ollama's ``/api/show``).
    HTTP 500 remains fatal because repeating a non-idempotent POST may duplicate
    work; the method-agnostic transient statuses retain their shared behavior.
    """

    return await _request_json_payload(
        "POST", url, headers, idempotent=False, payload=payload, auth_recovery=auth_recovery
    )


async def _request_json_payload(
    method: Literal["GET", "POST"],
    url: str,
    headers: _HeaderSource,
    *,
    idempotent: bool,
    payload: dict[str, Any] | None = None,
    auth_recovery: OAuthRequestRecovery | None = None,
) -> Any:
    """Execute one discovery request with the caller's explicit retry safety."""

    async def _request() -> Any:
        request_headers = await headers() if callable(headers) else headers
        async with httpx.AsyncClient(timeout=60.0, verify=shared_ssl_context()) as client:
            try:
                response = await client.request(method, url, headers=request_headers, json=payload)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            if auth_recovery is not None:
                auth_recovery.record_response(
                    response.status_code,
                    request_headers,
                    response.text if response.status_code >= 400 else "",
                )
            if response.status_code >= 400:
                error_body = response.text
                detail = (
                    f"{response.status_code} {error_body}".strip()
                    if error_body
                    else str(response.status_code)
                )
                classify_http_status(
                    response.status_code,
                    idempotent=idempotent,
                    detail=detail,
                    response_headers=response.headers,
                )
            return response.json()

    if auth_recovery is not None:
        return await auth_recovery.run(lambda: retry_async(_request))
    return await retry_async(_request)


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
        # Keyless connections (type "none") have an empty auth header and an
        # empty credential — sending "Bearer " garbage would be wrong.
        if auth.header and credential_value:
            headers[auth.header] = f"{auth.prefix}{credential_value}"
    discovery_headers = getattr(adapter_class, "discovery_headers", None)
    if callable(discovery_headers):
        return dict(
            discovery_headers(provider_config, credential_value, headers, connection=connection)
        )
    return headers


def _join_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def _require_discovery_target(
    provider_config: ProviderConfig,
    credential_connection: ConnectionConfig | None,
) -> tuple[str, str]:
    """Return the effective ``(base_url, models_endpoint)`` or raise ``ValueError``."""

    base_url, models_endpoint = _resolve_discovery_target(provider_config, credential_connection)
    if not models_endpoint:
        raise ValueError(
            f"Provider '{provider_config.id}' connection "
            f"'{credential_connection.id if credential_connection else None}' "
            "does not define a models_endpoint"
        )
    return base_url, models_endpoint


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


def _adapter_class_for_discovery(adapter: str) -> type[ProviderAdapter]:
    adapter_class = discovery_adapter_type(adapter)
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


def _get_discovery_params(
    adapter_class: Any, connection: ConnectionConfig | None
) -> dict[str, str]:
    """Return query parameters for the primary model-discovery request."""

    method = getattr(adapter_class, "discovery_params", None)
    if callable(method):
        result: dict[str, str] = method(connection=connection)
        return result
    return {}


async def _resolve_discovery_params(
    adapter_class: Any, connection: ConnectionConfig | None
) -> dict[str, str]:
    """Let an adapter refresh time-sensitive catalog parameters, fail-soft."""

    params = _get_discovery_params(adapter_class, connection)
    method = getattr(adapter_class, "resolve_discovery_params", None)
    if not callable(method):
        return params

    async def _fetch_public_json(url: str) -> Any:
        return await _fetch_json_payload(url, {})

    try:
        resolved = await method(_fetch_public_json, connection=connection)
        if not isinstance(resolved, Mapping):
            raise ValueError("resolved discovery parameters must be an object")
        dynamic_params = {
            key: value
            for key, value in resolved.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if len(dynamic_params) != len(resolved):
            raise ValueError("resolved discovery parameters must contain only strings")
    except (httpx.HTTPError, ProviderError, NetworkError, json.JSONDecodeError, ValueError) as exc:
        _LOGGER.warning(
            "Dynamic discovery parameters failed for adapter '%s'; using fallbacks: %s",
            adapter_class.__name__,
            exc,
        )
        return params

    return {**params, **dynamic_params}


def _append_query_params(url: str, params: dict[str, str]) -> str:
    """Append query parameters to a URL, preserving any existing query string."""
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    encoded = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{url}{separator}{encoded}"
